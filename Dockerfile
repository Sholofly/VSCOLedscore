FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Amsterdam

# tzdata voor de juiste tijden in de logging; geen root voor de applicatie zelf.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py messages.py push.py settings.py state.py web.py ./
COPY sources/ ./sources/
COPY static/ ./static/
RUN mkdir -p /app/data /app/logs && chown appuser:appuser /app/data /app/logs

USER appuser

ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    DATA_DIR=/app/data \
    LOG_DIR=/app/logs

EXPOSE 8080

# /health is het enige adres zonder inlog; het meldt alleen dat de webomgeving draait.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['WEB_PORT'], timeout=3)"]

CMD ["python", "app.py"]
