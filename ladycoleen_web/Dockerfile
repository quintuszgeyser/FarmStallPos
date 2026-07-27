FROM python:3.11-slim

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    git \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf-xlib-2.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements.txt is pre-fetched from GitHub by deploy.sh before each build.
# This layer is cached — pip only re-runs when requirements change.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Always-fresh: --no-cache in deploy.sh ensures this layer is never reused.
RUN git clone --depth=1 https://github.com/quintuszgeyser/FarmStallPos.git /tmp/repo && \
    cp -r /tmp/repo/ladycoleen_web/. /app/ && \
    rm -rf /tmp/repo

RUN mkdir -p /app/logs /app/uploads/cake_images /app/uploads/payment_proofs

EXPOSE 5001

ENV APP_ENV=production
ENV FLASK_APP=app.py

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s \
    CMD curl -f http://localhost:5001/health || exit 1

CMD ["gunicorn", "--workers", "2", "--threads", "2", "--worker-class", "gthread", \
     "--bind", "0.0.0.0:5001", "--timeout", "120", "--keep-alive", "5", "app:app"]
