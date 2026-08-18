import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(key: str, default=None, required: bool = False):
    value = os.getenv(key, default)
    if required and value is None:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


def _env_bool(key: str, default: str) -> bool:
    return _env(key, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: str) -> int:
    return int(_env(key, default))


class Config:
    # Reserved for future cryptographic signing (e.g., signed session cookies).
    # Currently session and CSRF tokens are generated with secrets.token_urlsafe
    # and validated server-side, so this key is not required.
    SECRET_KEY: str = _env("SECRET_KEY", "")
    ENV: str = _env("ENV", "development")
    DATABASE_PATH: Path = Path(_env("DATABASE_PATH", "./data/atomiser.db")).resolve()
    UPLOAD_DIR: Path = Path(_env("UPLOAD_DIR", "./uploads")).resolve()
    MAX_UPLOAD_MB: int = int(_env("MAX_UPLOAD_MB", "500"))
    HOST: str = _env("HOST", "127.0.0.1")
    PORT: int = int(_env("PORT", "8000"))
    PRODUCTION: bool = ENV == "production"
    WEBAUTHN_RP_ID: str = _env("WEBAUTHN_RP_ID", "localhost")
    TRANSCODE_VIDEOS: bool = _env_bool("TRANSCODE_VIDEOS", "true")

    # Absolute base URL used when a link has to be built outside a request
    # (password reset and invite emails). Falls back to the request's own base
    # URL when a request is available.
    SITE_URL: str = _env("SITE_URL", "").rstrip("/")

    # --- Transcoding worker -------------------------------------------------
    # Number of videos transcoded at once. 1 keeps ffmpeg from starving the web
    # worker; raise it only on a box with cores to spare.
    TRANSCODE_CONCURRENCY: int = max(1, _env_int("TRANSCODE_CONCURRENCY", "1"))
    # A job is retried this many times before it is marked failed for good.
    TRANSCODE_MAX_ATTEMPTS: int = max(1, _env_int("TRANSCODE_MAX_ATTEMPTS", "3"))
    # Delete the original upload once at least one rendition is ready. The raw
    # file is roughly as large as all renditions combined, so keeping it doubles
    # storage per video. Set true only if you want the pristine original back.
    KEEP_RAW_UPLOADS: bool = _env_bool("KEEP_RAW_UPLOADS", "false")
    # Set false to run the worker as a separate process (see docs/DEPLOYMENT.md)
    # instead of inside the web app.
    RUN_TRANSCODE_WORKER: bool = _env_bool("RUN_TRANSCODE_WORKER", "true")

    # --- Rate limiting ------------------------------------------------------
    # Portable, in-app throttling so protection does not depend on the nginx
    # config being present. Window is in seconds.
    RATE_LIMIT_ENABLED: bool = _env_bool("RATE_LIMIT_ENABLED", "true")
    RATE_LIMIT_WINDOW_SECONDS: int = max(1, _env_int("RATE_LIMIT_WINDOW_SECONDS", "900"))
    # Failed logins per email before the account is locked for the window.
    LOGIN_MAX_FAILURES_PER_EMAIL: int = max(1, _env_int("LOGIN_MAX_FAILURES_PER_EMAIL", "8"))
    # Failed logins per source IP before that IP is throttled.
    LOGIN_MAX_FAILURES_PER_IP: int = max(1, _env_int("LOGIN_MAX_FAILURES_PER_IP", "25"))
    LOCKOUT_MINUTES: int = max(1, _env_int("LOCKOUT_MINUTES", "15"))

    # --- Outbound email (optional) -----------------------------------------
    # With SMTP_HOST unset the app behaves exactly as before: invites are
    # copy-paste links and password recovery tells the user to contact an admin.
    SMTP_HOST: str = _env("SMTP_HOST", "")
    SMTP_PORT: int = _env_int("SMTP_PORT", "587")
    SMTP_USERNAME: str = _env("SMTP_USERNAME", "")
    SMTP_PASSWORD: str = _env("SMTP_PASSWORD", "")
    SMTP_STARTTLS: bool = _env_bool("SMTP_STARTTLS", "true")
    SMTP_SSL: bool = _env_bool("SMTP_SSL", "false")
    SMTP_FROM: str = _env("SMTP_FROM", "")
    SMTP_TIMEOUT: int = max(1, _env_int("SMTP_TIMEOUT", "20"))
    # Password reset links are short-lived by design.
    PASSWORD_RESET_TTL_MINUTES: int = max(1, _env_int("PASSWORD_RESET_TTL_MINUTES", "60"))

    # --- Email notifications ------------------------------------------------
    # Master switch for "a new video was posted" emails. Members are subscribed
    # by default and can opt out on their profile or from any notification's
    # unsubscribe link; turning this off suppresses the whole feature.
    NOTIFY_NEW_VIDEOS: bool = _env_bool("NOTIFY_NEW_VIDEOS", "true")
    # Run the email queue worker inside the web app. Set false to run it as a
    # separate process (see docs/DEPLOYMENT.md).
    RUN_EMAIL_WORKER: bool = _env_bool("RUN_EMAIL_WORKER", "true")
    # Messages sent per pass over one SMTP connection. A fan-out to a large
    # membership is throttled by this, which keeps a burst of uploads from
    # tripping the provider's rate limit.
    EMAIL_BATCH_SIZE: int = max(1, _env_int("EMAIL_BATCH_SIZE", "20"))
    # Seconds between passes when the queue is empty.
    EMAIL_POLL_SECONDS: int = max(1, _env_int("EMAIL_POLL_SECONDS", "10"))
    # Delivery attempts before a queued message is abandoned.
    EMAIL_MAX_ATTEMPTS: int = max(1, _env_int("EMAIL_MAX_ATTEMPTS", "3"))
    # Base delay before retrying a failed send. Doubles each attempt.
    EMAIL_RETRY_MINUTES: int = max(1, _env_int("EMAIL_RETRY_MINUTES", "5"))
    # Days a delivered/abandoned row is kept for the admin view before pruning.
    EMAIL_RETENTION_DAYS: int = max(1, _env_int("EMAIL_RETENTION_DAYS", "30"))

    @classmethod
    def mail_enabled(cls) -> bool:
        """True when enough SMTP settings are present to attempt delivery."""
        return bool(cls.SMTP_HOST and cls.SMTP_FROM)

    @classmethod
    def ensure_dirs(cls):
        cls.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cls.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        cls.UPLOAD_DIR.joinpath("raw").mkdir(exist_ok=True)
        cls.UPLOAD_DIR.joinpath("videos").mkdir(exist_ok=True)


Config.ensure_dirs()
