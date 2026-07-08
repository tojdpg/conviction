FROM python:3.12-slim

LABEL org.opencontainers.image.title="conviction" \
      org.opencontainers.image.description="Self-hosted portfolio decision cockpit" \
      org.opencontainers.image.source="https://github.com/tojdpg/conviction"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORTFOLIO_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py marketdata.py index.html config.example.json ./

# Mutable state (config.json, prices.db, cache) goes to the mounted volume
VOLUME /data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/config', timeout=3).read()" || exit 1

CMD ["python", "main.py"]
