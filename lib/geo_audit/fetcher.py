"""Polite, budgeted, read-only HTTP.

Design constraints, all of them load-bearing for this audit:

* **No JavaScript.** We deliberately fetch raw HTML, because that is what the
  crawlers behind AI assistants see. The gap between raw HTML and rendered DOM
  becomes a *measured finding* instead of an invisible assumption.
* **Every redirect hop is re-validated** through :mod:`netguard`. A public host
  that 302s to ``169.254.169.254`` must not be followed.
* **The validated address is the address we connect to.** netguard resolves the
  host and clears every answer; this module then dials that exact IP rather
  than re-resolving the name. Without the pin there are two lookups -- one
  checked, one used -- and a short-TTL record can differ between them, which is
  the whole DNS-rebinding attack.
* **Bounded decompression.** A response is capped on the wire *and* after
  inflation, so a few hundred KB of gzip cannot expand into gigabytes of RAM.
* **Hard budgets.** Wall-clock, page count and byte count are all capped so the
  audit finishes well inside the 5-minute limit on any site.
* **Read-only.** GET and HEAD only. Never authenticates, never posts.
"""

from __future__ import annotations

import http.client
import socket
import time
import zlib
from dataclasses import dataclass, field
from threading import Lock
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urljoin, urlsplit

from .netguard import UrlRejected, classify_ip, registrable_domain, validate_url

USER_AGENT = (
    "BrandAIReadinessAudit/1.0 (+read-only site audit; respects robots.txt)"
)
MAX_REDIRECTS = 5
MAX_BYTES_PER_PAGE = 3_000_000
DEFAULT_TIMEOUT = 15.0

# Ceiling on a single response *after* inflation. Compressed HTML runs about
# 4-8x; 12 MB leaves generous headroom for a legitimate page while making a
# decompression bomb inert. A body that hits the ceiling is truncated and the
# truncation is reported as evidence rather than swallowed.
MAX_DECOMPRESSED_BYTES = 12_000_000

# How many of a host's validated addresses to try before giving up. getaddrinfo
# already returns them in the order the OS prefers (RFC 6724), so this mirrors
# what socket.create_connection would have done -- minus the second lookup.
MAX_PINNED_ATTEMPTS = 3

# Time a single address may spend establishing its connection, when the host has
# more than one address to try.
#
# Without this, each address in turn received the *whole* request timeout, so a
# dual-stack host whose IPv6 route blackholes cost the full timeout on the dead
# family and then the full timeout again on the family that works -- every
# request, for every page. That is not a slow site; it is the audit doubling its
# own worst case. Capping the non-final attempts means a dead address is
# abandoned in seconds while the last address still gets everything left, so a
# single-address host (the common case) behaves exactly as it did before.
ADDRESS_CONNECT_SECONDS = 6.0


@dataclass
class Budget:
    """Global limits for one audit run."""

    max_pages: int = 20
    max_seconds: float = 240.0
    max_total_bytes: int = 40_000_000
    per_host_delay: float = 0.4

    started_at: float = field(default_factory=time.monotonic)
    pages_fetched: int = 0
    bytes_fetched: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed())

    def exhausted(self) -> tuple[bool, str]:
        if self.elapsed() >= self.max_seconds:
            return True, f"time budget of {self.max_seconds:.0f}s exhausted"
        if self.pages_fetched >= self.max_pages:
            return True, f"page budget of {self.max_pages} exhausted"
        if self.bytes_fetched >= self.max_total_bytes:
            return True, "byte budget exhausted"
        return False, ""

    def record(self, n_bytes: int) -> None:
        with self._lock:
            self.pages_fetched += 1
            self.bytes_fetched += n_bytes


def _registrable_of(url: str) -> str:
    """Best-effort registrable domain of an absolute URL, or '' if unusable."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    if not host:
        return ""
    try:
        return registrable_domain(host)
    except Exception:  # noqa: BLE001 - never let this break a fetch
        return host.lower()


@dataclass
class FetchResult:
    """One HTTP exchange, successful or not."""

    url: str
    final_url: str
    status: int | None
    ok: bool
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    raw_bytes: int = 0
    elapsed_ms: int = 0
    redirect_chain: list[dict] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    content_type: str = ""
    #: Registrable domain the landing URL moved away from. Empty unless
    #: ``fetch(..., allow_rescope=True)`` followed a cross-domain redirect.
    rehomed_from: str = ""

    @property
    def rescoped(self) -> bool:
        """True when the landing URL ended on a different registrable domain."""
        return bool(self.rehomed_from)

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "ok": self.ok,
            "content_type": self.content_type,
            "raw_bytes": self.raw_bytes,
            "elapsed_ms": self.elapsed_ms,
            "redirect_chain": self.redirect_chain,
            "error": self.error,
            "error_code": self.error_code,
            "rescoped": self.rescoped,
            "rehomed_from": self.rehomed_from,
            "headers": {
                k: v for k, v in self.headers.items()
                if k.lower() in (
                    "content-type", "cache-control", "last-modified", "etag",
                    "x-robots-tag", "content-encoding", "server", "location",
                    "strict-transport-security", "vary", "age",
                )
            },
        }


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Surface redirects to the caller so each hop can be re-validated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


# ---------------------------------------------------------------------------
# Address pinning
# ---------------------------------------------------------------------------
#
# netguard resolves the hostname and refuses every non-public answer. If we
# then hand the *name* to urllib, urllib resolves it a second time and connects
# to whatever that second lookup returns -- so the address we checked and the
# address we talk to need not be the same one. A record with a one-second TTL
# that answers public, then loopback, walks straight through the guard.
#
# The classes below dial a specific IP while leaving ``self.host`` untouched,
# so the ``Host:`` header and the TLS SNI / certificate check still use the
# real hostname. Only the socket's destination is overridden.


class _PinMixin:
    """Redirect a connection's socket to a fixed address.

    ``HTTPConnection.connect`` calls ``self._create_connection(...)``, which
    CPython assigns as an *instance* attribute in ``__init__`` -- so overriding
    it as a method does nothing. We rebind the attribute after ``__init__``
    instead, which works whether the base class treats it as an instance
    attribute or a class-level staticmethod.

    ``self.host`` is deliberately untouched: it is what supplies the ``Host:``
    header and, for TLS, ``server_hostname`` for SNI and certificate
    validation. Only the TCP destination changes.
    """

    def __init__(self, *args, pinned_ip: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pinned_ip = pinned_ip
        self._create_connection = self._connect_to_pin

    def _connect_to_pin(self, address, timeout=None, source_address=None):
        host, port = address
        return socket.create_connection(
            (self.pinned_ip or host, port), timeout, source_address
        )


class _PinnedHTTPConnection(_PinMixin, http.client.HTTPConnection):
    """An HTTP connection that dials ``pinned_ip`` instead of re-resolving."""


class _PinnedHTTPSConnection(_PinMixin, http.client.HTTPSConnection):
    """As above, over TLS. SNI and certificate checks still use the hostname."""


class _PinnedHTTPHandler(urlrequest.HTTPHandler):
    def __init__(self, pinned_ip: str | None) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req, pinned_ip=self._pinned_ip)


class _PinnedHTTPSHandler(urlrequest.HTTPSHandler):
    def __init__(self, pinned_ip: str | None) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def https_open(self, req):
        return self.do_open(
            _PinnedHTTPSConnection, req,
            context=self._context, pinned_ip=self._pinned_ip,
        )


def _pinned_opener(pinned_ip: str | None) -> urlrequest.OpenerDirector:
    """An opener bound to one address.

    Built per request rather than shared: the pin differs per host, and a fresh
    OpenerDirector is the simplest thing that is unambiguously correct with the
    reachability crawler's worker threads.
    """
    return urlrequest.build_opener(
        _NoRedirect, _PinnedHTTPHandler(pinned_ip), _PinnedHTTPSHandler(pinned_ip)
    )


def _connect_addresses(validated) -> list[str]:
    """The addresses we are willing to dial for *validated*, in preference order.

    Re-checks each one. netguard cleared them already; doing it again here means
    the check sits immediately next to the connect, where it cannot drift out of
    sync with what actually gets dialled.
    """
    candidates: list[str] = []
    for addr in validated.resolved_ips[:MAX_PINNED_ATTEMPTS]:
        ok, _ = classify_ip(addr)
        if ok or _private_addresses_permitted():
            candidates.append(addr)
    return candidates


def _private_addresses_permitted() -> bool:
    # Imported lazily so the test escape hatch stays in exactly one place.
    from .netguard import _private_allowed

    return _private_allowed()


class Fetcher:
    """Sequential-safe, thread-safe HTTP client bound to one audit scope."""

    def __init__(
        self,
        scope_domain: str,
        budget: Budget | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        respect_scope: bool = True,
    ) -> None:
        self.scope_domain = scope_domain
        self.budget = budget or Budget()
        self.timeout = timeout
        self.respect_scope = respect_scope
        self._last_request_at = 0.0
        self._throttle = Lock()

    # ------------------------------------------------------------------

    def _wait_turn(self) -> None:
        """Space requests out so the audit is never rate-abusive."""
        with self._throttle:
            gap = time.monotonic() - self._last_request_at
            if gap < self.budget.per_host_delay:
                time.sleep(self.budget.per_host_delay - gap)
            self._last_request_at = time.monotonic()

    @staticmethod
    def _inflate(raw: bytes, encoding: str, limit: int) -> tuple[bytes, bool]:
        """Decompress *raw*, stopping at *limit* bytes of output.

        Returns ``(data, hit_limit)``. The cap is the whole point: the wire-size
        cap says nothing about the inflated size, and a few hundred KB of gzip
        inflates to hundreds of MB without one -- which then goes on to be
        decoded to str, written to the workspace, and fed to the HTML parser.
        """
        if "gzip" in encoding:
            wbits = 16 + zlib.MAX_WBITS
        elif "deflate" in encoding:
            wbits = -zlib.MAX_WBITS
        else:
            return raw, False

        try:
            obj = zlib.decompressobj(wbits)
            data = obj.decompress(raw, limit)
            # Output held back means we stopped at the ceiling. Either way the
            # body is incomplete, and the caller reports it as truncated.
            hit_limit = bool(obj.unconsumed_tail) or len(data) >= limit
            return data, hit_limit
        except zlib.error:
            # Serve what we have -- a truncated or mislabelled body still yields
            # signal, and this is how the previous version behaved.
            return raw, False

    @staticmethod
    def _decode(raw: bytes, headers: dict, limit: int = MAX_DECOMPRESSED_BYTES):
        """Return ``(text, hit_decompression_limit)``."""
        encoding = (headers.get("content-encoding") or "").lower()
        raw, hit_limit = Fetcher._inflate(raw, encoding, limit)

        charset = "utf-8"
        ctype = headers.get("content-type", "")
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip().strip("\"'")
        for candidate in (charset, "utf-8", "latin-1"):
            try:
                return raw.decode(candidate, errors="strict"), hit_limit
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace"), hit_limit

    def _open_pinned(self, req, timeout: float, addresses: list):
        """Open *req* against each cleared address in turn.

        ``socket.create_connection`` would normally walk a host's addresses
        itself; pinning takes that away, so we walk the ones netguard already
        cleared. An HTTPError is a real answer from the server and is raised
        straight through -- only a failure to establish a connection moves on
        to the next address.

        The timeout is shared across the attempts rather than granted to each
        one. A non-final address is additionally capped at
        ``ADDRESS_CONNECT_SECONDS`` so an unreachable address family cannot
        consume the whole budget before a working one is even tried; the final
        address receives whatever remains.
        """
        last_error = None
        deadline = time.monotonic() + timeout
        final = len(addresses) - 1
        for index, address in enumerate(addresses):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempt_timeout = (
                remaining if index == final
                else min(ADDRESS_CONNECT_SECONDS, remaining)
            )
            try:
                return _pinned_opener(address).open(req, timeout=attempt_timeout)
            except urlerror.HTTPError:
                raise
            except (urlerror.URLError, TimeoutError, OSError) as exc:
                last_error = exc
        # The caller rejects an empty address list before calling this, but
        # ``raise None`` would surface as a TypeError rather than a transport
        # error if that contract ever changed, so say what actually happened.
        if last_error is None:
            raise urlerror.URLError("no addresses were available to connect to")
        raise last_error

    # ------------------------------------------------------------------

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        max_bytes: int = MAX_BYTES_PER_PAGE,
        allow_offsite: bool = False,
        count_against_budget: bool = True,
        allow_rescope: bool = False,
    ) -> FetchResult:
        """Fetch *url*, re-validating every redirect hop.

        Never raises for network conditions; failures come back as a
        ``FetchResult`` with ``ok=False`` so the audit can report them as
        evidence rather than crashing.

        *allow_rescope* is for the single landing request. A site that 301s to
        a different registrable domain (``cern.ch`` -> ``home.cern``) is a
        normal pattern, not a hostile one, so the redirect target is followed
        even though it leaves the original scope. Every *other* guard still
        applies to that hop: the destination is re-validated for private,
        loopback, link-local and metadata addresses exactly as before, so this
        cannot be used to reach a host the guard would otherwise refuse. The
        scope is not widened for anything else - the caller is expected to
        re-anchor the audit on the final host and crawl only that domain.
        """
        started = time.monotonic()
        chain: list[dict] = []
        base_scope = None if allow_offsite or not self.respect_scope else self.scope_domain
        rehomed_from = ""

        if count_against_budget:
            done, why = self.budget.exhausted()
            if done:
                return FetchResult(
                    url=url, final_url=url, status=None, ok=False,
                    error=f"skipped: {why}", error_code="budget_exhausted",
                )

        current = url
        for hop in range(MAX_REDIRECTS + 1):
            # The original URL must sit inside the requested scope. Redirect
            # targets may leave it, but only when the caller opted in.
            if hop == 0 or not allow_rescope:
                scope = base_scope
            elif base_scope and _registrable_of(current) != base_scope:
                # Out-of-scope redirect: drop only the scope guard for this hop.
                scope = None
            else:
                scope = base_scope
            try:
                validated = validate_url(current, scope_domain=scope)
            except UrlRejected as exc:
                return FetchResult(
                    url=url, final_url=current, status=None, ok=False,
                    redirect_chain=chain, error=exc.reason, error_code=exc.code,
                    rehomed_from=rehomed_from,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            if hop > 0 and scope is None and base_scope:
                rehomed_from = base_scope

            self._wait_turn()
            timeout = min(self.timeout, max(1.0, self.budget.remaining_seconds()))
            req = urlrequest.Request(
                validated.url,
                method=method,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                },
            )

            addresses = _connect_addresses(validated)
            if not addresses:
                return FetchResult(
                    url=url, final_url=validated.url, status=None, ok=False,
                    redirect_chain=chain, rehomed_from=rehomed_from,
                    error=(f"host {validated.host!r} produced no address that "
                           "cleared the address guard"),
                    error_code="no_safe_address",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            try:
                with self._open_pinned(req, timeout, addresses) as resp:
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    raw = resp.read(max_bytes + 1) if method == "GET" else b""
                    truncated = len(raw) > max_bytes
                    raw = raw[:max_bytes]
                    bomb = False
                    if method == "GET":
                        body, bomb = self._decode(raw, headers)
                    else:
                        body = ""
                    if count_against_budget:
                        # Charge the budget for the *inflated* size: that is the
                        # memory and disk the audit actually spends.
                        self.budget.record(max(len(raw), len(body)))
                    result = FetchResult(
                        url=url,
                        final_url=validated.url,
                        status=resp.status,
                        ok=200 <= resp.status < 300,
                        headers=headers,
                        body=body,
                        raw_bytes=len(raw),
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        redirect_chain=chain,
                        content_type=headers.get("content-type", ""),
                        rehomed_from=rehomed_from,
                    )
                    if truncated:
                        result.error = f"body truncated at {max_bytes} bytes"
                        result.error_code = "truncated"
                    elif bomb:
                        result.error = (
                            f"compressed body exceeded {MAX_DECOMPRESSED_BYTES} "
                            "bytes when inflated; truncated"
                        )
                        result.error_code = "decompression_limit"
                    return result

            except urlerror.HTTPError as exc:
                headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                if exc.status in (301, 302, 303, 307, 308) and "location" in headers:
                    target = urljoin(validated.url, headers["location"])
                    chain.append({"from": validated.url, "status": exc.status, "to": target})
                    # HTTPError *is* the response object. Nothing reads a redirect
                    # body, and the connection would otherwise stay open until
                    # garbage collection -- one leaked socket per hop on exactly
                    # the long chains this loop exists to walk.
                    exc.close()
                    if hop == MAX_REDIRECTS:
                        return FetchResult(
                            url=url, final_url=target, status=exc.status, ok=False,
                            redirect_chain=chain, rehomed_from=rehomed_from,
                            error=f"redirect chain exceeded {MAX_REDIRECTS} hops",
                            error_code="too_many_redirects",
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                        )
                    current = target
                    continue

                body = ""
                try:
                    body, _ = self._decode(exc.read(max_bytes), headers)
                except Exception:  # noqa: BLE001 - error bodies are best-effort
                    pass
                if count_against_budget:
                    self.budget.record(len(body))
                return FetchResult(
                    url=url, final_url=validated.url, status=exc.status, ok=False,
                    headers=headers, body=body, redirect_chain=chain,
                    rehomed_from=rehomed_from,
                    content_type=headers.get("content-type", ""),
                    error=f"HTTP {exc.status} {exc.reason}", error_code=f"http_{exc.status}",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            except urlerror.URLError as exc:
                reason = getattr(exc, "reason", exc)
                return FetchResult(
                    url=url, final_url=validated.url, status=None, ok=False,
                    redirect_chain=chain, rehomed_from=rehomed_from,
                    error=f"network error: {reason}",
                    error_code="network_error",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            except (TimeoutError, OSError) as exc:
                return FetchResult(
                    url=url, final_url=validated.url, status=None, ok=False,
                    redirect_chain=chain, rehomed_from=rehomed_from,
                    error=f"transport error: {exc}",
                    error_code="timeout",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

        return FetchResult(
            url=url, final_url=current, status=None, ok=False, redirect_chain=chain,
            rehomed_from=rehomed_from, error="redirect loop", error_code="redirect_loop",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
