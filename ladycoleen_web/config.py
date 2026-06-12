import os
import urllib.parse


def get_database_url():
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db   = os.environ.get("POSTGRES_DB", "farm_pos")
    user = os.environ.get("POSTGRES_USER", "farmstall")
    pw   = urllib.parse.quote_plus(os.environ.get("POSTGRES_PASSWORD", "FarmStall@2024!"))
    return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{db}"


class Config:
    SECRET_KEY        = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    JWT_SECRET_KEY    = os.environ.get("JWT_SECRET", "dev-jwt-change-me")
    SQLALCHEMY_DATABASE_URI = get_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 300}

    UPLOAD_PATH       = os.environ.get("UPLOAD_PATH", os.path.join(os.path.dirname(__file__), "uploads"))
    MAX_UPLOAD_BYTES  = 5 * 1024 * 1024  # 5 MB
    ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png"}
    ALLOWED_PROOF_EXT = {"jpg", "jpeg", "png", "pdf"}

    SMTP_HOST  = os.environ.get("SMTP_HOST", "")
    SMTP_PORT  = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER  = os.environ.get("SMTP_USER", "")
    SMTP_PASS  = os.environ.get("SMTP_PASS", "")
    FROM_EMAIL = os.environ.get("FROM_EMAIL", "")
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

    APP_ENV    = os.environ.get("APP_ENV", "development")
    PORT       = int(os.environ.get("PORT", "5001"))

    # PayFast
    PAYFAST_MERCHANT_ID  = os.environ.get("PAYFAST_MERCHANT_ID", "")
    PAYFAST_MERCHANT_KEY = os.environ.get("PAYFAST_MERCHANT_KEY", "")
    PAYFAST_PASSPHRASE   = os.environ.get("PAYFAST_PASSPHRASE", "")
    PAYFAST_SANDBOX      = os.environ.get("PAYFAST_SANDBOX", "true").lower() == "true"
    SITE_URL             = os.environ.get("SITE_URL", "https://ladycoleen.co.za")

    # Cake order minimum notice in days
    CAKE_MIN_NOTICE_DAYS = int(os.environ.get("CAKE_MIN_NOTICE_DAYS", "2"))
