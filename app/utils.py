import hmac
import hashlib
import secrets
import string
import uuid
from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def generate_token(length: int = 32) -> str:
    """Return a URL-safe random token."""
    return secrets.token_urlsafe(length)


def hash_token(token: str) -> str:
    """Return a SHA-256 hex digest of a token for storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_csrf() -> str:
    return secrets.token_urlsafe(24)


def verify_csrf(token: str, expected: str) -> bool:
    if not token or not expected:
        return False
    return hmac.compare_digest(token, expected)


def new_video_uuid() -> str:
    return str(uuid.uuid4())


# Alphabet for generated passwords: letters + digits only, so the output
# always satisfies password_policy_error() (letters and a digit present) and
# stays easy to read/type. Length 16 gives comfortable brute-force resistance.
_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int = 16) -> str:
    """Return a random password that satisfies the app's password policy.

    Guaranteed to contain at least one letter and one digit so it always passes
    password_policy_error().
    """
    chars = [secrets.choice(_PASSWORD_ALPHABET) for _ in range(length)]
    # Ensure the policy's letter+digit requirement is met even if the random
    # draw happened to be all-one-class (a ~5% chance of no digit at length 16).
    if not any(c.isdigit() for c in chars):
        chars[0] = secrets.choice(string.digits)
    if not any(c.isalpha() for c in chars):
        chars[1] = secrets.choice(string.ascii_letters)
    return "".join(chars)
