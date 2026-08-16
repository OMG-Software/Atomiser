from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth, invites, users, admin, videos
from app.config import Config, BASE_DIR
from app.db import init_db
from app.utils import generate_csrf


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Apply database migrations on startup.
    await init_db()
    yield


app = FastAPI(title="Atomiser", lifespan=lifespan)

# Templates
app_templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app_templates.env.globals["now"] = lambda: datetime.now(timezone.utc)
app_templates.env.globals["site_title_default"] = "Atomiser Site"


def _asset_version(path: str) -> str:
    """Return a cache-busting version string for a file under app/static.

    Based on the file's mtime so the URL changes whenever the asset changes —
    essential because nginx serves /static with `Cache-Control: immutable`
    and the browser would otherwise never re-request a same-URL asset.
    """
    try:
        full = BASE_DIR / "app" / "static" / path.strip("/")
        return str(int(full.stat().st_mtime))
    except OSError:
        return "1"


app_templates.env.globals["asset_version"] = _asset_version


def _timeago(value) -> str:
    """Render an ISO/SQLite timestamp as a short relative label (e.g. '3d ago')."""
    if not value:
        return ""
    try:
        s = str(value)
        # SQLite CURRENT_TIMESTAMP uses "YYYY-MM-DD HH:MM:SS"; ISO uses "T".
        dt = datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def _initials(value) -> str:
    """Derive up to two uppercase initials from a display name or email."""
    if not value:
        return "?"
    name = str(value).strip()
    # Use the part before '@' if it looks like an email with no real name.
    if "@" in name and " " not in name:
        name = name.split("@", 1)[0]
    parts = [p for p in name.replace("_", " ").replace(".", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _duration(seconds) -> str:
    """Format a number of seconds as H:MM:SS or M:SS."""
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _avatar_color(value) -> str:
    """Pick a stable HSL hue from a name so avatars vary but stay consistent."""
    name = str(value or "?")
    h = sum(ord(c) for c in name) % 360
    return f"hsl({h}, 62%, 52%)"


app_templates.env.filters["timeago"] = _timeago
app_templates.env.filters["initials"] = _initials
app_templates.env.filters["duration"] = _duration
app_templates.env.filters["avatar_color"] = _avatar_color

# Compatibility shim: Starlette >= 1.3 changed TemplateResponse signature to
# (request, name, context, ...). Our routes use the old (name, context, ...)
# form, so we transparently rewrite calls that do not start with a Request.
_orig_template_response = app_templates.TemplateResponse


def _compat_template_response(*args, **kwargs):
    if args and isinstance(args[0], Request):
        return _orig_template_response(*args, **kwargs)
    # Old signature: TemplateResponse(name, context, [status_code], ...)
    if len(args) >= 2:
        name, context = args[0], args[1]
        request = context.get("request") if isinstance(context, dict) else None
        if isinstance(context, dict):
            context = {k: v for k, v in context.items() if k != "request"}
        if len(args) >= 3 and "status_code" not in kwargs:
            kwargs["status_code"] = args[2]
        return _orig_template_response(request, name, context, **kwargs)
    return _orig_template_response(*args, **kwargs)


app_templates.TemplateResponse = _compat_template_response

# Wire templates into modules so routes can render.
auth.templates = app_templates
invites.templates = app_templates
users.templates = app_templates
admin.templates = app_templates
videos.templates = app_templates

# Static files (development convenience; nginx serves these in production).
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

# Routers
app.include_router(auth.router)
app.include_router(invites.router)
app.include_router(users.router)
app.include_router(admin.router)
app.include_router(videos.router)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def csrf_cookie_middleware(request: Request, call_next):
    """Ensure a double-submit CSRF cookie exists for every request."""
    existing = request.cookies.get(auth.CSRF_COOKIE)
    if existing:
        request.state.csrf_token = existing
    else:
        request.state.csrf_token = generate_csrf()
    response = await call_next(request)
    if existing is None:
        response.set_cookie(
            key=auth.CSRF_COOKIE,
            value=request.state.csrf_token,
            httponly=False,
            secure=Config.PRODUCTION,
            samesite="Lax",
            path="/",
        )
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    csp = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "media-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    response.headers["Content-Security-Policy"] = csp
    if Config.PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ---------------------------------------------------------------------------
# Global error pages
# ---------------------------------------------------------------------------

@app.exception_handler(auth.LoginRequiredException)
async def login_required_handler(request: Request, exc: auth.LoginRequiredException):
    # Set the Location header directly. Some Starlette versions percent-encode
    # the "?" in RedirectResponse's quoting, which turns "?next=/" into part of
    # the path and 404s the login page for logged-out visitors.
    resp = RedirectResponse(url="/auth/login", status_code=303)
    resp.headers["location"] = f"/auth/login?next={auth._safe_next_url(exc.next_url)}"
    return resp


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return app_templates.TemplateResponse("errors/404.html", {"request": request, "detail": exc.detail}, status_code=404)
    if exc.status_code == 403:
        return app_templates.TemplateResponse("errors/403.html", {"request": request, "detail": exc.detail}, status_code=403)
    return app_templates.TemplateResponse("errors/generic.html", {"request": request, "detail": exc.detail, "status": exc.status_code}, status_code=exc.status_code)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
