# Farm POS — pinned appliance image.
#
# This is the MULTI-STORE build: source is COPY'd in at build time (not git-cloned),
# so a CI-built tag is an immutable, reproducible artifact that every appliance box
# pulls by version. Built and pushed by .github/workflows/release.yml on a git tag.
#
# NOTE: the original Lady Coleen box builds from its own server-side
# ~/farmpos-docker/pos/Dockerfile (git-clone). This file does NOT affect it —
# LC keeps deploying exactly as before. This image is for new stores only.
FROM python:3.11-slim

# System deps: libpq for psycopg, curl for the healthcheck. No git — source is COPY'd.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer — only re-runs when requirements.txt changes).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the POS application source. .dockerignore keeps the image lean and excludes
# the ladycoleen_web subfolder (the web shop is a separate image / optional per store).
COPY . .

RUN mkdir -p /app/logs

EXPOSE 5000

ENV FLASK_APP=app.py

# strong_migrate() runs on startup; the 120s gunicorn timeout covers a first-boot
# migration against an empty DB. Health uses the app's own /health route.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s \
    CMD curl -fsS http://localhost:5000/health || exit 1

CMD ["gunicorn", "--workers", "4", "--threads", "2", "--worker-class", "gthread", \
     "--bind", "0.0.0.0:5000", "--timeout", "120", "--keep-alive", "5", "app:app"]
