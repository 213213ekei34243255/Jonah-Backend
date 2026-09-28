"""Developer access for the Mac app: sign-in, one device per account, short-lived signed tokens, and the Developer Console (/admin/).

`build_license(settings)` never raises: a licence problem must not take the search API down. If developer access cannot start (no
persistent storage in production, a bad signing key, ...) the licence endpoints answer 503 with the reason and everything else keeps working.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from app.config import Settings, secret_value
from app.license import crypto as C
from app.license.db import Db
from app.license.service import LicenseConfig, LicenseService
from app.logging_config import log_event

log = logging.getLogger("jonah.license")
DISK_MOUNT = Path("/var/data")  # where a Render persistent disk is usually mounted


def _render_without_disk(path: Path) -> bool:
    """On Render the container's own filesystem is wiped on every restart and deploy; only a mounted Disk survives. A folder that is not
    on a mount point there looks perfectly writable and would silently lose every account, so it is not trusted. (Elsewhere, e.g. a VPS,
    the ordinary filesystem is durable and nothing is checked.)"""
    if not os.environ.get("RENDER"):
        return False
    for candidate in (path, *path.parents):
        if candidate.parent == candidate:
            return True  # reached the filesystem root without meeting a mount point
        if os.path.ismount(candidate):
            return False
    return True


def resolve_storage(settings: Settings) -> tuple[Path | None, bool]:
    """(directory, persistent). Accounts, bans and device bindings live here, so on a host whose disk is wiped on every deploy they would
    vanish: production therefore refuses to start licensing without a real directory, unless temporary storage is explicitly allowed."""
    explicit = settings.license_data_dir.strip()
    if explicit:
        path: Path | None = Path(explicit)
    elif DISK_MOUNT.is_dir() and os.access(DISK_MOUNT, os.W_OK):
        path = DISK_MOUNT / "license"
    elif settings.environment.strip().lower() == "development":
        return Path("data") / "license", True  # a developer running it on their own machine (tests and other environments never write here)
    else:
        path = None
    if path is not None:
        if _render_without_disk(path):
            return (path, False) if (settings.license_allow_temporary_storage or not settings.is_production) else (None, False)
        return path, True
    if settings.license_allow_temporary_storage:
        return Path(tempfile.gettempdir()) / "jonah-license", False
    return None, False


def build_license(settings: Settings) -> tuple[LicenseService | None, str]:
    """(service, "") when running, otherwise (None, why not)."""
    if not settings.license_enabled:
        return None, "LICENSE_ENABLED is false"
    data_dir, persistent = resolve_storage(settings)
    if data_dir is None:
        return None, "no persistent disk found: attach a disk and set LICENSE_DATA_DIR to a folder on it (accounts must survive restarts), or set LICENSE_ALLOW_TEMPORARY_STORAGE=true to only try it out"
    env_key = secret_value(settings.license_signing_key)
    if not env_key and not persistent:
        return None, "temporary storage needs LICENSE_SIGNING_KEY (a key that changes on every restart would lock every user out)"
    try:
        key = C.load_signing_key(env_key, data_dir if persistent else None)
        db = Db(data_dir / "license.db")
        config = LicenseConfig(
            token_ttl_seconds=settings.license_token_ttl_seconds, session_idle_seconds=settings.license_session_idle_seconds,
            max_sessions_per_account=settings.license_max_sessions_per_account, admin_username=settings.license_admin_username.strip(),
            admin_password=secret_value(settings.license_admin_password), admin_reset_password=settings.license_admin_reset_password,
            storage_persistent=persistent, unlimited_rate_limit_per_minute=settings.license_unlimited_rate_limit_per_minute,
        )
        service = LicenseService(db, config, key)
        seeds = secret_value(settings.license_seed_accounts)
        if seeds:
            try:
                parsed = json.loads(seeds)
            except ValueError:
                raise ValueError('LICENSE_SEED_ACCOUNTS must be JSON like [{"username":"a","password":"b"}]') from None
            created = service.seed_accounts(parsed if isinstance(parsed, list) else [])
            if created:
                log_event(log, "license_seeded", accounts=created, note="remove LICENSE_SEED_ACCOUNTS from the environment now")
        service.ensure_bootstrap_admin(lambda text: print(text, flush=True))
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        log_event(log, "license_unavailable", level=logging.ERROR, error=type(exc).__name__, detail=str(exc)[:200])
        return None, f"{type(exc).__name__}: {str(exc)[:160]}"
    if not persistent:
        log_event(log, "license_temporary_storage", level=logging.WARNING, detail="accounts are lost whenever this server restarts")
    try:
        if int(os.environ.get("WEB_CONCURRENCY", "1")) > 1:
            log_event(log, "license_multiple_workers", level=logging.WARNING, detail="developer access assumes ONE worker (WEB_CONCURRENCY=1)")
    except ValueError:
        pass
    log_event(log, "license_ready", key_id=key.kid, public_key=key.spki, data_dir=str(data_dir), persistent=persistent)
    return service, ""
