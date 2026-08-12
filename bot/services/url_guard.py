"""SSRF guard for every URL this process hands to a downstream fetcher.

Lifted out of `add_service.py` unchanged (2026-08-12). It lived there because
*arr's `release/push` and the direct qBittorrent handoff were its only callers;
the TorrServer streaming contour needs the same guard, and importing the whole
AddService (Radarr + Sonarr + qBittorrent + Lidarr) to get at one function is a
bad trade.

`add_service` re-exports every name below, so `bot.services.add_service.<name>`
keeps resolving — including
`patch("bot.services.add_service._validate_download_url", ...)`, which several
tests rely on: `add_service` binds the name in its own namespace and calls it
unqualified, so the patch is still observed.
"""

import asyncio
import ipaddress
import re
import socket
import urllib.parse

from bot.config import get_settings

_ALLOWED_SCHEMES = {"http", "https", "magnet"}

# SEC-04/SEC-03: parameters in indexer download URLs commonly contain private
# trackers' credentials. `link`/`file`/`r`/`rss` are how Prowlarr's own
# download proxy embeds the ORIGINAL tracker URL (which itself carries a
# passkey/apikey) as a nested, url-encoded query value — masking only
# `apikey` leaves that nested secret in the clear.
_SENSITIVE_QUERY_PARAMS = {
    "apikey", "api_key", "token", "passkey", "auth", "authkey",
    "link", "file", "r", "rss",
}

# SEC-03: many private trackers embed the passkey directly as a path segment
# instead of (or in addition to) a query param, e.g.
# https://tracker/download/123/<32-char-hex-passkey>/name.torrent. Any long
# hex/base64-ish path segment is treated as a credential and masked.
_SECRET_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")

_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def _mask_path(path: str) -> str:
    """Mask path segments that look like a passkey/token (long hex/base64-ish)."""
    segments = path.split("/")
    masked = [
        "***" if _SECRET_PATH_SEGMENT_RE.match(seg) else seg
        for seg in segments
    ]
    return "/".join(masked)


def _mask_url(url: str, max_len: int = 100) -> str:
    """Return a safe representation of a download URL for logs (strips secrets)."""
    if not url:
        return ""
    if url.startswith("magnet:"):
        return url[:max_len]
    parsed = urllib.parse.urlparse(url)
    masked_path = _mask_path(parsed.path)
    if not parsed.query:
        base = f"{parsed.scheme}://{parsed.netloc}{masked_path}"
        return base[:max_len]
    parts = []
    for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if k.lower() in _SENSITIVE_QUERY_PARAMS:
            parts.append(f"{k}=***")
        else:
            parts.append(f"{k}={v}")
    base = f"{parsed.scheme}://{parsed.netloc}{masked_path}?{'&'.join(parts)}"
    return base[:max_len]


def _is_internal_ip(addr: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """Classify any non-public IP (private/loopback/link-local/reserved/multicast)."""
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _trusted_service_hosts() -> set[tuple[str, int]]:
    """(hostname, port) pairs of the user's OWN configured services.

    A self-hosted single-household stack runs Prowlarr/*arr/qBit on a private
    LAN, and a grab URL legitimately points at Prowlarr's download proxy on
    that LAN. Trust download URLs aimed at a configured service host; other
    internal addresses stay blocked.

    SEC-01: the pair MUST include the port. Trusting a hostname alone would
    trust ANY port on that host — in a typical stack the services share one
    LAN IP on different ports, so hostname-only trust degrades to "trust
    every port on this IP".
    """
    s = get_settings()
    hosts: set[tuple[str, int]] = set()
    for url in (
        s.prowlarr_url,
        s.radarr_url,
        s.sonarr_url,
        s.lidarr_url,
        s.qbittorrent_url,
        s.emby_url,
        # 2026-08-12: the streaming contour hands links to TorrServer, and
        # TorrServer itself is a legitimate destination on the same LAN.
        s.torrserver_url,
    ):
        if url:
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname
            if host:
                port = parsed.port or _DEFAULT_SCHEME_PORTS.get(parsed.scheme, 0)
                hosts.add((host.lower(), port))
    return hosts


async def _validate_download_url(
    url: str, extra_trusted: frozenset[tuple[str, int]] = frozenset(),
) -> bool:
    """
    Validate URL is safe for download (not SSRF).

    SEC-16 (fix round 1, restored — this docstring previously argued the
    opposite of the recovered pre-migration rationale, which re-justified a
    real regression): gates every URL the bot hands to a downstream fetcher
    that will act on it — *arr's `release/push` call AND the bot's own direct
    qBittorrent handoff (the force-download escape hatch and the push
    chain's own qBittorrent fallback step). *arr fetches whatever URL it is
    given, from inside the LAN — handing it a private/loopback URL would
    turn *arr itself into an SSRF proxy, exactly like handing that URL to
    qBittorrent directly would. Only the **native** grab path is exempt: it
    hands *arr a guid it already resolved itself via its own interactive
    search, never a URL this process constructed.

    Async to avoid blocking the event loop on DNS (SEC-11) and to inspect every
    A/AAAA record returned by getaddrinfo so a hostname with both public and
    private addresses is rejected (SEC-01).

    Exception: a URL pointing at one of the user's OWN configured services is
    trusted even on a private LAN.

    `extra_trusted` carries (host, port) pairs a caller knows to be legitimate
    beyond the configured services — today TorrServer's own torznab sources
    (`TorrServerClient.get_source_hosts`), which are where its release links
    come from. Empty by default, so every existing call site is unchanged.

    SEC-08: accepted risk — this is a check-then-use validation (TOCTOU). We
    resolve the hostname here, but the actual download happens later inside
    qBittorrent, which performs its OWN resolution. Closing this fully would
    require qBittorrent to accept a pre-resolved IP, which it doesn't support
    — out of scope for a self-hosted single-household deployment.
    """
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False
    if parsed.scheme == "magnet":
        return url.startswith("magnet:?xt=urn:btih:")
    if not parsed.hostname:
        return False
    url_port = parsed.port or _DEFAULT_SCHEME_PORTS.get(parsed.scheme, 0)
    if (parsed.hostname.lower(), url_port) in (_trusted_service_hosts() | set(extra_trusted)):
        return True
    try:
        addr = ipaddress.ip_address(parsed.hostname)
        return not _is_internal_ip(addr)
    except ValueError:
        pass  # hostname, resolve below
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, None)
    except socket.gaierror:
        return False
    for family, _t, _p, _c, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_internal_ip(addr):
            return False
    return True
