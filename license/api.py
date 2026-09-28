"""HTTP surface of developer access: the Mac app's API (/v1/...), the Developer Console's API (/admin/api/...) and its page (/admin/).

These routes are deliberately outside the search API's key/rate-limit guard (a Mac app cannot carry a shared secret, and the console has
its own sign-in); they have their own limits, lock-outs and HTTPS requirement instead. None are listed in /docs.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from app.api.deps import client_ip
from app.license import crypto as C
from app.license.service import AuthError, LicenseService

log = logging.getLogger("jonah.license")
router = APIRouter(include_in_schema=False)

MAX_BODY = 16 * 1024
ADMIN_COOKIE = "jl_admin"
STATIC_DIR = Path(__file__).parent / "static"
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
PAGES = {"/admin": ("index.html", "text/html; charset=utf-8"), "/admin/": ("index.html", "text/html; charset=utf-8"), "/admin/console.js": ("console.js", "text/javascript; charset=utf-8"), "/admin/console.css": ("console.css", "text/css; charset=utf-8")}
_static_cache: dict[str, bytes] = {}


def _static(name: str) -> bytes | None:
    if name not in _static_cache:
        try:
            _static_cache[name] = (STATIC_DIR / name).read_bytes()
        except OSError:
            return None
    return _static_cache[name]


def auth_error_response(exc: AuthError) -> JSONResponse:
    body: dict = {"ok": False, "code": exc.code, "message": exc.message}
    headers = {}
    if exc.retry_after_seconds:
        body["retryAfterSeconds"] = exc.retry_after_seconds
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return JSONResponse(body, status_code=exc.status, headers=headers)


def _plain(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message}, status_code=status, headers=headers)


async def _json(request: Request) -> dict:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise AuthError("bad_request")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise AuthError("bad_request")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise AuthError("bad_request") from None
    return value if isinstance(value, dict) else {}


def _is_secure(request: Request, settings) -> bool:
    if request.url.scheme == "https":
        return True
    if settings.trust_proxy_headers:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[-1].strip().lower()
        return proto == "https"
    return False


def _require_https(settings) -> bool:
    return settings.is_production if settings.license_require_https is None else settings.license_require_https


def _service(request: Request) -> LicenseService | None:
    return getattr(request.app.state, "license", None)


def _not_configured(request: Request) -> JSONResponse:
    problem = getattr(request.app.state, "license_problem", "") or "developer access is not configured on this server"
    return _plain(503, "not_configured", f"Developer access is not available: {problem}")


def _same_site(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    return urlparse(origin).netloc == request.headers.get("host", "")


# ---------------------------------------------------------------------- the Mac app's API

@router.get("/v1/public-keys")
async def public_keys(request: Request):
    service = _service(request)
    if service is None:
        return _not_configured(request)
    return {"ok": True, **service.public_keys()}


@router.post("/v1/{group}/{name}")
async def client_api(group: str, name: str, request: Request):
    settings = request.app.state.settings
    service = _service(request)
    path = f"/v1/{group}/{name}"
    if path not in {"/v1/auth/challenge", "/v1/auth/login", "/v1/session/refresh", "/v1/session/logout"}:
        return _plain(404, "not_found", "No such endpoint.")
    if service is None:
        return _not_configured(request)
    if _require_https(settings) and not _is_secure(request, settings):
        return _plain(400, "https_required", "HTTPS is required.")
    ip = client_ip(request, settings.trust_proxy_headers, settings.trusted_proxy_hops)
    body = await _json(request)
    if path == "/v1/auth/challenge":
        return {"ok": True, **await run_in_threadpool(service.issue_challenge, ip)}
    if path == "/v1/auth/login":
        return await run_in_threadpool(lambda: service.login(username=body.get("username"), password=body.get("password"), challenge_id=body.get("challengeId"), device=body.get("device"), ip=ip))
    if path == "/v1/session/refresh":
        return await run_in_threadpool(lambda: service.refresh(session_id=body.get("sessionId"), refresh_token=body.get("refreshToken"), challenge_id=body.get("challengeId"), sig=body.get("sig"), ip=ip))
    return await run_in_threadpool(lambda: service.logout(session_id=body.get("sessionId"), refresh_token=body.get("refreshToken")))


# ---------------------------------------------------------------------- the Developer Console

def _admin_allowed(settings, ip: str) -> bool:
    allowed = [x.strip() for x in settings.license_admin_allowed_ips.split(",") if x.strip()]
    return settings.license_admin_enabled and (not allowed or ip in allowed)


def _admin_auth(request: Request, service: LicenseService, need_csrf: bool):
    """(admin, None) or (None, error response). Every change also needs the CSRF header, and a browser Origin (if sent) must be this site."""
    admin = service.admin_from_token(request.cookies.get(ADMIN_COOKIE))
    if not admin:
        return None, _plain(401, "session_ended", "Please sign in.")
    if need_csrf:
        if not _same_site(request):
            return None, _plain(403, "bad_request", "Cross-site request refused.")
        if not C.safe_equal(request.headers.get("x-csrf-token", ""), admin["csrf"]):
            return None, _plain(403, "bad_request", "Missing or wrong CSRF token.")
    return admin, None


@router.api_route("/admin", methods=["GET"])
@router.api_route("/admin/", methods=["GET"])
@router.api_route("/admin/console.js", methods=["GET"])
@router.api_route("/admin/console.css", methods=["GET"])
async def console_page(request: Request):
    settings = request.app.state.settings
    ip = client_ip(request, settings.trust_proxy_headers, settings.trusted_proxy_hops)
    if not _admin_allowed(settings, ip):
        return _plain(404, "not_found", "Not found.")
    if _require_https(settings) and not _is_secure(request, settings):
        return _plain(400, "https_required", "HTTPS is required.")
    name, media = PAGES[request.url.path]
    content = _static(name)
    if content is None:
        return _plain(404, "not_found", "Not found.")
    return Response(content, media_type=media, headers={"Content-Security-Policy": CSP})


@router.api_route("/admin/api/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def console_api(rest: str, request: Request):
    settings = request.app.state.settings
    ip = client_ip(request, settings.trust_proxy_headers, settings.trusted_proxy_hops)
    if not _admin_allowed(settings, ip):
        return _plain(404, "not_found", "Not found.")
    service = _service(request)
    if service is None:
        return _not_configured(request)
    secure = _is_secure(request, settings)
    if _require_https(settings) and not secure:
        return _plain(400, "https_required", "HTTPS is required.")
    method, p = request.method, "/" + rest
    cookie_flags = "; Secure" if secure else ""

    if p == "/login" and method == "POST":
        if not _same_site(request):
            return _plain(403, "bad_request", "Cross-site request refused.")
        body = await _json(request)
        r = await run_in_threadpool(lambda: service.admin_login(username=body.get("username"), password=body.get("password"), ip=ip))
        response = JSONResponse({"ok": True, "username": r["username"], "csrf": r["csrf"]})
        response.set_cookie(ADMIN_COOKIE, r["token"], max_age=service.config.admin_max_seconds, path="/admin", httponly=True, samesite="strict", secure=secure)
        return response

    write = method != "GET"
    admin, error = _admin_auth(request, service, write)
    if error is not None:
        return error
    actor = f"admin:{admin['username']}"
    body = await _json(request) if write else {}

    if p == "/logout" and method == "POST":
        service.admin_logout(request.cookies.get(ADMIN_COOKIE))
        response = JSONResponse({"ok": True})
        response.set_cookie(ADMIN_COOKIE, "", max_age=0, path="/admin", httponly=True, samesite="strict", secure=secure)
        return response
    if p == "/me" and method == "GET":
        return {"ok": True, "username": admin["username"], "csrf": admin["csrf"]}
    if p == "/overview" and method == "GET":
        return {"ok": True, **await run_in_threadpool(service.overview)}
    if p == "/audit" and method == "GET":
        return {"ok": True, "entries": service.list_audit(request.query_params.get("limit"))}
    if p == "/settings" and method == "PATCH":
        return {"ok": True, "settings": service.update_settings(body, actor, ip)}
    if p == "/password" and method == "POST":
        await run_in_threadpool(lambda: service.admin_change_password(admin["adminId"], body.get("current"), body.get("next"), admin["tokenHash"]))
        return {"ok": True}
    if p == "/accounts" and method == "POST":
        account = await run_in_threadpool(lambda: service.create_account(body.get("username"), body.get("password"), body.get("expiresAt"), body.get("note"), actor=actor, ip=ip))
        return JSONResponse({"ok": True, "account": account}, status_code=201)
    m = re.fullmatch(r"/accounts/(\d+)", p)
    if m:
        account_id = int(m.group(1))
        if method == "PATCH":
            return {"ok": True, "account": service.update_account(account_id, body, actor, ip)}
        if method == "DELETE":
            return {"ok": True, **service.delete_account(account_id, actor, ip)}
    m = re.fullmatch(r"/accounts/(\d+)/password", p)
    if m and method == "POST":
        return {"ok": True, "account": await run_in_threadpool(lambda: service.set_password(int(m.group(1)), body.get("password"), actor, ip))}
    m = re.fullmatch(r"/accounts/(\d+)/action", p)
    if m and method == "POST":
        return {"ok": True, "account": service.action(int(m.group(1)), body.get("action"), actor, ip)}
    return _plain(404, "not_found", "No such endpoint.")
