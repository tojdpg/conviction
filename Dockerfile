FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py marketdata.py index.html config.example.json ./

# Mutable state (config.json, prices.db, cache) goes to the mounted volume
ENV PORTFOLIO_DATA_DIR=/data
VOLUME /data

EXPOSE 8080
CMD ["python", "main.py"]
