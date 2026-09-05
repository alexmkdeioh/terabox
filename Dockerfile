FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure full read/write permissions for non-root runtimes like Hugging Face (UID 1000)
RUN chmod -R 777 /app

ENV PORT=7860
EXPOSE 7860 8080

CMD ["sh", "-c", "gunicorn app:app --workers 1 --threads 8 --worker-class gthread --timeout 120 --max-requests 500 --max-requests-jitter 50 --bind 0.0.0.0:${PORT:-7860}"]
