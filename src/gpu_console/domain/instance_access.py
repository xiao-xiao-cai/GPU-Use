"""Validate connection addresses shared by the platform and administrator."""

import re
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit

DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE | re.ASCII)


def valid_host(host: str) -> bool:
    if not host or "%" in host:
        return False
    try:
        ip_address(host)
        return True
    except ValueError:
        pass
    try:
        dns = host.removesuffix(".").encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = dns.split(".")
    return (
        len(dns) <= 253 and all(DNS_LABEL.fullmatch(label) for label in labels)
        # A browser can reinterpret a numeric final label as an IPv4 address.
        and not re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", labels[-1], re.IGNORECASE)
    )


def safe_instance_url(value: object, *, platform: bool = False) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if "\\" in value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if not platform and ("?" in value or "#" in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if (
        parts.scheme not in {"http", "https"} or not valid_host(host)
        or "@" in parts.netloc or parts.netloc.endswith(":") or port == 0
        or (parts.netloc.startswith("[") and (
            ":" not in host or not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parts.netloc)
        ))
    ):
        return None
    # Provider URLs may include a login token. Persist only the application address.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
