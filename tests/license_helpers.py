"""Helpers for the developer-access tests: a stand-in for one Mac, and a service with a fake clock."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.license import crypto as C
from app.license.db import Db
from app.license.service import AuthError, LicenseConfig, LicenseService


@dataclass
class Device:
    label: str
    pub: str
    hw: str
    _key: Ed25519PrivateKey

    def sign(self, message: str) -> str:
        return C.b64u(self._key.sign(message.encode()))


def make_device(label: str = "Test Mac") -> Device:
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return Device(label, C.b64u(pub), C.sha256_hex(C.random_hex(16)), key)


class Clock:
    def __init__(self, start: int = 1_800_000_000) -> None:
        self.t = start
        self._lock = threading.Lock()

    def __call__(self) -> int:
        return self.t


def new_service(**config) -> tuple[LicenseService, Clock, C.SigningKey]:
    clock = Clock()
    key = C.new_signing_key()
    service = LicenseService(Db(":memory:"), LicenseConfig(**config), key, now=clock)
    return service, clock, key


def login_as(service: LicenseService, dev: Device, username: str, password: str, ip: str = "10.0.0.1") -> dict:
    ch = service.issue_challenge(ip)
    sig = dev.sign(f"login|{ch['nonce']}|{username.strip().lower()}|{dev.hw}")
    return service.login(username=username, password=password, challenge_id=ch["challengeId"], device={"pub": dev.pub, "hw": dev.hw, "label": dev.label, "sig": sig}, ip=ip)


def refresh_as(service: LicenseService, dev: Device, session: dict, ip: str = "10.0.0.1") -> dict:
    ch = service.issue_challenge(ip)
    return service.refresh(session_id=session["sessionId"], refresh_token=session["refreshToken"], challenge_id=ch["challengeId"], sig=dev.sign(f"refresh|{ch['nonce']}|{session['sessionId']}"), ip=ip)


def code_of(fn, *args, **kwargs) -> str:
    """'OK' if the call succeeded, otherwise the AuthError's code."""
    try:
        fn(*args, **kwargs)
        return "OK"
    except AuthError as exc:
        return exc.code
