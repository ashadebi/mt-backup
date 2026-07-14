FROM python:3.12-slim

WORKDIR /app

# Install cron + tini for proper signal handling
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends cron tini tzdata && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

# Create required directories
RUN mkdir -p /app/data /app/backups /app/logs && \
    chmod +x /app/scripts/backup.py

ENV PYTHONUNBUFFERED=1
ENV MT_DATA_DIR=/app/data
ENV MT_BACKUP_DIR=/app/backups
ENV TZ=Asia/Jakarta

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
