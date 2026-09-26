"""SSRF protection for everything this service fetches.

The service downloads arbitrary URLs taken from search results, so it must never be usable to reach internal systems. Three layers:

1. The URL itself: only http/https, no credentials in the URL, only allowed ports, no internal-looking host names.
2. The host name is RESOLVED here and EVERY address it resolves to must be public. If any single address is private / loopback /
   link-local / cloud-metadata, the whole lookup is refused (this also defeats DNS answers that mix a public and a private address).
3. The connection is then made to the validated IP address (see fetcher.py), never re-resolving the name: a hostname that changes its
   DNS answer between the check and the connection ("DNS rebinding") cannot redirect the request. The same checks run again for
   every redirect hop.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

Resolver = Callable[[str, int], Awaitable[list[str]]]

MAX_URL_LENGTH = 2048

# Blocked on top of ipaddress' own is_global test (belt and braces, and explicit about what is meant).
_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this" network
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # carrier-grade NAT (also Alibaba's metadata service 100.100.100.200)
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, incl. the cloud metadata endpoint 169.254.169.254
        "172.16.0.0/12",  # private
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # documentation
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # documentation
        "203.0.113.0/24",  # documentation
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
        "255.255.255.255/32",  # broadcast
        "::/128",  # unspecified
        "::1/128",  # loopback
        "64:ff9b::/96",  # NAT64 (embeds an IPv4 address that could be internal)
        "100::/64",  # discard-only
        "2001::/32",  # Teredo
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 (embeds an IPv4 address)
        "fc00::/7",  # unique local (incl. AWS IPv6 metadata fd00:ec2::254)
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
    )
]

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata", "metadata.google.internal", "instance-data"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".intranet", ".lan", ".home", ".corp", ".home.arpa")


class UnsafeURLError(ValueError):
    """The URL (or an address it resolves to) must not be fetched. `code` is a short machine-readable reason."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class UrlParts:
    scheme: str
    host: str  # lower-case, IDNA (ascii) form, no brackets
    port: int
    path_query: str  # always starts with '/'
    host_header: str  # value for the Host header (port included only when non-default)


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is 127.0.0.1
    if not ip.is_global:
        return True
    return any(ip in net for net in _BLOCKED_NETWORKS if net.version == ip.version)


def validate_url(url: str, allowed_ports: frozenset[int] = frozenset({80, 443})) -> UrlParts:
    """Structural checks on the URL text. Does not touch the network. Raises UnsafeURLError."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("invalid_url", "empty URL")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError("invalid_url", "URL is too long")
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = parts.hostname
    except ValueError as exc:
        raise UnsafeURLError("invalid_url", "malformed URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UnsafeURLError("unsupported_scheme", f"scheme '{scheme or '(none)'}' is not allowed (http and https only)")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("invalid_url", "credentials in the URL are not allowed")
    if not hostname:
        raise UnsafeURLError("invalid_url", "URL has no host")
    host = hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)  # a literal IP: nothing to encode
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise UnsafeURLError("invalid_url", "invalid host name") from exc
        if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
            raise UnsafeURLError("blocked_host", "internal host names are not allowed")
    port = port or (443 if scheme == "https" else 80)
    if port not in allowed_ports:
        raise UnsafeURLError("blocked_port", f"port {port} is not allowed")
    default_port = 443 if scheme == "https" else 80
    host_for_header = f"[{host}]" if ":" in host else host
    path = parts.path or "/"
    path_query = path + (f"?{parts.query}" if parts.query else "")
    return UrlParts(scheme, host, port, path_query, host_for_header if port == default_port else f"{host_for_header}:{port}")


async def system_resolver(host: str, port: int) -> list[str]:
    """Resolve with the operating system (non-blocking). Returns unique address strings in the order given."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: dict[str, None] = {}
    for info in infos:
        seen[str(info[4][0]).split("%")[0]] = None  # drop an IPv6 scope id such as fe80::1%eth0
    return list(seen)


async def resolve_public_ips(host: str, port: int, resolver: Resolver | None = None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The public addresses `host` resolves to. Raises UnsafeURLError if it resolves to nothing, or to ANY non-public address."""
    resolver = resolver or system_resolver
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            raw = await resolver(host, port)
        except (OSError, UnicodeError) as exc:
            raise UnsafeURLError("dns_failure", "host name could not be resolved") from exc
        addresses = []
        for text in raw:
            try:
                addresses.append(ipaddress.ip_address(text))
            except ValueError as exc:
                raise UnsafeURLError("dns_failure", "resolver returned an invalid address") from exc
    if not addresses:
        raise UnsafeURLError("dns_failure", "host name resolved to no addresses")
    for address in addresses:
        if is_blocked_ip(address):
            raise UnsafeURLError("blocked_address", "the host resolves to a non-public address")
    return addresses
