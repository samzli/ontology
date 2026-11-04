# Onti — container image for a persistent, zero-external-service deployment.
# The SQLite database, uploads and generated reports live on the mounted
# /data volume, so accounts and sessions survive restarts and updates.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ONTI_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 8010

CMD ["python", "main.py"]
