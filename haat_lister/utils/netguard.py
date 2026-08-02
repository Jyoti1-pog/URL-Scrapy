"""SSRF guard. Pulled forward from Phase 9 to Phase 1 on purpose.

From the moment the web console exists, this tool takes URLs typed by whoever
can reach port 8000 and fetches them from inside the operator's network. A guard
added after that surface exists is a guard that was absent while it mattered, so
it lands before the surface does.

    Blocked: loopback, private ranges, link-local (which covers the cloud
    metadata endpoint at 169.254.169.254), reserved, multicast, and any scheme
    that is not http or https.

Checked on EVERY redirect hop, not just the URL the operator typed. A guard that
only inspects the input is decorative: `http://harmless.example/go` returning a
302 to `http://169.254.169.254/latest/meta-data/` walks straight past it. The
hook below is wired into httpx's request event hooks, which fire once per hop.

WHAT THIS DOES NOT STOP, stated plainly rather than implied away: DNS rebinding.
We resolve a hostname and check the addresses; httpx then resolves it again when
it opens the socket. A hostile resolver can answer differently the second time.
Closing that hole means pinning the checked address into the connection, which
means replacing httpx's transport. If this tool is ever exposed beyond
127.0.0.1, that becomes worth doing -- and `serve --host 0.0.0.0` warns about it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from .logging import get_logger

log = get_logger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})


class BlockedHost(Exception):
    """A URL resolved somewhere we will not fetch from.

    Carries the remedy, because the single most likely person to hit this is an
    operator pointing the tool at their own staging box, and "blocked" without
    "here is how to allow it" just gets the guard switched off wholesale.
    """

    def __init__(self, host: str, reason: str, address: str | None = None) -> None:
        where = f" ({address})" if address else ""
        super().__init__(
            f"{host}{where} is {reason}, so it was not fetched.\n"
            f"If this is a machine you own and meant to point at, add it to "
            f"config.yaml:\n"
            f"    fetch:\n"
            f"      allow_private_hosts: [{host}]\n"
            f"That list is read from config only -- it can never be set by a request."
        )
        self.host = host
        self.reason = reason
        self.address = address


# NAT64. On an IPv6-only or DNS64 network -- ordinary on mobile, and on plenty
# of corporate wifi -- a public IPv4 host resolves to a v6 address with the v4
# one embedded in the low 32 bits. Python calls the whole 64:ff9b::/96 block
# "reserved", so a guard that stops there blocks EVERY public site on such a
# network. Caught by pointing the console at a real shop and being refused.
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _unwrap(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The address that actually gets connected to.

    A v6 address carrying a v4 one -- NAT64, v4-mapped, v4-compatible -- must be
    judged on the v4 address inside it. That cuts both ways: it unblocks a public
    host behind DNS64, and it still catches `::ffff:169.254.169.254`, which a
    naive check would wave through because the wrapper is not link-local.
    """
    if not isinstance(ip, ipaddress.IPv6Address):
        return ip
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if any(ip in block for block in _NAT64):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _classify(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """The reason this address is off limits, or None if it is fine.

    Ordered most-specific first so the message names the useful thing: "a cloud
    metadata endpoint" tells an operator more than "link-local".
    """
    ip = _unwrap(ip)
    if str(ip) in ("169.254.169.254", "fd00:ec2::254"):
        return "a cloud metadata endpoint"
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "link-local"
    if ip.is_private:
        return "a private network address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_reserved or ip.is_unspecified:
        return "a reserved address"
    return None


# A batch of 200 URLs on one shop is 200 requests to one host, plus its images.
# Without a cache that is 200-odd identical resolutions, each one a syscall the
# guard adds on top of the one httpx makes anyway. Short-lived on purpose: the
# point of re-checking every hop is that answers can change.
_CACHE_TTL_S = 300.0
_CACHE_MAX = 2048
_cache: dict[str, tuple[float, list[str] | None]] = {}

# A resolver that has stopped answering must not add twenty seconds to every
# row. Nothing is allowed through on a timeout that would not also be allowed
# through on NXDOMAIN -- in both cases the address is unknown, and a connection
# to an address we could not get is one httpx cannot make either.
DNS_TIMEOUT_S = 2.0


def clear_cache() -> None:
    _cache.clear()


async def _resolve(host: str) -> list[str] | None:
    """Every address the host answers with, or None if it does not resolve.

    All of them must pass: a name answering with one public and one private
    address is not half-safe.
    """
    now = asyncio.get_running_loop().time()
    if (hit := _cache.get(host)) is not None and now - hit[0] < _CACHE_TTL_S:
        return hit[1]

    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP), timeout=DNS_TIMEOUT_S
        )
        addresses: list[str] | None = sorted({str(info[4][0]) for info in infos})
    except (socket.gaierror, TimeoutError, OSError):
        addresses = None

    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[host] = (now, addresses)
    return addresses


async def check_url(url: str, allow_hosts: list[str] | None = None) -> None:
    """Raise BlockedHost unless this URL is safe to fetch. Otherwise return None."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()

    if scheme not in ALLOWED_SCHEMES:
        raise BlockedHost(host or url, f"not an http(s) URL (scheme {scheme or 'none'!r})")
    if not host:
        raise BlockedHost(url, "a URL with no host")

    if host in {h.lower() for h in (allow_hosts or [])}:
        log.debug("%s is in fetch.allow_private_hosts; skipping the address check", host)
        return

    # A literal IP needs no lookup, and must not get one -- resolving it would
    # be a no-op that could still fail on a broken resolver.
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None

    if literal is not None:
        if reason := _classify(literal):
            raise BlockedHost(host, reason)
        return

    addresses = await _resolve(host)
    if addresses is None:
        # Not a security failure, and not treated as one. A name we cannot
        # resolve is a name httpx cannot connect to either, so the row gets the
        # fetcher's ordinary dns_or_connect_error rather than a scary-sounding
        # block that sends an operator looking for a breach.
        log.debug("Could not resolve %s for the address check", host)
        return

    for address in addresses:
        if reason := _classify(ipaddress.ip_address(address)):
            raise BlockedHost(host, reason, address)


def request_hook(allow_hosts: list[str] | None = None):
    """An httpx request event hook that re-checks every hop.

    Attached to the client rather than called at the top of the fetcher, because
    a redirect chain never passes back through the fetcher.
    """

    async def hook(request: httpx.Request) -> None:
        await check_url(str(request.url), allow_hosts)

    return hook
