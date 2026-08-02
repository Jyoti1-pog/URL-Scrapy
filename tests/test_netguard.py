"""The SSRF guard, including the hop that makes it worth having.

A guard that only inspects the URL an operator typed is decorative:
`http://harmless.example/go` returning a 302 to the cloud metadata endpoint
walks straight past it. So the test that matters here is the redirect one.
"""

from __future__ import annotations

import ipaddress

import httpx
import pytest
import respx

from haat_lister.config import Settings
from haat_lister.fetch.static import FetchError, build_client, fetch_static
from haat_lister.utils.netguard import BlockedHost, check_url

BLOCKED = [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8000/admin",
    "http://localhost/admin",
    "http://10.0.0.5/internal",
    "http://192.168.1.1/router",
    "http://172.16.4.4/service",
    "http://[::1]/admin",
    "http://0.0.0.0/",
]


@pytest.mark.parametrize("url", BLOCKED)
async def test_private_and_metadata_addresses_are_refused(url: str) -> None:
    with pytest.raises(BlockedHost):
        await check_url(url)


async def test_the_metadata_endpoint_is_named_as_itself() -> None:
    """"link-local" is technically right and operationally useless. An operator
    reading a log should see what was actually reached for."""
    with pytest.raises(BlockedHost) as caught:
        await check_url("http://169.254.169.254/latest/meta-data/")
    assert "metadata" in str(caught.value)


async def test_the_message_carries_its_own_remedy() -> None:
    """The most likely person to hit this is an operator pointing at their own
    staging box. "Blocked" with no way forward just gets the guard switched off."""
    with pytest.raises(BlockedHost) as caught:
        await check_url("http://127.0.0.1:8799/plain/0")
    message = str(caught.value)
    assert "allow_private_hosts" in message
    assert "127.0.0.1" in message


async def test_a_non_http_scheme_is_refused() -> None:
    for url in ("file:///etc/passwd", "gopher://x/", "ftp://files.example/x"):
        with pytest.raises(BlockedHost):
            await check_url(url)


async def test_the_allowlist_is_by_host_not_by_switch() -> None:
    await check_url("http://127.0.0.1:8799/plain/0", allow_hosts=["127.0.0.1"])
    # Allowing one host does not allow the next one.
    with pytest.raises(BlockedHost):
        await check_url("http://10.0.0.5/x", allow_hosts=["127.0.0.1"])


async def test_a_public_address_passes() -> None:
    await check_url("http://93.184.216.34/anything")


@respx.mock
async def test_ssrf_blocks_metadata_endpoint_after_redirect(settings: Settings) -> None:
    """The whole reason the check is an event hook rather than a call at the top
    of the fetcher: httpx issues each redirect itself, and a chain never passes
    back through the fetcher."""
    start = "https://harmless.example/go"
    respx.get(start).mock(
        return_value=httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        )
    )
    metadata = respx.get("http://169.254.169.254/latest/meta-data/").mock(
        return_value=httpx.Response(200, text="ami-id")
    )

    async with build_client(settings) as client:
        with pytest.raises(FetchError) as caught:
            await fetch_static(client, start, settings)

    assert caught.value.reason == "blocked_address"
    assert not metadata.called, "the guard let the second hop through"


@respx.mock
async def test_a_normal_redirect_still_works(settings: Settings) -> None:
    """The guard must not make ordinary shop redirects fail."""
    respx.get("https://shop.example/p/1").mock(
        return_value=httpx.Response(301, headers={"location": "https://shop.example/products/1"})
    )
    respx.get("https://shop.example/products/1").mock(
        return_value=httpx.Response(
            200, html="<html><h1>Kurta</h1></html>", headers={"content-type": "text/html"}
        )
    )

    async with build_client(settings) as client:
        result = await fetch_static(client, "https://shop.example/p/1", settings)

    assert result.final_url == "https://shop.example/products/1"


async def test_an_unresolvable_host_is_not_treated_as_an_attack() -> None:
    """A typo should produce the fetcher's ordinary dns error, not a security
    message that sends an operator looking for a breach."""
    await check_url("http://this-host-does-not-exist-xyzzy-9182.invalid/p")


async def test_a_host_is_resolved_once_not_once_per_request() -> None:
    """Caught by a suite that went from two minutes to fifteen.

    The guard resolves before every hop, which is what makes it worth having --
    but a 200-URL batch on one shop is 200 requests to the same host plus its
    images, and without a cache that is 200 identical lookups the guard adds on
    top of the one httpx makes anyway. Unresolvable hosts are cached too, or a
    typo costs a full DNS timeout per row.
    """
    from haat_lister.utils import netguard

    netguard.clear_cache()
    calls = 0
    real = netguard._resolve

    async def counting(host: str):
        nonlocal calls
        calls += 1
        return await real(host)

    netguard._resolve = counting  # type: ignore[assignment]
    try:
        for _ in range(5):
            await check_url("http://this-host-does-not-exist-xyzzy-9182.invalid/p")
    finally:
        netguard._resolve = real  # type: ignore[assignment]

    assert calls == 5, "the wrapper itself should see every call"
    # The cache sits inside _resolve, so a second pass costs no syscall.
    netguard.clear_cache()
    import time

    start = time.monotonic()
    for _ in range(20):
        await check_url("http://this-host-does-not-exist-xyzzy-9182.invalid/p")
    assert time.monotonic() - start < netguard.DNS_TIMEOUT_S * 2, (
        "twenty checks of one unresolvable host paid more than two lookups"
    )


async def test_a_dead_resolver_does_not_add_twenty_seconds_a_row() -> None:
    from haat_lister.utils.netguard import DNS_TIMEOUT_S

    assert DNS_TIMEOUT_S <= 5.0, "the guard's DNS budget has to be well under a fetch timeout"


# --------------------------------------------------------------------------
# NAT64 / DNS64
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "allowed", "note"),
    [
        ("64:ff9b::401d:1141", True, "a public IPv4 host behind DNS64"),
        ("64:ff9b::8.8.8.8", True, "another public one, written the readable way"),
        ("64:ff9b::a9fe:a9fe", False, "the metadata endpoint smuggled through NAT64"),
        ("64:ff9b::7f00:1", False, "loopback through NAT64"),
        ("64:ff9b::c0a8:1", False, "192.168.0.1 through NAT64"),
        ("::ffff:169.254.169.254", False, "the metadata endpoint, v4-mapped"),
        ("::ffff:93.184.216.34", True, "a public address, v4-mapped"),
        ("2606:4700:4700::1111", True, "ordinary public v6"),
    ],
)
async def test_a_v4_address_inside_a_v6_one_is_judged_on_the_v4(
    address: str, allowed: bool, note: str
) -> None:
    """Caught by pointing the console at a real shop and being refused.

    On an IPv6-only or DNS64 network -- ordinary on mobile, common on corporate
    wifi -- a public IPv4 host resolves to 64:ff9b:: with the v4 address in the
    low 32 bits. Python calls that whole block "reserved", so a guard that stops
    there blocks EVERY public site on such a network.

    Unwrapping cuts both ways, which is why the blocked cases are here too: a
    naive fix that only unblocked things would wave `::ffff:169.254.169.254`
    straight through.
    """
    from haat_lister.utils.netguard import _classify

    verdict = _classify(ipaddress.ip_address(address))
    if allowed:
        assert verdict is None, f"{note}: blocked as {verdict}"
    else:
        assert verdict is not None, f"{note}: allowed through"


async def test_robots_does_not_raise_when_the_guard_refuses_an_origin(
    settings: Settings,
) -> None:
    """A blocked origin is not a robots decision, and raising here turned a
    preflight into a 500 in the browser."""
    from haat_lister.utils.robots import RobotsCache

    async with build_client(settings) as client:
        robots = RobotsCache(client, settings.user_agent)
        # Loopback is refused by the guard; asking about it must still answer.
        assert await robots.allowed("http://127.0.0.1:9/products/1") is True
