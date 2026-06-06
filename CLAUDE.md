# CLAUDE.md — auto-svg-edit

Bu dosya Claude Code'un bu repoda çalışırken başvurması için hazırlanmıştır.

## Proje Hakkında

Lazer kesim ve print-on-demand (POD) baskı için tek renkli SVG tasarımlarını işleyen **FastAPI mikroservisi**.

İki temel problem çözer:
1. **Floating island temizleme** — SVG içinde ana gövdeye bağlı olmayan yüzen koyu şekilleri tespit eder; siler ya da köprülerle ana gövdeye bağlar.
2. **Stencil metin üretimi** — Tek parça, lazer kesilebilir SVG metni oluşturur; eğrilik, dikey ölçek, köşe yuvarlatma destekler.

## Dosya Yapısı

```
auto-svg-edit/
├── app.py          # Tüm backend mantığı + FastAPI endpoint'leri (~820 satır)
├── index.html      # Tek sayfa web UI (~47KB, vanilla JS)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── fonts/          # Build sırasında Google Fonts'tan indirilir (repoda yok)
```

Proje kasıtlı olarak iki dosyadan oluşur: `app.py` ve `index.html`. Yeni modüller/dosyalar ekleme; tek dosya yapısı korunmalıdır.

## Komutlar

```bash
# Lokal geliştirme (Docker ile)
docker compose up --build

# Direkt çalıştırma (fontlar /fonts klasöründe varsa)
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 5000 --reload

# Sağlık kontrolü
curl http://localhost:5000/health
```

Port: **5000**. Coolify'a deploy edilir; healthcheck `/health` endpoint'ini kullanır.

## Mimari

**Backend:** `app.py` — tek dosya, saf Python. FastAPI + Uvicorn.

**Veri akışı:**
```
SVG girdi (multipart file VEYA raw body)
  → extract_svg_header() — viewBox/width/height çıkar
  → render_to_gray()     — CairoSVG ile PNG'ye çevir → NumPy grayscale array
  → ndimage.label()      — bağlantılı bileşen analizi
  → path eşleştirme      — her koyu <path> tek tek render edilip main_mask ile kesişim kontrolü
  → çıktı SVG            — silinen ya da köprülenen path'lerle birlikte
```

**UI:** `index.html` — vanilla JS, 4 sekme, dark monospace tema. API'ye `fetch()` ile bağlanır. Hiç framework yok.

## Temel Fonksiyonlar (`app.py`)

### Ada Tespiti ve Temizleme

```python
find_and_remove_islands(content, dark_threshold=110.0, scale=2.0,
                        keep_larger_than=None, do_remove=True)
```
- `dark_threshold`: 0-255 parlaklık; altındakiler koyu sayılır (varsayılan 110)
- `scale`: render çözünürlüğü çarpanı (yüksek = daha hassas ama yavaş)
- `keep_larger_than`: pt² cinsinden bu alanı aşan adaları koru
- `do_remove=False`: sadece rapor üret, silme (analyze endpoint'i kullanır)
- Döner: `(cleaned_svg, report_dict)`

**Algoritma özeti:**
1. SVG'deki koyu `<path>` etiketlerini renk filtresiyle bul
2. Tüm SVG'yi render et → bağlantılı bileşen analizi
3. En büyük bileşen = ana gövde (`main_label`)
4. `binary_dilation` ile ana gövde genişletilir → dokunma kontrolü
5. Her path tek tek render edilip `main_dil` ile kesişiyorsa korunur, `island_mask` ile kesişiyorsa silinir

### Köprüleme

```python
find_and_bridge_islands(content, dark_threshold=110.0, scale=2.0,
                        bridge_width=2.0, color="#000000",
                        auto_multi=True, bridges_per=150.0, max_bridges=6,
                        min_bridge_svg=10.0)
```
- Ada silmez; en yakın noktadan ana gövdeye eksen-hizalı (`<rect>`) veya çapraz (`<line>`) köprü ekler
- `auto_multi=True`: büyük adalar için boyuta göre birden fazla köprü dağıtır
- `bridges_per`: her ~N SVG birimi açıklık için 1 köprü
- Öncelik sırası: yatay H → dikey V → çapraz D

### Stencil Metin

```python
stencil_text_svg(text, font="stardos", size=120.0, letter_spacing=6.0,
                 mode="knockout", color="#000000", pad=40.0, radius=0.0,
                 curve=0.0, fit_width=None, vscale=1.0)
```
- `mode="knockout"`: banner + harfler tek `evenodd` path → harfler delik (lazer için)
- `mode="solid"`: dolu harfler
- `curve`: +yukarı / -aşağı kavis (derece cinsinden)
- `fit_width`: verilirse `size` yerine hedef genişliğe sığdırır
- `vscale`: dikey ölçek, genişliğe dokunmaz
- Font'lar `FontTools` ile parse edilir; her harf `SVGPathPen + TransformPen` ile çizilir

## API Endpoint'leri

| Method | Path | Açıklama |
|--------|------|----------|
| GET | `/` | Web UI (index.html) |
| GET | `/health` | `{"status": "ok"}` |
| GET | `/info` | Tüm endpoint'lerin listesi |
| GET | `/fonts` | Kullanılabilir stencil fontlar |
| POST | `/analyze` | Ada tespiti (sadece JSON rapor, silme yok) |
| POST | `/clean` | Ada silme → temizlenmiş SVG döner |
| POST | `/bridge` | Ada köprüleme → tek parça SVG döner |
| POST | `/svg2png` | SVG → PNG (opsiyonel trim) |
| POST | `/trim` | PNG/JPG boş kenar kırpma |
| POST | `/text` | Stencil metin SVG üretimi |
| POST | `/compose` | Tek metin katmanını taban SVG'ye bindirme |
| POST | `/compose_multi` | Çok katmanlı metin bindirme (multipart JSON) |

### Girdi formatı (SVG endpoint'leri)

İki yöntemden biri:
- `multipart/form-data` — `file` alanı (`.svg`)
- Ham istek gövdesi — raw SVG metni (`Content-Type: image/svg+xml` veya `text/plain`)

### Önemli response header'ları

- `/clean` → `X-Removed-Count`, `X-Components-After`
- `/bridge` → `X-Bridge-Count`, `X-Components-After`

## Stencil Fontlar

Fontlar **build sırasında** `Dockerfile` tarafından Google Fonts'tan indirilir. Repoda binary olarak tutulmaz.

| Anahtar | Dosya |
|---------|-------|
| `stardos` | StardosStencil-Bold.ttf |
| `saira` | SairaStencilOne-Regular.ttf |
| `allerta` | AllertaStencil-Regular.ttf |

Font ekleme: `STENCIL_FONTS` sözlüğüne ekle + Dockerfile'a curl satırı ekle.

## Bağımlılıklar

```
fastapi==0.115.6       # Web framework
uvicorn[standard]      # ASGI server
cairosvg==2.7.1        # SVG → PNG render (libcairo2 sistem kütüphanesi gerektirir)
pillow==11.1.0         # Görüntü manipülasyonu
numpy==2.2.1           # Pixel analizi
scipy==1.15.1          # ndimage.label (bağlantılı bileşen)
fonttools==4.55.3      # TTF parse + SVGPathPen
python-multipart       # FastAPI dosya upload desteği
```

`libcairo2` sistem paketi Dockerfile'da `apt-get` ile kurulur — geliştirme ortamında da gereklidir.

## Önemli Detaylar

- **`_font_cache`**: Fontlar ilk yüklemede önbelleğe alınır, her istekte yeniden yüklenmez.
- **`extract_svg_header`**: `width`/`height` yoksa `viewBox`'tan boyut çıkarır; her ikisi de yoksa hata fırlatır.
- **`hex_luminance`**: Renk parlaklığını hesaplar; `None` dönerse path koyu sayılmaz.
- **`_stamp_line`**: Köprüleme sırasında `rest` maskesine piksel çizer → sonraki ada için güncel ana gövde.
- **`compose_text`**: Metin SVG'yi `<g transform="...">` içine sarar ve taban SVG'nin `</svg>` kapanış etiketinden önce ekler.
- **`trim_png_bytes`**: Şeffaf VE beyaza yakın pikselleri boş sayar (her ikisi de); sadece alfa kanalına bakmaz.

## Geliştirme Notları

- Yeni endpoint eklerken `GET /info` endpoint'indeki `endpoints` sözlüğünü güncelle.
- `dark_threshold` varsayılanı 110 — çok yüksek tutulursa gri tonlardaki istenilen şekiller de ada sayılabilir.
- `scale=2.0` çoğu SVG için yeterli; çok küçük detaylarda `scale=3.0` denenebilir (bellek artar).
- `/compose_multi` endpoint'i `multipart/form-data` + JSON string (`items` form alanı) kullanır — diğer SVG endpoint'lerinden farklı.
