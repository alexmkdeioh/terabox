FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["gunicorn", "app:app", "--workers", "4", "--threads", "2", "--bind", "0.0.0.0:8080"]
