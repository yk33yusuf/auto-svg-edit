#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto-svg-clean
==============
Tek renk (lazer kesim / POD baskı) SVG tasarımlarinda ana govdeye HICBIR yerden
bagli olmayan "yuzen" koyu adacıklari (floating islands) tespit edip silen
FastAPI mikroservisi. Coolify'a deploy edilmek uzere tasarlandi.

Endpoint'ler:
  GET  /health          -> {"status": "ok"}
  POST /analyze         -> Sadece tespit (silmeden): adalarin listesi + JSON rapor
  POST /clean           -> Temizlenmis SVG dondurur (image/svg+xml)
                           X-Removed-Count header'inda silinen ada sayisi

Girdi (her iki POST icin):
  - multipart form-data 'file' alani (yuklenen .svg)   VEYA
  - ham istek govdesi (raw SVG metni, Content-Type: image/svg+xml | text/plain)

Sorgu parametreleri:
  dark_threshold (float, varsayilan 110) : 0-255 parlaklik; altindakiler "koyu"
  scale          (float, varsayilan 2.0) : render hassasiyeti
  keep_larger_than (float, opsiyonel)    : bu pt2 alandan buyuk adalari silme
"""

import io
import json
import math
import os
import re

import numpy as np
from fastapi import FastAPI, File, Form, Query, Request, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from PIL import Image
from scipy import ndimage
import cairosvg
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

app = FastAPI(title="auto-svg-clean", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
STENCIL_FONTS = {
    "stardos": "StardosStencil-Bold.ttf",
    "saira": "SairaStencilOne-Regular.ttf",
    "allerta": "AllertaStencil-Regular.ttf",
}
_font_cache = {}


def _load_font(key):
    if key not in STENCIL_FONTS:
        raise ValueError(f"Bilinmeyen font '{key}'. Secenekler: {list(STENCIL_FONTS)}")
    if key not in _font_cache:
        _font_cache[key] = TTFont(os.path.join(_FONT_DIR, STENCIL_FONTS[key]))
    return _font_cache[key]


def stencil_text_svg(text, font="stardos", size=120.0, letter_spacing=6.0,
                     mode="knockout", color="#000000", pad=40.0, radius=0.0,
                     curve=0.0, fit_width=None, vscale=1.0):
    """
    Stencil fontla yaziyi SVG'ye cevirir.
      fit_width : None = size parametresini kullan.
                  Sayi verilirse harfleri tam bu genislige sigdir (harf araligini
                  koruyarak s otomatik hesaplanir). Harfler buyudugunde aralik acilmaz.
      vscale    : Harflerin dikey olcegi. 1.0=normal, 2.0=iki kat uzun.
                  Genislige dokunmadan banner yuksekligini doldurmak icin kullan.
      curve     : 0=duz. +yukari kavis, -asagi kavis (derece).
    """
    f = _load_font(font)
    glyphset = f.getGlyphSet()
    cmap = f.getBestCmap()
    upm = f["head"].unitsPerEm
    hmtx = f["hmtx"]
    asc = f["hhea"].ascent
    desc = f["hhea"].descent

    # 1) on gecis: tum ilerleme (advance) degerlerini topla
    items = []
    for ch in text:
        gname = cmap.get(ord(ch)) if ch != " " else None
        adv_f = hmtx[gname][0] if gname else int(upm * 0.42)
        items.append((gname, adv_f))

    total_adv_f = sum(a for _, a in items)
    n_gaps = max(0, len(items) - 1)

    # 2) olcek hesapla
    if fit_width is not None and total_adv_f > 0:
        inner = max(1.0, fit_width - 2 * pad)
        s_fit = (inner - letter_spacing * n_gaps) / total_adv_f
        s = max(s_fit, 0.01)          # sifira dusturme
    else:
        s = size / upm

    # 3) toplam yatay uzunluk ve kavis parametresi
    L = total_adv_f * s + letter_spacing * n_gaps
    A = math.radians(curve)
    arc = abs(A) > 1e-4
    R = (L / A) if arc else 0.0

    # 4) her harf icin transform matrisini hesapla ve path al
    glyph_ds = []
    minx = miny = 1e9
    maxx = maxy = -1e9
    cum = 0.0

    for gname, adv_f in items:
        adv_s = adv_f * s
        if gname is None:
            cum += adv_s + letter_spacing
            continue
        center = cum + adv_s / 2.0
        if arc:
            t = center - L / 2.0
            ang = t / R
            gx = R * math.sin(ang)
            gy = R - R * math.cos(ang)
            theta = ang
        else:
            gx = center
            gy = 0.0
            theta = 0.0

        cos_t, sin_t = math.cos(theta), math.sin(theta)
        # vscale sadece dikey ekseni etkiler (yx ve yy'ye uygula)
        xx = s * cos_t
        xy = s * sin_t
        yx = s * vscale * sin_t
        yy = -s * vscale * cos_t
        dx = gx - s * cos_t * (adv_f / 2.0)
        dy = gy - s * sin_t * (adv_f / 2.0)

        pen = SVGPathPen(glyphset)
        glyphset[gname].draw(TransformPen(pen, (xx, xy, yx, yy, dx, dy)))
        d = pen.getCommands()
        if d:
            glyph_ds.append(d)

        # bbox icin em-kutu koselerini donustur
        for fx in (0.0, float(adv_f)):
            for fy in (float(desc), float(asc)):
                px = xx * fx + yx * fy + dx
                py = xy * fx + yy * fy + dy
                minx, maxx = min(minx, px), max(maxx, px)
                miny, maxy = min(miny, py), max(maxy, py)
        cum += adv_s + letter_spacing

    vx, vy = minx - pad, miny - pad
    W = (maxx - minx) + 2 * pad
    Hh = (maxy - miny) + 2 * pad

    if arc or mode == "solid":
        body = f'<path d="{" ".join(glyph_ds)}" fill="{color}"/>'
    else:  # knockout: banner + harfler tek evenodd path -> harfler delik
        if radius > 0:
            r = min(radius, Hh / 2, W / 2)
            rect = (f"M{vx+r:.1f},{vy:.1f} H{vx+W-r:.1f} A{r:.1f},{r:.1f} 0 0 1 {vx+W:.1f},{vy+r:.1f} "
                    f"V{vy+Hh-r:.1f} A{r:.1f},{r:.1f} 0 0 1 {vx+W-r:.1f},{vy+Hh:.1f} "
                    f"H{vx+r:.1f} A{r:.1f},{r:.1f} 0 0 1 {vx:.1f},{vy+Hh-r:.1f} "
                    f"V{vy+r:.1f} A{r:.1f},{r:.1f} 0 0 1 {vx+r:.1f},{vy:.1f} Z")
        else:
            rect = f"M{vx:.1f},{vy:.1f} H{vx+W:.1f} V{vy+Hh:.1f} H{vx:.1f} Z"
        body = (f'<path fill-rule="evenodd" fill="{color}" '
                f'd="{rect} {" ".join(glyph_ds)}"/>')

    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="{vx:.1f} {vy:.1f} {W:.1f} {Hh:.1f}" width="{W:.1f}" height="{Hh:.1f}">'
            f'{body}</svg>')






# --------------------------------------------------------------------------- #
# Cekirdek mantik
# --------------------------------------------------------------------------- #
def hex_luminance(hexstr):
    if not hexstr:
        return None
    s = hexstr.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return None
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None
    return 0.299 * r + 0.587 * g + 0.114 * b


def extract_svg_header(content):
    m = re.search(r"<svg\b[^>]*>", content, re.IGNORECASE)
    if not m:
        raise ValueError("SVG acilis etiketi bulunamadi.")
    header = m.group(0)

    def num(attr):
        mm = re.search(attr + r'="?([\d.]+)', header)
        return float(mm.group(1)) if mm else None

    w, h = num("width"), num("height")
    vb = re.search(r'viewBox="([^"]+)"', header)
    if vb:
        parts = [float(x) for x in re.split(r"[ ,]+", vb.group(1).strip())]
        if len(parts) == 4:
            return header, (w or parts[2]), (h or parts[3])
    if w and h:
        return header, w, h
    raise ValueError("SVG boyutlari (width/height/viewBox) okunamadi.")


def render_to_gray(svg_text, w, h, scale):
    png = cairosvg.svg2png(
        bytestring=svg_text.encode("utf-8"),
        output_width=int(w * scale),
        output_height=int(h * scale),
        background_color="white",
    )
    return np.array(Image.open(io.BytesIO(png)).convert("L"))


def find_and_remove_islands(content, dark_threshold=110.0, scale=2.0,
                            keep_larger_than=None, do_remove=True):
    """
    Donen: (temizlenmis_svg, rapor_dict)
    rapor: components_before/after, removed_count, removed[ {x0,y0,x1,y1,w,h,area_pt2} ]
    """
    header, W, H = extract_svg_header(content)

    # 1) Renk filtresi — hem bare <path/> hem de <g transform="..."><path/></g> kalıpları
    # Her item: (remove_str, render_fragment)
    #   remove_str    → cleaned.replace() ile SVG'den kaldırılacak metin
    #   render_fragment → pixel konumunu doğru bulmak için header+…+</svg> içine konacak parça
    dark_items = []

    # 1a) <g transform="...">...<path />...</g> — compose ile eklenen yazılar vb.
    g_blocks = re.findall(
        r'<g\b[^>]*transform="[^"]*"[^>]*>[\s\S]*?</g>',
        content, re.IGNORECASE
    )
    covered_paths = set()
    for gblock in g_blocks:
        inner_path = re.search(r'<path\b[^>]*?/>', gblock, re.IGNORECASE)
        if not inner_path:
            continue
        ptag = inner_path.group(0)
        fm = re.search(r'fill="([^"]+)"', ptag)
        lum = hex_luminance(fm.group(1) if fm else None)
        if lum is not None and lum <= dark_threshold:
            dark_items.append((gblock, gblock))
            covered_paths.add(ptag)

    # 1b) Duzce <path /> — g blogu icinde olmayan
    for ptag in re.findall(r"<path\b[^>]*?/>", content, re.IGNORECASE):
        if ptag in covered_paths:
            continue
        fm = re.search(r'fill="([^"]+)"', ptag)
        lum = hex_luminance(fm.group(1) if fm else None)
        if lum is not None and lum <= dark_threshold:
            dark_items.append((ptag, ptag))

    if not dark_items:
        raise ValueError("Koyu (siyah) path bulunamadi. dark_threshold'u yukseltin.")

    # 2) Tam render + 3) baglantili bilesen
    gray = render_to_gray(content, W, H, scale)
    black = gray < dark_threshold
    labels, n = ndimage.label(black, structure=np.ones((3, 3)))
    if n == 0:
        raise ValueError("Render'da koyu piksel yok.")
    sizes = ndimage.sum(black, labels, range(1, n + 1))
    main_label = int(np.argmax(sizes)) + 1
    main_mask = labels == main_label
    main_dil = ndimage.binary_dilation(main_mask, iterations=max(1, int(scale)))
    island_mask = black & ~main_mask

    # 4) Esleme + silme
    removed = []
    cleaned = content
    for remove_str, render_frag in dark_items:
        single = f'{header}{render_frag}</svg>'
        try:
            pmask = render_to_gray(single, W, H, scale) < dark_threshold
        except Exception:
            continue
        if (pmask & main_dil).any():
            continue  # ana govdeye degiyor -> KORU
        if not (pmask & island_mask).any():
            continue
        ys, xs = np.where(pmask)
        area_pt = pmask.sum() / (scale * scale)
        if keep_larger_than and area_pt > keep_larger_than:
            continue
        if do_remove:
            cleaned = cleaned.replace(remove_str, "", 1)
        removed.append({
            "x0": round(xs.min() / scale, 1), "y0": round(ys.min() / scale, 1),
            "x1": round(xs.max() / scale, 1), "y1": round(ys.max() / scale, 1),
            "w": round((xs.max() - xs.min()) / scale, 1),
            "h": round((ys.max() - ys.min()) / scale, 1),
            "area_pt2": round(area_pt, 1),
        })
    removed.sort(key=lambda r: r["y0"])

    # 5) Dogrulama
    n2 = n
    if do_remove:
        g2 = render_to_gray(cleaned, W, H, scale)
        _, n2 = ndimage.label(g2 < dark_threshold, structure=np.ones((3, 3)))

    report = {
        "components_before": int(n),
        "components_after": int(n2),
        "removed_count": len(removed),
        "removed": removed,
        "dark_threshold": dark_threshold,
        "scale": scale,
    }
    return cleaned, report


# --------------------------------------------------------------------------- #
# KOPRULEME: adalari silmek yerine en yakin noktadan ana govdeye bagla
# --------------------------------------------------------------------------- #
def _stamp_line(mask, x0, y0, x1, y1):
    n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    xs = np.clip(np.linspace(x0, x1, n).round().astype(int), 0, mask.shape[1] - 1)
    ys = np.clip(np.linspace(y0, y1, n).round().astype(int), 0, mask.shape[0] - 1)
    mask[ys, xs] = True


def _axis_bridge_candidates(island_mask, main_mask):
    """Yatay/dikey kopru adaylarini bul. Returns list of (gap_px,x1,y1,x2,y2,dir)."""
    edge = island_mask & ~ndimage.binary_erosion(island_mask)
    if not edge.any():
        edge = island_mask
    cands = []
    # Yatay tarama
    rows = np.where(edge.any(axis=1))[0]
    for y in rows:
        il = np.where(edge[y])[0]
        ml = np.where(main_mask[y])[0]
        if len(ml) == 0:
            continue
        for ix in il:
            hi = np.searchsorted(ml, ix + 1)
            if hi < len(ml):
                gap = int(ml[hi]) - ix
                if gap > 0:
                    cands.append((gap, ix, y, int(ml[hi]), y, 'H'))
            hi = np.searchsorted(ml, ix) - 1
            if hi >= 0:
                gap = ix - int(ml[hi])
                if gap > 0:
                    cands.append((gap, int(ml[hi]), y, ix, y, 'H'))
    # Dikey tarama
    cols = np.where(edge.any(axis=0))[0]
    for x in cols:
        ic = np.where(edge[:, x])[0]
        mc = np.where(main_mask[:, x])[0]
        if len(mc) == 0:
            continue
        for iy in ic:
            vi = np.searchsorted(mc, iy + 1)
            if vi < len(mc):
                gap = int(mc[vi]) - iy
                if gap > 0:
                    cands.append((gap, x, iy, x, int(mc[vi]), 'V'))
            vi = np.searchsorted(mc, iy) - 1
            if vi >= 0:
                gap = iy - int(mc[vi])
                if gap > 0:
                    cands.append((gap, x, int(mc[vi]), x, iy, 'V'))
    cands.sort(key=lambda c: c[0])
    return cands


def _bridge_svg_elem(x1, y1, x2, y2, direction, bw, color, scale, min_svg=8.0):
    """Kopruyu temiz SVG elemanina donustur. H/V icin <rect>, capraz icin <line>."""
    sx1, sy1, sx2, sy2 = x1/scale, y1/scale, x2/scale, y2/scale
    if direction == 'H':
        length = abs(sx2 - sx1)
        if length < min_svg:
            mid = (sx1 + sx2) / 2
            sx1, sx2 = mid - min_svg/2, mid + min_svg/2
        return (f'<rect x="{min(sx1,sx2):.2f}" y="{(sy1+sy2)/2-bw/2:.2f}" '
                f'width="{abs(sx2-sx1):.2f}" height="{bw:.2f}" fill="{color}"/>')
    elif direction == 'V':
        length = abs(sy2 - sy1)
        if length < min_svg:
            mid = (sy1 + sy2) / 2
            sy1, sy2 = mid - min_svg/2, mid + min_svg/2
        return (f'<rect x="{(sx1+sx2)/2-bw/2:.2f}" y="{min(sy1,sy2):.2f}" '
                f'width="{bw:.2f}" height="{abs(sy2-sy1):.2f}" fill="{color}"/>')
    else:
        length = math.hypot(sx2-sx1, sy2-sy1)
        if length < min_svg and length > 0:
            dx, dy = (sx2-sx1)/length, (sy2-sy1)/length
            mx, my = (sx1+sx2)/2, (sy1+sy2)/2
            sx1,sy1 = mx-dx*min_svg/2, my-dy*min_svg/2
            sx2,sy2 = mx+dx*min_svg/2, my+dy*min_svg/2
        return (f'<line x1="{sx1:.2f}" y1="{sy1:.2f}" x2="{sx2:.2f}" y2="{sy2:.2f}" '
                f'stroke="{color}" stroke-width="{bw:.2f}" stroke-linecap="square"/>')


def find_and_bridge_islands(content, dark_threshold=110.0, scale=2.0,
                            bridge_width=2.0, color="#000000",
                            auto_multi=True, bridges_per=150.0, max_bridges=6,
                            min_bridge_svg=10.0):
    """
    Yuzen adalari eksen-hizali (yatay/dikey) koprulerle ana govdeye baglar.
    - Oncelik H/V: capraz slash yok; <rect> tab gorunumu.
    - min_bridge_svg: minimum kopru boyu (gorunur, yapisal).
    - Buyuk adalar auto_multi ile dagitilmis coklu kopru alir.
    """
    header, W, H = extract_svg_header(content)
    gray = render_to_gray(content, W, H, scale)
    black = gray < dark_threshold
    labels, n = ndimage.label(black, structure=np.ones((3, 3)))
    if n <= 1:
        return content, {"components_before": int(n), "components_after": int(n),
                         "bridges": 0, "bridge_segments": []}

    sizes = ndimage.sum(black, labels, range(1, n + 1))
    main_label = int(np.argmax(sizes)) + 1
    rest = (labels == main_label).copy()
    remaining = [l for l in range(1, n + 1) if l != main_label]

    svg_bridges, seg_report = [], []

    while remaining:
        edt, edt_inds = ndimage.distance_transform_edt(~rest, return_indices=True)
        # en yakin adayi sec
        best_l, best_d = None, np.inf
        for l in remaining:
            ys, xs = np.where(labels == l)
            d = edt[ys, xs].min()
            if d < best_d:
                best_d, best_l = d, l
        l = best_l
        isl = (labels == l)

        # boyut -> kopru sayisi
        ys, xs = np.where(isl)
        span_svg = max(xs.max()-xs.min(), ys.max()-ys.min()) / scale
        n_br = int(np.clip(round(span_svg/bridges_per), 1, max_bridges)) if auto_multi else 1

        # eksen-hizali adaylar (tercih)
        axis_cands = _axis_bridge_candidates(isl, rest)

        # capraz fallback
        edge = isl & ~ndimage.binary_erosion(isl)
        if not edge.any(): edge = isl
        ey, ex = np.where(edge)
        d_vals = edt[ey, ex]
        ord_ = np.argsort(d_vals)
        diag_cands = []
        for idx in ord_[:min(300, len(ord_))]:
            px, py = int(ex[idx]), int(ey[idx])
            tx, ty = int(edt_inds[1][py,px]), int(edt_inds[0][py,px])
            diag_cands.append((int(d_vals[idx]), px, py, tx, ty, 'D'))

        pool = axis_cands if axis_cands else diag_cands
        fallback = diag_cands if axis_cands else []

        min_gap_px = max(span_svg * scale / (n_br + 1), 15)
        chosen = []
        for src in [pool, fallback]:
            for c in src:
                px, py = c[1], c[2]
                if all((px-cx)**2+(py-cy)**2 >= min_gap_px**2 for cx,cy,*_ in chosen):
                    chosen.append(c)
                if len(chosen) >= n_br:
                    break
            if len(chosen) >= n_br:
                break

        for c in chosen:
            gap_px, x1c, y1c, x2c, y2c, d = c
            svg_bridges.append(_bridge_svg_elem(x1c,y1c,x2c,y2c,d,bridge_width,color,scale,min_bridge_svg))
            _stamp_line(rest, x1c, y1c, x2c, y2c)
            seg_report.append({"x1":round(x1c/scale,1),"y1":round(y1c/scale,1),
                                "x2":round(x2c/scale,1),"y2":round(y2c/scale,1),
                                "dir":d,"gap_svg":round(gap_px/scale,1)})
        rest = rest | isl
        remaining.remove(l)

    i = content.rfind("</svg>")
    bridged = content[:i] + "".join(svg_bridges) + content[i:]
    g2 = render_to_gray(bridged, W, H, scale)
    _, n2 = ndimage.label(g2 < dark_threshold, structure=np.ones((3,3)))

    return bridged, {"components_before":int(n),"components_after":int(n2),
                     "bridges":len(svg_bridges),"bridge_width":bridge_width,
                     "bridge_segments":seg_report}

def trim_png_bytes(data, threshold=10.0, padding=0):
    """
    Bir raster goruntunun etrafindaki bos (seffaf VEYA beyaza yakin) kenari kirp.
    threshold: 0-255 tolerans. padding: kirptiktan sonra birakılacak pt/piksel boslugu.
    Donen: PNG bytes.
    """
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    arr = np.asarray(im).astype(int)
    rgb, alpha = arr[..., :3], arr[..., 3]
    near_white = (rgb > (255 - threshold)).all(axis=2)
    content = (alpha > threshold) & ~near_white  # icerik = saydam degil VE beyaz degil
    ys, xs = np.where(content)
    out = io.BytesIO()
    if xs.size == 0:  # tamamen bos -> oldugu gibi dondur
        im.save(out, "PNG")
        return out.getvalue()
    W, H = im.size
    pad = int(padding)
    x0 = max(0, int(xs.min()) - pad)
    y0 = max(0, int(ys.min()) - pad)
    x1 = min(W - 1, int(xs.max()) + pad)
    y1 = min(H - 1, int(ys.max()) + pad)
    im.crop((x0, y0, x1 + 1, y1 + 1)).save(out, "PNG")
    return out.getvalue()


def render_svg_to_png(svg_text, scale=2.0, width=None, height=None,
                      bg=None, trim=False, threshold=10.0, padding=0):
    """
    SVG metnini PNG'ye cevir.
      - width/height verilirse onlar kullanilir; yoksa viewBox * scale.
      - bg: None/"" -> seffaf;  "white"/"#rrggbb" -> dolu zemin.
      - trim=True -> render sonrasi bos kenari kirp.
    Donen: PNG bytes.
    """
    kwargs = {}
    if width:
        kwargs["output_width"] = int(float(width))
    if height:
        kwargs["output_height"] = int(float(height))
    if not width and not height and scale:
        _, W, H = extract_svg_header(svg_text)
        kwargs["output_width"] = int(W * scale)
        kwargs["output_height"] = int(H * scale)
    png = cairosvg.svg2png(
        bytestring=svg_text.encode("utf-8"),
        background_color=(bg or None),
        **kwargs,
    )
    if trim:
        png = trim_png_bytes(png, threshold, padding)
    return png


# --------------------------------------------------------------------------- #
# COMPOSE: stencil yaziyi taban tasarimin uzerine yerlestir (tek SVG)
# --------------------------------------------------------------------------- #
def _viewbox(svg):
    m = re.search(r'viewBox="([^"]+)"', svg)
    if m:
        p = [float(x) for x in re.split(r"[ ,]+", m.group(1).strip())]
        if len(p) == 4:
            return p
    _, w, h = extract_svg_header(svg)
    return [0.0, 0.0, w, h]


def compose_text(base_svg, text_svg, cx, cy, width, rotate=0.0):
    """
    text_svg'yi base_svg uzerine, MERKEZI (cx,cy) base-birimi noktasina gelecek,
    genisligi 'width' base-birimi olacak ve 'rotate' derece donuk sekilde yerlestirir.
    Donen: birlesik SVG.
    """
    tx, ty, tw, th = _viewbox(text_svg)
    inner = re.search(r"<svg[^>]*>(.*)</svg>", text_svg, re.S)
    inner = inner.group(1) if inner else ""
    sc = (width / tw) if tw else 1.0
    g = (f'<g transform="translate({cx:.2f},{cy:.2f}) rotate({rotate:.3f}) '
         f'scale({sc:.5f}) translate({-tw/2 - tx:.2f},{-th/2 - ty:.2f})">{inner}</g>')
    i = base_svg.rfind("</svg>")
    return base_svg[:i] + g + base_svg[i:]


# --------------------------------------------------------------------------- #
# Yardimci: govde veya dosyadan SVG metni al
# --------------------------------------------------------------------------- #
async def read_svg(request: Request, file: UploadFile | None):
    if file is not None:
        return (await file.read()).decode("utf-8", errors="replace")
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "SVG girdisi yok. 'file' yukleyin veya govdeye SVG koyun.")
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Endpoint'ler
# --------------------------------------------------------------------------- #
import os
from fastapi.responses import FileResponse

_HERE = os.path.dirname(os.path.abspath(__file__))


@app.get("/")
def index():
    """Tarayici arayuzu (index.html)."""
    path = os.path.join(_HERE, "index.html")
    if os.path.exists(path):
        return FileResponse(path, media_type="text/html")
    return JSONResponse({"service": "auto-svg-clean", "ui": "index.html yok"})


@app.get("/proxy")
async def proxy_svg(url: str = Query(..., description="Uzak SVG URL — CORS bypass icin sunucu tarafi cekme")):
    """Verilen URL'deki SVG'yi sunucu tarafindan cekip dondurur."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "auto-svg-clean/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            content = r.read()
    except Exception as e:
        raise HTTPException(400, f"SVG cekilemedi: {e}")
    return Response(content=content, media_type="image/svg+xml")


@app.get("/info")
def info():
    return {
        "service": "auto-svg-clean",
        "version": "1.0.0",
        "status": "ok",
        "endpoints": {
            "GET /": "tarayici arayuzu",
            "GET /health": "saglik kontrolu",
            "POST /analyze": "silmeden ada tespiti (JSON rapor)",
            "POST /clean": "temizlenmis SVG dondurur (image/svg+xml)",
            "POST /bridge": "adalari silmez, koprulerle birlestirir (tek parca)",
            "POST /svg2png": "SVG -> PNG (opsiyonel trim)",
            "POST /trim": "raster goruntu bos kenarini kirpar",
            "POST /text": "stencil fontla tek parca kesilebilir yazi (SVG)",
            "POST /compose": "stencil yaziyi taban tasarima yerlestirir (tek SVG)",
            "POST /compose_multi": "birden cok yaziyi yerlestirir (tek SVG)",
            "GET /fonts": "kullanilabilir stencil fontlar",
        },
        "params": ["dark_threshold", "scale", "keep_larger_than", "bg", "trim", "threshold", "padding"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(request: Request,
                  file: UploadFile | None = File(default=None),
                  dark_threshold: float = Query(110.0),
                  scale: float = Query(2.0),
                  keep_larger_than: float | None = Query(default=None)):
    svg = await read_svg(request, file)
    try:
        _, report = find_and_remove_islands(
            svg, dark_threshold, scale, keep_larger_than, do_remove=False)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return JSONResponse(report)


@app.post("/clean")
async def clean(request: Request,
                file: UploadFile | None = File(default=None),
                dark_threshold: float = Query(110.0),
                scale: float = Query(2.0),
                keep_larger_than: float | None = Query(default=None)):
    svg = await read_svg(request, file)
    try:
        cleaned, report = find_and_remove_islands(
            svg, dark_threshold, scale, keep_larger_than, do_remove=True)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return Response(
        content=cleaned,
        media_type="image/svg+xml",
        headers={
            "X-Removed-Count": str(report["removed_count"]),
            "X-Components-After": str(report["components_after"]),
        },
    )


@app.post("/bridge")
async def bridge(request: Request,
                 file: UploadFile | None = File(default=None),
                 dark_threshold: float = Query(110.0),
                 scale: float = Query(2.0),
                 bridge_width: float = Query(2.0, description="kopru kalinligi (svg birimi)"),
                 color: str = Query("#000000", description="kopru rengi"),
                 auto_multi: bool = Query(True, description="buyuk adalara coklu kopru"),
                 bridges_per: float = Query(150.0, description="her ~N svg-birimi acikliga 1 kopru"),
                 max_bridges: int = Query(6, description="ada basina kopru ust siniri")):
    """Adalari silmez; en yakin noktadan ana govdeye baglar -> TEK PARCA SVG dondurur."""
    svg = await read_svg(request, file)
    try:
        bridged, report = find_and_bridge_islands(
            svg, dark_threshold, scale, bridge_width, color,
            auto_multi, bridges_per, max_bridges)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return Response(
        content=bridged,
        media_type="image/svg+xml",
        headers={
            "X-Bridge-Count": str(report["bridges"]),
            "X-Components-After": str(report["components_after"]),
        },
    )


@app.post("/svg2png")
async def svg2png(request: Request,
                  file: UploadFile | None = File(default=None),
                  scale: float = Query(2.0, description="viewBox * scale (width/height verilmezse)"),
                  width: float | None = Query(default=None),
                  height: float | None = Query(default=None),
                  bg: str = Query("none", description="none/transparent | white | #rrggbb"),
                  trim: bool = Query(True, description="render sonrasi bos kenari kirp"),
                  threshold: float = Query(10.0),
                  padding: int = Query(0)):
    """SVG -> PNG. Opsiyonel olarak bos kenari kirpar (trim)."""
    svg = await read_svg(request, file)
    bg_val = None if bg.lower() in ("none", "transparent", "") else bg
    try:
        png = render_svg_to_png(svg, scale, width, height, bg_val, trim, threshold, padding)
    except Exception as e:
        raise HTTPException(422, f"SVG->PNG hatasi: {e}")
    return Response(content=png, media_type="image/png")


@app.post("/trim")
async def trim(request: Request,
               file: UploadFile | None = File(default=None),
               threshold: float = Query(10.0, description="0-255 bos kenar toleransi"),
               padding: int = Query(0, description="kirptiktan sonra birakılacak bosluk")):
    """Yuklenen raster goruntunun (PNG/JPG) bos kenarini kirpar -> PNG dondurur."""
    if file is not None:
        data = await file.read()
    else:
        data = await request.body()
    if not data:
        raise HTTPException(400, "Goruntu girdisi yok. 'file' yukleyin veya govdeye koyun.")
    try:
        out = trim_png_bytes(data, threshold, padding)
    except Exception as e:
        raise HTTPException(422, f"Trim hatasi: {e}")
    return Response(content=out, media_type="image/png")


@app.get("/fonts")
def list_fonts():
    """Kullanilabilir stencil fontlar."""
    return {"fonts": list(STENCIL_FONTS.keys())}


@app.post("/text")
def text_endpoint(text: str = Query(..., description="yazilacak metin"),
                  font: str = Query("stardos", description="stencil font: stardos | saira | allerta"),
                  size: float = Query(120.0),
                  letter_spacing: float = Query(6.0),
                  mode: str = Query("knockout", description="knockout (tek parca, onerilen) | solid"),
                  color: str = Query("#000000"),
                  pad: float = Query(40.0, description="banner ic bosluk"),
                  radius: float = Query(0.0, description="banner kose yuvarlakligi"),
                  curve: float = Query(0.0, description="kavis derecesi (+yukari/-asagi)"),
                  vscale: float = Query(1.0, description="dikey olcek (1=normal, 2=iki kat uzun)"),
                  fit_width: float | None = Query(default=None, description="hedef genislik (verilirse size yerine bu kullanilir)")):
    """Stencil fontla yaziyi TEK PARCA kesilebilir SVG'ye cevirir."""
    try:
        svg = stencil_text_svg(text, font, size, letter_spacing, mode, color, pad, radius, curve, fit_width, vscale)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return Response(content=svg, media_type="image/svg+xml")


@app.post("/compose")
async def compose_endpoint(
        request: Request,
        file: UploadFile | None = File(default=None),
        text: str = Query(..., description="yerlestirilecek metin"),
        font: str = Query("stardos"),
        size: float = Query(120.0),
        letter_spacing: float = Query(6.0),
        mode: str = Query("knockout"),
        color: str = Query("#000000"),
        pad: float = Query(40.0),
        radius: float = Query(0.0),
        curve: float = Query(0.0, description="kavis derecesi (+yukari/-asagi)"),
        cx: float = Query(..., description="yazi merkezi X (taban svg birimi)"),
        cy: float = Query(..., description="yazi merkezi Y (taban svg birimi)"),
        width: float = Query(..., description="yazi hedef genisligi (taban svg birimi)"),
        rotate: float = Query(0.0, description="donme acisi (derece)"),
        vscale: float = Query(1.0, description="dikey olcek"),
        bridge: bool = Query(False, description="birlestirdikten sonra tek parca yap"),
        bridge_width: float = Query(10.0),
        dark_threshold: float = Query(110.0),
        scale: float = Query(2.0)):
    """Stencil yaziyi taban tasarima yerlestirip TEK SVG dondurur. Opsiyonel kopruleme."""
    base = await read_svg(request, file)
    try:
        tsvg = stencil_text_svg(text, font, size, letter_spacing, mode, color, pad, radius, curve, float(width), vscale)
        merged = compose_text(base, tsvg, cx, cy, width, rotate)
        if bridge:
            merged, _ = find_and_bridge_islands(merged, dark_threshold, scale, bridge_width, color)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return Response(content=merged, media_type="image/svg+xml")


@app.post("/selective")
async def selective_endpoint(
        request: Request,
        file: UploadFile | None = File(default=None),
        delete_indices: str = Form("[]"),
        bridge_indices: str = Form("[]"),
        dark_threshold: float = Form(110.0),
        scale: float = Form(2.0),
        bridge_width: float = Form(2.0)):
    """
    Ada bazında seçici silme + köprüleme.
    delete_indices / bridge_indices: JSON dizisi (analyze sırasıyla eşleşir).
    Geri kalan adalar olduğu gibi bırakılır.
    """
    svg = await read_svg(request, file)
    try:
        del_set = set(json.loads(delete_indices))
        br_set = set(json.loads(bridge_indices))
    except Exception:
        raise HTTPException(400, "delete_indices / bridge_indices geçerli JSON dizisi olmalı")

    try:
        header, W, H = extract_svg_header(svg)
    except ValueError as e:
        raise HTTPException(422, str(e))

    # ── Ada tespiti (analyze ile aynı sıra) ──
    gray = render_to_gray(svg, W, H, scale)
    black = gray < dark_threshold
    labels, n = ndimage.label(black, structure=np.ones((3, 3)))
    if n == 0:
        raise HTTPException(422, "Render'da koyu piksel yok.")
    sizes = ndimage.sum(black, labels, range(1, n + 1))
    main_label = int(np.argmax(sizes)) + 1
    main_mask = labels == main_label
    main_dil = ndimage.binary_dilation(main_mask, iterations=max(1, int(scale)))
    island_mask = black & ~main_mask

    dark_items = []
    g_blocks = re.findall(r'<g\b[^>]*transform="[^"]*"[^>]*>[\s\S]*?</g>', svg, re.IGNORECASE)
    covered_paths = set()
    for gblock in g_blocks:
        inner_path = re.search(r'<path\b[^>]*?/>', gblock, re.IGNORECASE)
        if not inner_path:
            continue
        ptag = inner_path.group(0)
        fm = re.search(r'fill="([^"]+)"', ptag)
        lum = hex_luminance(fm.group(1) if fm else None)
        if lum is not None and lum <= dark_threshold:
            dark_items.append((gblock, gblock))
            covered_paths.add(ptag)
    for ptag in re.findall(r"<path\b[^>]*?/>", svg, re.IGNORECASE):
        if ptag in covered_paths:
            continue
        fm = re.search(r'fill="([^"]+)"', ptag)
        lum = hex_luminance(fm.group(1) if fm else None)
        if lum is not None and lum <= dark_threshold:
            dark_items.append((ptag, ptag))

    # Adaları bul (y0'a göre sıralı → analyze ile aynı sıra)
    island_list = []
    for remove_str, render_frag in dark_items:
        single = f'{header}{render_frag}</svg>'
        try:
            pmask = render_to_gray(single, W, H, scale) < dark_threshold
        except Exception:
            continue
        if (pmask & main_dil).any():
            continue
        if not (pmask & island_mask).any():
            continue
        ys, _ = np.where(pmask)
        island_list.append({"remove_str": remove_str, "render_frag": render_frag,
                            "pmask": pmask, "y0": int(ys.min())})
    island_list.sort(key=lambda r: r["y0"])

    # ── Adım 1: Sil ──
    result = svg
    for i, isl in enumerate(island_list):
        if i in del_set:
            result = result.replace(isl["remove_str"], "", 1)

    # ── Adım 2: Seçili adaları köprüle ──
    br_valid = [i for i in sorted(br_set) if i < len(island_list) and i not in del_set]
    if br_valid:
        gray2 = render_to_gray(result, W, H, scale)
        black2 = gray2 < dark_threshold
        labels2, n2 = ndimage.label(black2, structure=np.ones((3, 3)))
        sizes2 = ndimage.sum(black2, labels2, range(1, n2 + 1)) if n2 > 0 else []
        if n2 > 0:
            main_label2 = int(np.argmax(sizes2)) + 1
            rest = (labels2 == main_label2).copy()
            svg_bridges = []
            for i in br_valid:
                orig_mask = island_list[i]["pmask"]
                overlap = labels2[orig_mask]
                overlap = overlap[overlap > 0]
                if len(overlap) == 0:
                    continue
                lbl = int(np.bincount(overlap).argmax())
                if lbl == 0 or lbl == main_label2:
                    continue
                isl_mask2 = labels2 == lbl
                edt, edt_inds = ndimage.distance_transform_edt(~rest, return_indices=True)
                axis_cands = _axis_bridge_candidates(isl_mask2, rest)
                edge = isl_mask2 & ~ndimage.binary_erosion(isl_mask2)
                if not edge.any():
                    edge = isl_mask2
                ey, ex = np.where(edge)
                d_vals = edt[ey, ex]
                ord_ = np.argsort(d_vals)
                diag_cands = []
                for idx in ord_[:min(300, len(ord_))]:
                    px, py = int(ex[idx]), int(ey[idx])
                    tx, ty = int(edt_inds[1][py, px]), int(edt_inds[0][py, px])
                    diag_cands.append((int(d_vals[idx]), px, py, tx, ty, 'D'))
                pool = axis_cands if axis_cands else diag_cands
                if not pool:
                    continue
                # Ada büyüklüğüne göre köprü sayısı
                ys2, xs2 = np.where(isl_mask2)
                span_svg = max(xs2.max() - xs2.min(), ys2.max() - ys2.min()) / scale
                n_br = int(np.clip(round(span_svg / 150.0), 1, 6))
                min_gap_px = max(span_svg * scale / (n_br + 1), 15)
                chosen = []
                for src in [pool, diag_cands if pool is axis_cands else []]:
                    for c in src:
                        px2, py2 = c[1], c[2]
                        if all((px2 - cx) ** 2 + (py2 - cy) ** 2 >= min_gap_px ** 2 for cx, cy, *_ in chosen):
                            chosen.append(c)
                        if len(chosen) >= n_br:
                            break
                    if len(chosen) >= n_br:
                        break
                for c in chosen:
                    _, x1c, y1c, x2c, y2c, d = c
                    svg_bridges.append(_bridge_svg_elem(x1c, y1c, x2c, y2c, d, bridge_width, "#000000", scale, 10.0))
                    _stamp_line(rest, x1c, y1c, x2c, y2c)
                rest = rest | isl_mask2
            if svg_bridges:
                i_close = result.rfind("</svg>")
                result = result[:i_close] + "".join(svg_bridges) + result[i_close:]

    return Response(content=result, media_type="image/svg+xml",
                    headers={"X-Processed": str(len(del_set) + len(br_set))})


@app.post("/compose_multi")
async def compose_multi(
        file: UploadFile = File(...),
        items: str = Form(..., description='JSON liste: [{text,font,mode,curve,cx,cy,width,rotate},...]'),
        bridge: bool = Form(False),
        bridge_width: float = Form(10.0),
        dark_threshold: float = Form(110.0),
        scale: float = Form(2.0)):
    """Birden cok stencil yaziyi taban tasarima yerlestirir -> tek SVG. Opsiyonel kopruleme."""
    base = (await file.read()).decode("utf-8", errors="replace")
    try:
        placements = json.loads(items)
        if not isinstance(placements, list):
            raise ValueError("items bir liste olmali")
    except Exception as e:
        raise HTTPException(400, f"items gecerli JSON degil: {e}")

    merged = base
    try:
        for it in placements:
            fw = float(it["width"]) if "width" in it else None
            tsvg = stencil_text_svg(
                it["text"], it.get("font", "stardos"), float(it.get("size", 120)),
                float(it.get("letter_spacing", 6)), it.get("mode", "solid"),
                it.get("color", "#000000"), float(it.get("pad", 40)),
                float(it.get("radius", 20)), float(it.get("curve", 0)), fw,
                float(it.get("vscale", 1.0)))
            merged = compose_text(merged, tsvg, float(it["cx"]), float(it["cy"]),
                                  float(it["width"]), float(it.get("rotate", 0)))
        if bridge:
            merged, _ = find_and_bridge_islands(merged, dark_threshold, scale, bridge_width)
    except (KeyError, ValueError) as e:
        raise HTTPException(422, f"yerlestirme hatasi: {e}")
    return Response(content=merged, media_type="image/svg+xml")
