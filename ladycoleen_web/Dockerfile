FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
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

# Clone monorepo and use ladycoleen_web subfolder - same repo as POS
RUN git clone https://github.com/quintuszgeyser/FarmStallPos.git /tmp/repo && \
    cp -r /tmp/repo/ladycoleen_web/. /app/ && \
    rm -rf /tmp/repo

RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/logs /app/uploads/cake_images /app/uploads/payment_proofs

EXPOSE 5001

ENV APP_ENV=production
ENV FLASK_APP=app.py

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s \
    CMD curl -f http://localhost:5001/health || exit 1

CMD ["gunicorn", "--workers", "2", "--threads", "2", "--worker-class", "gthread", \
     "--bind", "0.0.0.0:5001", "--timeout", "120", "--keep-alive", "5", "app:app"]
