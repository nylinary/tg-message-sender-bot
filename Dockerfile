FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tgsender/ ./tgsender/
COPY config.toml ./

# No web server here: this is a worker that polls Telegram and sends on a timer.
CMD ["python", "-m", "tgsender", "run"]
