"""Cryptography for developer access: scrypt for passwords, Ed25519 for tokens and device proofs.

Formats are the same as the original Node licence server (`scrypt$N$r$p$salt$hash`, `header.payload.signature` tokens, SPKI-DER keys in
base64url), so a database or a signed token from one works with the other and the Mac app cannot tell them apart.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 32768, 8, 1, 64
SCRYPT_MAXMEM = 128 * 1024 * 1024
MAX_PASSWORD_LENGTH = 256
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def from_b64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return b64u(secrets.token_bytes(nbytes))


def random_hex(nbytes: int = 16) -> str:
    return secrets.token_hex(nbytes)


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a).encode(), str(b).encode())


# ------------------------------------------------------------------ passwords

# scrypt is CPU-heavy: allow only a few at once so a flood of login attempts cannot starve the search endpoints that share this process.
_scrypt_gate = threading.BoundedSemaphore(3)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    with _scrypt_gate:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=length)


def hash_password(password: str) -> str:
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("password too long")
    salt = secrets.token_bytes(16)
    key = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64u(salt)}${b64u(key)}"


def verify_password(password: str, stored: str) -> bool:
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    parts = str(stored or "").split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = from_b64u(parts[4]), from_b64u(parts[5])
    except (ValueError, binascii.Error):
        return False
    if n > 1 << 17 or n < 2 or r < 1 or p < 1 or len(expected) < 16:
        return False
    try:
        actual = _scrypt(password, salt, n, r, p, len(expected))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


_dummy_hash: str | None = None
_dummy_lock = threading.Lock()


def verify_against_dummy(password: str) -> bool:
    """A real check against a random password's hash: 'no such user' costs the same time as 'wrong password'."""
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            _dummy_hash = hash_password(random_hex(12))
    verify_password(password, _dummy_hash)
    return False


# ------------------------------------------------------------------ signing key + tokens

@dataclass(frozen=True)
class SigningKey:
    private_key: Ed25519PrivateKey
    kid: str
    spki: str  # base64url of the SPKI DER public key: what the Mac app and other backends pin

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()


def _key_info(private_key: Ed25519PrivateKey) -> SigningKey:
    der = private_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return SigningKey(private_key, "k" + sha256_hex(der)[:10], b64u(der))


def new_signing_key() -> SigningKey:
    return _key_info(Ed25519PrivateKey.generate())


def signing_key_pem(key: SigningKey) -> str:
    return key.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def load_signing_key(pem_or_base64: str, data_dir: Path | None) -> SigningKey:
    """The private key comes from LICENSE_SIGNING_KEY (a PEM, or its base64: the right place on a host whose disk can be wiped) or,
    when a data directory is given, from a file that is created once. Raises ValueError when neither is available."""
    text = (pem_or_base64 or "").strip()
    if text and "BEGIN" not in text:
        text = base64.b64decode(text).decode("utf-8")
    if text:
        key = serialization.load_pem_private_key(text.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("LICENSE_SIGNING_KEY must be an Ed25519 private key")
        return _key_info(key)
    if data_dir is None:
        raise ValueError("no signing key: set LICENSE_SIGNING_KEY")
    file = data_dir / "signing-key.pem"
    if file.exists():
        return _key_info(serialization.load_pem_private_key(file.read_bytes(), password=None))  # type: ignore[arg-type]
    data_dir.mkdir(parents=True, exist_ok=True)
    key = new_signing_key()
    file.write_text(signing_key_pem(key))
    try:
        os.chmod(file, 0o600)
    except OSError:
        pass
    return key


def sign_token(claims: dict, key: SigningKey) -> str:
    head = b64u(json.dumps({"alg": "EdDSA", "typ": "JLT", "kid": key.kid}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    sig = key.private_key.sign(f"{head}.{body}".encode("ascii"))
    return f"{head}.{body}.{b64u(sig)}"


def verify_token(token: str, keys: dict[str, Ed25519PublicKey], *, now: float | None = None, issuer: str | None = None, audience: str | None = None, skew: int = 30) -> tuple[dict | None, str]:
    """(claims, "ok") for a good token, else (None, reason). Only EdDSA (never "none", never a shared secret)."""
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None, "malformed"
    try:
        head = json.loads(from_b64u(parts[0]))
        claims = json.loads(from_b64u(parts[1]))
        signature = from_b64u(parts[2])
    except (ValueError, binascii.Error):
        return None, "malformed"
    if not isinstance(head, dict) or not isinstance(claims, dict):
        return None, "malformed"
    if head.get("alg") != "EdDSA" or head.get("typ") != "JLT":
        return None, "bad_alg"
    key = keys.get(head.get("kid"))
    if key is None:
        return None, "unknown_kid"
    try:
        key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"))
    except (InvalidSignature, ValueError):
        return None, "bad_signature"
    if issuer and claims.get("iss") != issuer:
        return None, "bad_issuer"
    if audience and claims.get("aud") != audience:
        return None, "bad_audience"
    exp, iat = claims.get("exp"), claims.get("iat")
    if isinstance(exp, bool) or isinstance(iat, bool) or not isinstance(exp, (int, float)) or not isinstance(iat, (int, float)):
        return None, "malformed"
    t = time.time() if now is None else now
    if exp + skew < t:
        return None, "expired"
    if iat - skew > t:
        return None, "not_yet_valid"
    return claims, "ok"


# ------------------------------------------------------------------ device keys

def parse_device_public_key(pub: str) -> tuple[Ed25519PublicKey, str] | None:
    """(public key, device id) for a well-formed Ed25519 SPKI key in base64url; otherwise None."""
    try:
        der = from_b64u(pub)
        if len(der) != 44 or not der.startswith(ED25519_SPKI_PREFIX):
            return None
        key = serialization.load_der_public_key(der)
        if not isinstance(key, Ed25519PublicKey):
            return None
        return key, sha256_hex(der)[:32]
    except (ValueError, binascii.Error, TypeError):
        return None


def verify_device_signature(pub: str, message: str, sig: str) -> bool:
    parsed = parse_device_public_key(pub)
    if parsed is None:
        return False
    try:
        parsed[0].verify(from_b64u(sig), message.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, binascii.Error):
        return False
