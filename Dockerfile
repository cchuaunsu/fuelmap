FROM python:3.12-slim

WORKDIR /app

# Install the engine (package data — station DB, known sources — included).
COPY pyproject.toml ./
COPY fie ./fie
RUN pip install --no-cache-dir ".[postgres]"

# The map frontend, served by the engine at "/".
COPY web ./web
ENV FIE_WEB_DIR=/app/web

# SQLite store lives here; mount a volume at /data on hosts that offer one,
# otherwise the store rebuilds itself on the first refresh.
ENV FIE_DB_PATH=/data/fie.db
RUN mkdir -p /data

EXPOSE 8000
# `fie serve` honors the PORT env var set by the platform.
CMD ["python", "-m", "fie.cli", "serve", "--host", "0.0.0.0"]
