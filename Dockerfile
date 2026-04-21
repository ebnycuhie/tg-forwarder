FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_forwarder.py .

# Railway expects a worker process, not a web server.
# No PORT binding needed — this is a long-running background worker.
CMD ["python", "telegram_forwarder.py"]
