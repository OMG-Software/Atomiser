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
    TRANSCODE_VIDEOS: bool = _env("TRANSCODE_VIDEOS", "true").lower() in ("1", "true", "yes")

    @classmethod
    def ensure_dirs(cls):
        cls.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cls.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        cls.UPLOAD_DIR.joinpath("raw").mkdir(exist_ok=True)
        cls.UPLOAD_DIR.joinpath("videos").mkdir(exist_ok=True)


Config.ensure_dirs()
