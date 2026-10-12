FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIEW_HOST=0.0.0.0 \
    VIEW_PORT=80 \
    VIEW_STATE_DIR=/app/data \
    VIEW_DATABASE_PATH=/app/data/backup_state.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends rsync openssh-client sshpass ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py config.py runner.py scan_sync_backup.py ./
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('VIEW_PORT', '80') + '/', timeout=3)"

CMD ["python", "app.py"]
