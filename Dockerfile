FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=pillow-heif -r requirements.txt
COPY app.py ./
USER 3009:3009
EXPOSE 8080
# One process is essential: the semaphore serializes model calls in this process.
# Spare threads keep health checks responsive during document processing.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--worker-class", "gthread", "--threads", "4", "--timeout", "900", "--graceful-timeout", "20", "--worker-tmp-dir", "/tmp", "app:app"]
