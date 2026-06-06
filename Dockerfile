FROM python:3.11-slim

# cairosvg icin cairo + Coolify healthcheck'i icin curl gerekli
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcairo2 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY index.html .

# Stencil fontlari build sirasinda indir (repoda binary tutmaya gerek yok)
RUN mkdir -p fonts && \
    curl -fsSL -o fonts/StardosStencil-Bold.ttf \
      https://raw.githubusercontent.com/google/fonts/main/ofl/stardosstencil/StardosStencil-Bold.ttf && \
    curl -fsSL -o fonts/SairaStencilOne-Regular.ttf \
      https://raw.githubusercontent.com/google/fonts/main/ofl/sairastencilone/SairaStencilOne-Regular.ttf && \
    curl -fsSL -o fonts/AllertaStencil-Regular.ttf \
      https://raw.githubusercontent.com/google/fonts/main/ofl/allertastencil/AllertaStencil-Regular.ttf

EXPOSE 5000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]
