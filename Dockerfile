FROM python:3.11-slim

WORKDIR /app

# Install Node.js, Chromium and required headless browser libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    chromium \
    chromium-driver \
    fonts-liberation \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["gunicorn", "app:app", "--workers", "4", "--threads", "2", "--timeout", "120", "--bind", "0.0.0.0:8080"]
