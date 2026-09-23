"""URL canonicalisation and SSRF defence.

Everything the marketplace fetches passes through :func:`validate_url` first,
and every redirect hop is re-validated (defence against DNS rebinding and
redirect-to-internal attacks).

The audit is recommend-only and read-only; this module is what keeps a
user-supplied string from turning the auditing agent into a confused deputy
against internal infrastructure.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit, quote, unquote

# Test-only escape hatch. The fixture site in tests/ is served on 127.0.0.1,
# which the guard below is correct to refuse. Rather than weaken the guard or
# skip the end-to-end tests, private address space can be allowed by an
# explicit opt-in that is never set in normal operation.
#
# This is deliberately awkward to enable: it requires setting an environment
# variable whose name says what it does, and every ValidatedUrl produced under
# it carries a loud note that travels into the audit report.
ALLOW_PRIVATE_ENV = "GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES"


def _private_allowed(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get(ALLOW_PRIVATE_ENV, "").strip().lower() in ("1", "true", "yes")


ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = {80, 443, 8000, 8080, 8443}
DEFAULT_PORTS = {"http": 80, "https": 443}

# NAT64 (RFC 6052) well-known prefix. On an IPv6-only network a DNS64 resolver
# answers AAAA queries by embedding the real IPv4 in this range, so a genuinely
# public host presents as a "reserved" address unless it is unwrapped first.
NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")

# Hostnames that resolve to, or stand in for, the local machine.
BLOCKED_HOST_LITERALS = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "broadcasthost", "0.0.0.0", "[::]", "::", "::1",
}

# Suffixes reserved for private / internal namespaces (RFC 6762, RFC 8375,
# RFC 2606, and the cloud-metadata convention).
BLOCKED_HOST_SUFFIXES = (
    ".localhost", ".local", ".internal", ".intranet", ".private",
    ".corp", ".home", ".lan", ".home.arpa", ".test", ".example", ".invalid",
)

# Cloud instance-metadata endpoints. These are link-local and already caught by
# the IP checks, but naming them produces a far clearer diagnostic.
METADATA_HOSTS = {
    "metadata.google.internal", "metadata.goog",
    "169.254.169.254", "fd00:ec2::254", "100.100.100.200",
}

# A hostname label per RFC 1123: alphanumeric plus hyphen, not hyphen-initial
# or hyphen-final, 1-63 octets.
_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")

# Decimal ("2130706433"), octal ("0177.0.0.1") and hex ("0x7f000001") forms of
# an IPv4 address that naive parsers miss but the resolver honours.
_ALL_DIGITS_RE = re.compile(r"^\d+$")
_HEX_IP_RE = re.compile(r"^0[xX][0-9a-fA-F]+$")
_OCTAL_DOTTED_RE = re.compile(r"^0\d+(\.0*\d+){0,3}$")


class UrlRejected(ValueError):
    """The URL is unsafe or out of scope. Never retried, never followed."""

    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass
class ValidatedUrl:
    """A URL that has cleared every gate, together with the evidence."""

    url: str
    scheme: str
    host: str
    port: int
    path: str
    registrable_domain: str
    resolved_ips: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        if self.port == DEFAULT_PORTS.get(self.scheme):
            return f"{self.scheme}://{self.host}"
        return f"{self.scheme}://{self.host}:{self.port}"

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "origin": self.origin,
            "registrable_domain": self.registrable_domain,
            "resolved_ips": self.resolved_ips,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# Address classification
# --------------------------------------------------------------------------

def classify_ip(raw: str) -> tuple[bool, str]:
    """Return ``(is_public, reason)`` for an IP literal.

    Anything that is not unambiguously a public unicast address is refused.
    IPv4-mapped, 6to4 and NAT64 IPv6 addresses are unwrapped first so that, for
    example, ``::ffff:127.0.0.1`` cannot smuggle loopback past the check, while
    a DNS64-synthesised address for a real public host is not misread as
    reserved.
    """
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False, f"not a valid IP address: {raw!r}"

    # Unwrap tunnelled IPv4 before classifying.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif getattr(ip, "teredo", None):
            ip = ip.teredo[1]
        elif ip in NAT64_WELL_KNOWN:
            # RFC 6052: for the /96 form the last 32 bits are the IPv4 address.
            # Unwrapping keeps the guard correct both ways -- a wrapped loopback
            # or cloud-metadata address is still refused, but now on its real
            # classification rather than incidentally as "reserved".
            ip = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)

    checks = (
        (ip.is_loopback, "loopback address"),
        (ip.is_private, "private (RFC 1918 / ULA) address"),
        (ip.is_link_local, "link-local address (includes cloud metadata)"),
        (ip.is_multicast, "multicast address"),
        (ip.is_reserved, "reserved address"),
        (ip.is_unspecified, "unspecified address"),
    )
    for failed, reason in checks:
        if failed:
            return False, reason

    # Carrier-grade NAT: not flagged private by ipaddress, still not public.
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return False, "carrier-grade NAT address (RFC 6598)"

    return True, "public unicast address"


def _reject_obfuscated_ipv4(host: str) -> None:
    """Refuse decimal, octal and hexadecimal spellings of an IPv4 address."""
    if _ALL_DIGITS_RE.match(host):
        raise UrlRejected(
            f"host {host!r} is a bare integer, which resolvers expand to an "
            "IPv4 address; use dotted-quad or a hostname",
            "obfuscated_ip_decimal",
        )
    if _HEX_IP_RE.match(host):
        raise UrlRejected(
            f"host {host!r} is a hexadecimal IPv4 literal", "obfuscated_ip_hex"
        )
    if _OCTAL_DOTTED_RE.match(host):
        raise UrlRejected(
            f"host {host!r} uses octal octets, which resolve differently than "
            "they read",
            "obfuscated_ip_octal",
        )


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 without a Public Suffix List dependency.

    Handles the common two-part public suffixes (``co.uk``, ``com.au``, ...)
    that a naive last-two-labels rule gets wrong. Used only for scope
    confinement, so erring toward a *narrower* scope is the safe direction.
    """
    host = host.lower().strip(".")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    labels = host.split(".")
    if len(labels) <= 2:
        return host

    two_part_suffixes = {
        "co", "com", "net", "org", "gov", "edu", "ac", "mil", "or", "ne",
        "go", "in", "nom", "sch", "gob", "asn", "id", "plc", "ltd", "me",
    }
    if labels[-2] in two_part_suffixes and len(labels[-1]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_scope(host: str, scope_domain: str) -> bool:
    """True when *host* is inside the audit scope (registrable domain + subdomains)."""
    host = host.lower().strip(".")
    scope_domain = scope_domain.lower().strip(".")
    return host == scope_domain or host.endswith("." + scope_domain)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def normalise_input(raw: str) -> str:
    """Accept what a human would type: bare domains, missing scheme, stray spaces."""
    candidate = (raw or "").strip()
    if not candidate:
        raise UrlRejected("empty URL", "empty")
    # Strip wrapping punctuation people paste along with a link.
    candidate = candidate.strip("<>\"'` \t\r\n")
    if "://" not in candidate:
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        else:
            candidate = "https://" + candidate
    return candidate


def validate_url(
    raw: str,
    *,
    scope_domain: str | None = None,
    resolve_dns: bool = True,
    allow_ports: Iterable[int] | None = None,
    allow_private: bool | None = None,
) -> ValidatedUrl:
    """Canonicalise and clear a URL for fetching, or raise :class:`UrlRejected`.

    When *scope_domain* is given the host must fall inside it; this is how the
    audit stays confined to the site it was pointed at, including across
    redirects.
    """
    notes: list[str] = []
    allowed_ports = set(allow_ports) if allow_ports is not None else ALLOWED_PORTS
    permit_private = _private_allowed(allow_private)
    if permit_private:
        notes.append(
            "PRIVATE ADDRESS SPACE ALLOWED: this run was started with "
            f"{ALLOW_PRIVATE_ENV} set. Intended for the test fixture only."
        )
        allowed_ports = allowed_ports | set(range(1024, 65536))

    candidate = normalise_input(raw)
    if len(candidate) > 2048:
        raise UrlRejected("URL exceeds 2048 characters", "too_long")

    parts = urlsplit(candidate)

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlRejected(
            f"scheme {scheme!r} is not permitted; only http and https are fetched",
            "bad_scheme",
        )
    if scheme == "http":
        notes.append("plaintext http; https strongly preferred")

    if parts.username or parts.password:
        raise UrlRejected(
            "URL carries embedded credentials, which the audit never transmits",
            "embedded_credentials",
        )

    host = (parts.hostname or "").lower().strip(".")
    if not host:
        raise UrlRejected("URL has no host component", "no_host")

    # Punycode any internationalised host so the checks below see ASCII.
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise UrlRejected(
            f"host {host!r} is not a valid internationalised domain name",
            "bad_idna",
        ) from None

    # Cloud metadata endpoints stay blocked even under the test escape hatch:
    # there is no legitimate reason for a fixture to live there.
    if host in METADATA_HOSTS:
        raise UrlRejected(f"host {host!r} is a cloud metadata endpoint", "blocked_host")
    if not permit_private:
        if host in BLOCKED_HOST_LITERALS:
            raise UrlRejected(
                f"host {host!r} refers to local or metadata infrastructure", "blocked_host"
            )
        for suffix in BLOCKED_HOST_SUFFIXES:
            if host.endswith(suffix):
                raise UrlRejected(
                    f"host {host!r} uses the reserved suffix {suffix!r}", "reserved_suffix"
                )

    _reject_obfuscated_ipv4(host)

    # A bracketed or bare IP literal must itself be public.
    is_ip_literal = False
    try:
        ipaddress.ip_address(host)
        is_ip_literal = True
    except ValueError:
        pass

    if is_ip_literal:
        ok, reason = classify_ip(host)
        if not ok and not permit_private:
            raise UrlRejected(f"host {host!r} is a {reason}", "private_ip")
        notes.append("target is a bare IP literal rather than a hostname")
    else:
        labels = host.split(".")
        if len(labels) < 2:
            raise UrlRejected(
                f"host {host!r} is not a fully-qualified domain name", "not_fqdn"
            )
        for label in labels:
            if not _LABEL_RE.match(label):
                raise UrlRejected(
                    f"host {host!r} contains an invalid DNS label {label!r}",
                    "bad_label",
                )
        if len(host) > 253:
            raise UrlRejected("hostname exceeds 253 octets", "host_too_long")

    port = parts.port if parts.port is not None else DEFAULT_PORTS[scheme]
    if port not in allowed_ports:
        raise UrlRejected(
            f"port {port} is outside the permitted set {sorted(allowed_ports)}",
            "bad_port",
        )

    if scope_domain and not same_scope(host, scope_domain):
        raise UrlRejected(
            f"host {host!r} is outside the audit scope {scope_domain!r}",
            "out_of_scope",
        )

    resolved: list[str] = []
    if resolve_dns and not is_ip_literal:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise UrlRejected(
                f"host {host!r} does not resolve ({exc.strerror or exc})", "dns_failure"
            ) from None
        seen: set[str] = set()
        for info in infos:
            addr = info[4][0]
            if addr in seen:
                continue
            seen.add(addr)
            ok, reason = classify_ip(addr)
            if not ok and not permit_private:
                # Every A/AAAA record must be public. One private answer is
                # enough to make the host unsafe to fetch.
                raise UrlRejected(
                    f"host {host!r} resolves to {addr}, a {reason}",
                    "resolves_private",
                )
            resolved.append(addr)
        if not resolved:
            raise UrlRejected(f"host {host!r} produced no addresses", "dns_empty")
    elif is_ip_literal:
        resolved.append(host)

    path = quote(unquote(parts.path or "/"), safe="/%:@!$&'()*+,;=~-._")
    canonical = urlunsplit(
        (
            scheme,
            host if port == DEFAULT_PORTS[scheme] else f"{host}:{port}",
            path,
            parts.query,
            "",  # fragments are never sent to a server
        )
    )

    return ValidatedUrl(
        url=canonical,
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        registrable_domain=registrable_domain(host),
        resolved_ips=resolved,
        notes=notes,
    )
