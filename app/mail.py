"""Optional outbound email.

Atomiser works with no mail server at all: invites are copy-paste links and a
forgotten password is handled by an admin reset. That stays the default. When
SMTP_HOST and SMTP_FROM are set, the same flows can additionally deliver a
message, which is what makes self-service password recovery possible.

Delivery uses stdlib smtplib on a threadpool rather than an async SMTP client,
so this adds no new dependency. Sending is always best-effort: a failure is
logged and reported to the caller, never raised into a request handler, because
an invite link that could not be emailed is still a perfectly good invite link.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from starlette.concurrency import run_in_threadpool

from app.config import Config

logger = logging.getLogger(__name__)


def mail_enabled() -> bool:
    return Config.mail_enabled()


def email_links_available() -> bool:
    """True when a link can be built from configuration alone.

    Anything that goes into an email needs this. It is separate from
    mail_enabled() because a working SMTP server is not sufficient: without
    SITE_URL there is no trustworthy hostname to put in the message.
    """
    return bool(Config.SITE_URL)


def email_link(path: str) -> str:
    """Absolute URL for a link that will be emailed. Requires SITE_URL.

    Deliberately does NOT fall back to the request's host. Starlette derives
    request.base_url from the Host header, which the client controls and nginx
    passes through verbatim (the shipped config sets `proxy_set_header Host
    $host`, and a single 443 server block is the default for any Host). Building
    a password reset link that way is a host-header injection: an attacker POSTs
    to /auth/forgot with the victim's address and `Host: evil.test`, and the app
    emails the victim a genuine reset token pointing at the attacker's domain.
    Following it hands over the token, and with it the account.

    Callers must check email_links_available() and degrade rather than send.
    """
    if not Config.SITE_URL:
        raise RuntimeError("SITE_URL is required to build links for outbound email")
    return f"{Config.SITE_URL}/{path.lstrip('/')}"


def display_url(request, path: str) -> str:
    """Absolute URL for showing on screen to the user who made the request.

    Falls back to the request host, which is safe here only because the value is
    rendered back to the person who supplied it. Never use this for email -
    use email_link().
    """
    path = "/" + path.lstrip("/")
    if Config.SITE_URL:
        return f"{Config.SITE_URL}{path}"
    return str(request.base_url).rstrip("/") + path


def _build_message(
    to_address: str,
    subject: str,
    body: str,
    site_title: str,
    unsubscribe_url: str = None,
) -> EmailMessage:
    message = EmailMessage()
    from_name, from_email = parseaddr(Config.SMTP_FROM)
    message["From"] = formataddr((from_name or site_title, from_email or Config.SMTP_FROM))
    message["To"] = to_address
    message["Subject"] = subject

    if unsubscribe_url:
        # RFC 8058. Mail clients that understand these render a native
        # "unsubscribe" control, and honouring it is what keeps bulk mail out of
        # spam folders. List-Unsubscribe-Post tells the client it may POST
        # rather than follow the link, which matters because our GET only shows
        # a confirmation page.
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    message.set_content(body)
    return message


def _connect_sync():
    """Open an authenticated SMTP connection. Caller must close it."""
    context = ssl.create_default_context()

    if Config.SMTP_SSL:
        client = smtplib.SMTP_SSL(
            Config.SMTP_HOST, Config.SMTP_PORT, timeout=Config.SMTP_TIMEOUT, context=context
        )
    else:
        client = smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=Config.SMTP_TIMEOUT)

    client.ehlo()
    if Config.SMTP_STARTTLS and not Config.SMTP_SSL:
        client.starttls(context=context)
        client.ehlo()
    if Config.SMTP_USERNAME:
        client.login(Config.SMTP_USERNAME, Config.SMTP_PASSWORD)
    return client


def _send_sync(message: EmailMessage) -> None:
    """Blocking SMTP send. Runs on a threadpool via send_mail()."""
    with _connect_sync() as client:
        client.send_message(message)


def _send_batch_sync(messages: list) -> list:
    """Send several messages over one connection.

    Returns a list of (index, error) for the ones that failed. A per-recipient
    rejection does not abort the batch; a connection-level failure marks every
    remaining message as failed so they are all retried together.
    """
    failures = []
    try:
        client = _connect_sync()
    except Exception as exc:  # noqa: BLE001 - reported per message, not raised
        return [(i, f"SMTP connection failed: {exc}") for i in range(len(messages))]

    try:
        with client:
            for index, message in enumerate(messages):
                try:
                    client.send_message(message)
                except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                        smtplib.SMTPDataError) as exc:
                    # A bad address should not stop the rest of the batch.
                    failures.append((index, str(exc)))
                except Exception as exc:  # noqa: BLE001 - connection is suspect now
                    failures.append((index, str(exc)))
                    for remaining in range(index + 1, len(messages)):
                        failures.append((remaining, "connection lost earlier in batch"))
                    break
    except Exception as exc:  # noqa: BLE001
        sent = {i for i, _ in failures}
        failures.extend((i, str(exc)) for i in range(len(messages)) if i not in sent)

    return failures


async def send_mail(
    to_address: str,
    subject: str,
    body: str,
    site_title: str = "Atomiser",
    unsubscribe_url: str = None,
) -> bool:
    """Send one message. Returns False (and logs) rather than raising."""
    if not mail_enabled():
        logger.debug("Mail not configured; skipping message to %s", to_address)
        return False
    if not to_address:
        return False

    try:
        message = _build_message(to_address, subject, body, site_title, unsubscribe_url)
        await run_in_threadpool(_send_sync, message)
        logger.info("Sent %r to %s", subject, to_address)
        return True
    except Exception:  # noqa: BLE001 - delivery failure must not break the request
        logger.exception("Failed to send %r to %s", subject, to_address)
        return False


async def send_batch(items: list, site_title: str = "Atomiser") -> dict:
    """Send a batch of messages over a single SMTP connection.

    ``items`` are dicts with ``to_address``, ``subject``, ``body`` and an
    optional ``unsubscribe_url``. Returns ``{index: error}`` for the failures,
    so the caller can retry or abandon each message individually. Opening one
    connection per recipient would be far slower and gets an account throttled
    by most providers.
    """
    if not mail_enabled() or not items:
        return {index: "mail is not configured" for index in range(len(items))}

    messages = [
        _build_message(
            item["to_address"], item["subject"], item["body"], site_title,
            item.get("unsubscribe_url"),
        )
        for item in items
    ]
    failures = await run_in_threadpool(_send_batch_sync, messages)
    if failures:
        logger.warning("%d of %d messages in batch failed", len(failures), len(messages))
    return dict(failures)


# ---------------------------------------------------------------------------
# Message bodies
# ---------------------------------------------------------------------------

async def send_invite(to_address: str, invite_url: str, site_title: str, inviter: str, expires_hours: int) -> bool:
    body = (
        f"{inviter} has invited you to join {site_title}.\n\n"
        f"Create your account here:\n{invite_url}\n\n"
        f"This link expires in {expires_hours} hour{'s' if expires_hours != 1 else ''} "
        f"and can only be used a limited number of times.\n\n"
        f"If you were not expecting this invitation you can ignore this message.\n"
    )
    return await send_mail(to_address, f"You have been invited to {site_title}", body, site_title)


async def send_password_reset(to_address: str, reset_url: str, site_title: str, ttl_minutes: int) -> bool:
    body = (
        f"Someone asked to reset the password for your {site_title} account.\n\n"
        f"Set a new password here:\n{reset_url}\n\n"
        f"This link expires in {ttl_minutes} minutes and can only be used once.\n\n"
        f"If you did not request this, you can ignore this message — your password "
        f"has not been changed.\n"
    )
    return await send_mail(to_address, f"Reset your {site_title} password", body, site_title)
