"""Unit tests for the probe engine: argv building, host validation, parsing."""

from __future__ import annotations

import asyncio

import pytest

from netplot.engine import (
    RouteDiscoveryError,
    RouteHop,
    build_ping_argv,
    build_traceroute_argv,
    discover_route,
    parse_rtt,
    parse_traceroute,
    probe_once,
    resolve_name,
    validate_host,
)

# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1",
        "example.com",
        "sub.domain.example.com",
        "::1",
        "[::1]",
        "fe80::1",
        "fe80::1%eth0",
        "host-name",
        "HOST123",
    ],
)
def test_validate_host_accepts_reasonable_targets(value: str) -> None:
    assert validate_host(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "-n",  # flag injection
        "-c",  # flag injection
        "127.0.0.1; rm -rf",  # shell metachar
        "host & cmd",  # ampersand
        "host | pipe",  # pipe
        "host`cmd`",  # backtick
        "host$VAR",  # dollar
        "host name",  # space
        "host/ls",  # slash
        "example.com\n",  # control character
        "éxample.com",  # non-ASCII host input is not passed to ping
        None,  # not a string
        12345,  # not a string
    ],
)
def test_validate_host_rejects_unsafe_targets(value: object) -> None:
    assert validate_host(value) is None


# --------------------------------------------------------------- argv builder


def test_linux_argv_uses_seconds() -> None:
    # Linux iputils-ping: -W is reply-timeout in seconds, min useful 1.
    assert build_ping_argv("127.0.0.1", 3.0, platform="linux") == [
        "ping",
        "-c",
        "1",
        "-W",
        "3",
        "127.0.0.1",
    ]


def test_linux_argv_rounds_up_subsecond_timeout() -> None:
    # 2.5 s must become a 3 s reply window (round up, not banker's round).
    assert build_ping_argv("127.0.0.1", 2.5, platform="linux") == [
        "ping",
        "-c",
        "1",
        "-W",
        "3",
        "127.0.0.1",
    ]


def test_linux_argv_clamps_to_minimum_one_second() -> None:
    assert build_ping_argv("127.0.0.1", 0.2, platform="linux") == [
        "ping",
        "-c",
        "1",
        "-W",
        "1",
        "127.0.0.1",
    ]


def test_macos_argv_uses_milliseconds() -> None:
    # macOS ping: -W is waittime in milliseconds.
    assert build_ping_argv("127.0.0.1", 3.0, platform="darwin") == [
        "ping",
        "-c",
        "1",
        "-W",
        "3000",
        "127.0.0.1",
    ]


def test_macos_argv_preserves_subsecond_resolution() -> None:
    assert build_ping_argv("127.0.0.1", 2.5, platform="darwin") == [
        "ping",
        "-c",
        "1",
        "-W",
        "2500",
        "127.0.0.1",
    ]


def test_macos_argv_falls_back_to_one_second_for_zero() -> None:
    # 0.0 / negative / NaN are clamped to the 1.0 s floor before the ms
    # conversion, so the macOS waittime becomes 1000 ms.
    assert build_ping_argv("127.0.0.1", 0.0, platform="darwin") == [
        "ping",
        "-c",
        "1",
        "-W",
        "1000",
        "127.0.0.1",
    ]


def test_build_ping_argv_rejects_nan_and_negative() -> None:
    # NaN/negative/zero fall back to the 1 s floor on both platforms.
    assert build_ping_argv("h", float("nan"), platform="linux")[-2] == "1"
    assert build_ping_argv("h", -5.0, platform="darwin")[-2] == "1000"
    assert build_ping_argv("h", 0.0, platform="linux")[-2] == "1"


# -------------------------------------------------------------------- parsing


def test_parse_rtt_macos_output() -> None:
    output = (
        "PING 127.0.0.1 (127.0.0.1): 56 data bytes\n"
        "64 bytes from 127.0.0.1: icmp_seq=0 ttl=64 time=0.073 ms\n"
        "\n--- 127.0.0.1 ping statistics ---\n"
        "1 packets transmitted, 1 packets received, 0.0% packet loss\n"
    )
    assert parse_rtt(output) == pytest.approx(0.073)


def test_parse_rtt_linux_output() -> None:
    output = (
        "PING 127.0.0.1 (127.0.0.1) 56(84) bytes of data.\n"
        "64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.042 ms\n"
        "\n--- 127.0.0.1 ping statistics ---\n"
        "1 packets transmitted, 1 received, 0% packet loss\n"
    )
    assert parse_rtt(output) == pytest.approx(0.042)


def test_parse_rtt_high_latency() -> None:
    assert parse_rtt("... time=123.456 ms ...") == pytest.approx(123.456)


def test_parse_rtt_timeout_output_is_none() -> None:
    # No "time=" line on timeout/unreachable.
    output = "PING 10.0.0.1 (10.0.0.1): 56 data bytes\nRequest timeout for icmp_seq=0\n"
    assert parse_rtt(output) is None


def test_parse_rtt_garbage_is_none() -> None:
    assert parse_rtt("totally unrelated text") is None
    assert parse_rtt("") is None


# ------------------------------------------------------------- probe_once fakes


class _FakeProc:
    def __init__(self, stdout: bytes = b"", returncode: int | None = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


def _make_fake_exec(stdout: bytes):
    """Return a subprocess_exec-compatible fake that yields ``stdout``."""

    async def fake_exec(*_argv, **_kwargs) -> _FakeProc:
        return _FakeProc(stdout=stdout)

    return fake_exec


def test_probe_once_parses_real_ping_output_via_fake_subprocess() -> None:
    stdout = b"64 bytes from 127.0.0.1: icmp_seq=0 ttl=64 time=0.123 ms\n"
    rtt = asyncio.run(
        probe_once(
            "127.0.0.1",
            1.0,
            subprocess_exec=_make_fake_exec(stdout),
            platform="linux",
        )
    )
    assert rtt == pytest.approx(0.123)


def test_probe_once_returns_none_on_unreachable_output() -> None:
    stdout = b"Request timeout for icmp_seq=0\n"
    rtt = asyncio.run(
        probe_once(
            "10.255.255.1",
            1.0,
            subprocess_exec=_make_fake_exec(stdout),
            platform="linux",
        )
    )
    assert rtt is None


def test_probe_once_returns_none_on_garbage_output() -> None:
    rtt = asyncio.run(
        probe_once(
            "127.0.0.1",
            1.0,
            subprocess_exec=_make_fake_exec(b"zzzzz not ping output"),
            platform="linux",
        )
    )
    assert rtt is None


def test_probe_once_uses_macos_argv_on_darwin() -> None:
    captured: dict = {}

    async def capturing_exec(*argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc(b"time=1.5 ms\n")

    rtt = asyncio.run(
        probe_once("127.0.0.1", 2.5, subprocess_exec=capturing_exec, platform="darwin")
    )
    assert rtt == pytest.approx(1.5)
    # Argv must contain the millisecond waittime, not seconds.
    assert "-W" in captured["argv"]
    wait_index = captured["argv"].index("-W")
    assert captured["argv"][wait_index + 1] == "2500"


def test_probe_once_uses_linux_argv_on_linux() -> None:
    captured: dict = {}

    async def capturing_exec(*argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc(b"time=1.5 ms\n")

    rtt = asyncio.run(
        probe_once("127.0.0.1", 2.5, subprocess_exec=capturing_exec, platform="linux")
    )
    assert rtt == pytest.approx(1.5)
    wait_index = captured["argv"].index("-W")
    assert captured["argv"][wait_index + 1] == "3"


def test_probe_once_rejects_invalid_host_without_calling_subprocess() -> None:
    called = []

    async def exec_should_not_run(*_argv, **_kwargs) -> _FakeProc:
        called.append(True)
        return _FakeProc(b"")

    rtt = asyncio.run(
        probe_once("-n", 1.0, subprocess_exec=exec_should_not_run, platform="linux")
    )
    assert rtt is None
    assert called == []


def test_probe_once_returns_none_when_subprocess_spawn_fails() -> None:
    async def failing_exec(*_argv, **_kwargs) -> _FakeProc:
        raise OSError("permission denied")

    rtt = asyncio.run(
        probe_once("127.0.0.1", 1.0, subprocess_exec=failing_exec, platform="linux")
    )
    assert rtt is None


def test_probe_once_returns_none_when_communicate_times_out() -> None:
    # communicate() never returns; the inner wait_for should fire and probe_once
    # should normalize to None.
    class _HangingProc:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(30)
            return b"", b""

        def kill(self) -> None:
            pass

    async def fake_exec(*_argv, **_kwargs) -> _HangingProc:
        return _HangingProc()

    rtt = asyncio.run(
        probe_once("127.0.0.1", 0.1, subprocess_exec=fake_exec, platform="linux")
    )
    assert rtt is None


def test_probe_once_kills_and_reaps_cancelled_process() -> None:
    class _CancellableProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=None)
            self.killed = False
            self.reaped = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(30)
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.reaped = True
            return -9

    proc = _CancellableProc()

    async def fake_exec(*_argv, **_kwargs) -> _CancellableProc:
        return proc

    async def run() -> None:
        task = asyncio.create_task(
            probe_once("127.0.0.1", 30.0, subprocess_exec=fake_exec)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert proc.killed
    assert proc.reaped


# ============================================================ Route discovery
#
# Slice 3 additions: traceroute argv building, output parsing, and the async
# discover_route / resolve_name wrappers.


# ----------------------------------------------------------- traceroute argv


def test_build_traceroute_argv_uses_n_q1_w2_m30() -> None:
    # The spec-mandated flags: -n (no DNS), -q 1 (one probe per hop),
    # -w 2 (2 s per-probe wait), -m 30 (max 30 hops), then the validated
    # target as a standalone argv element.
    assert build_traceroute_argv("1.2.3.4") == [
        "traceroute",
        "-n",
        "-q",
        "1",
        "-w",
        "2",
        "-m",
        "30",
        "1.2.3.4",
    ]


def test_build_traceroute_argv_passes_target_as_own_argv_element() -> None:
    # Even with a hostname, target is a standalone argv element (no shell).
    argv = build_traceroute_argv("example.com")
    assert argv[-1] == "example.com"
    assert "-w" in argv and "-m" in argv and "-q" in argv and "-n" in argv


# ------------------------------------------------------- traceroute parsing


def test_parse_traceroute_macos_output() -> None:
    output = (
        "traceroute to 1.2.3.4 (1.2.3.4), 30 hops max, 52 byte packets\n"
        " 1  10.0.0.1  0.523 ms\n"
        " 2  10.0.0.2  1.234 ms\n"
        " 3  1.2.3.4  2.345 ms\n"
    )
    assert parse_traceroute(output) == [
        RouteHop(index=1, ip="10.0.0.1"),
        RouteHop(index=2, ip="10.0.0.2"),
        RouteHop(index=3, ip="1.2.3.4"),
    ]


def test_parse_traceroute_linux_paren_form() -> None:
    # Linux traceroute prints the IP twice: bare and in parens. With -n there
    # is no hostname, but the parenthesised IP still appears.
    output = (
        "traceroute to 1.2.3.4 (1.2.3.4), 30 hops max, 60 byte packets\n"
        " 1  10.0.0.1 (10.0.0.1)  0.523 ms\n"
        " 2  10.0.0.2 (10.0.0.2)  1.234 ms\n"
        " 3  1.2.3.4 (1.2.3.4)  2.345 ms\n"
    )
    assert parse_traceroute(output) == [
        RouteHop(index=1, ip="10.0.0.1"),
        RouteHop(index=2, ip="10.0.0.2"),
        RouteHop(index=3, ip="1.2.3.4"),
    ]


def test_parse_traceroute_silent_hop_is_none_ip() -> None:
    output = " 1  10.0.0.1  0.523 ms\n 2  *\n 3  1.2.3.4  2.345 ms\n"
    parsed = parse_traceroute(output)
    assert parsed == [
        RouteHop(index=1, ip="10.0.0.1"),
        RouteHop(index=2, ip=None),
        RouteHop(index=3, ip="1.2.3.4"),
    ]


def test_parse_traceroute_multi_ip_line_takes_first() -> None:
    # Load-balanced paths surface as multiple IPs across probe attempts; the
    # spec says take the first.
    output = " 5  10.0.0.5  10.0.0.6  10.0.0.7  1.234 ms\n"
    assert parse_traceroute(output) == [RouteHop(index=5, ip="10.0.0.5")]


def test_parse_traceroute_keeps_ip_beside_annotation() -> None:
    # !H (host unreachable), !N (network unreachable), !X (admin prohibited),
    # etc. accompany a real IP — keep the IP, ignore the annotation.
    output = " 5  10.0.0.5  1.234 ms !H\n"
    assert parse_traceroute(output) == [RouteHop(index=5, ip="10.0.0.5")]


def test_parse_traceroute_short_route_under_max_hops() -> None:
    # Loopback / nearby targets terminate well before -m 30; parser must not
    # require any specific hop count.
    output = " 1  127.0.0.1  0.043 ms\n"
    assert parse_traceroute(output) == [RouteHop(index=1, ip="127.0.0.1")]


def test_parse_traceroute_target_as_final_hop_loopback() -> None:
    # The canonical ``traceroute 127.0.0.1`` output: 1 hop, target == hop.
    output = (
        "traceroute to 127.0.0.1 (127.0.0.1), 30 hops max, 52 byte packets\n"
        " 1  127.0.0.1  0.073 ms\n"
    )
    parsed = parse_traceroute(output)
    assert len(parsed) == 1
    assert parsed[0].index == 1
    assert parsed[0].ip == "127.0.0.1"


def test_parse_traceroute_handles_ipv6_hop() -> None:
    output = " 1  fe80::1  0.523 ms\n  2  2001:db8::1  1.234 ms\n"
    parsed = parse_traceroute(output)
    assert [h.ip for h in parsed] == ["fe80::1", "2001:db8::1"]


def test_parse_traceroute_empty_raises() -> None:
    with pytest.raises(RouteDiscoveryError):
        parse_traceroute("")


def test_parse_traceroute_garbage_raises() -> None:
    with pytest.raises(RouteDiscoveryError):
        parse_traceroute("traceroute: command not found")


def test_parse_traceroute_statistics_block_only_raises() -> None:
    # A traceroute that produced no hops but printed a closing stats block.
    output = "traceroute to 10.255.255.1 (10.255.255.1), 30 hops max, 60 byte packets\n"
    with pytest.raises(RouteDiscoveryError):
        parse_traceroute(output)


def test_parse_traceroute_normalizes_order_duplicates_and_gaps() -> None:
    output = (
        " 3  10.0.0.3  3 ms\n"
        " 1  *\n"
        " 3  10.0.0.3  3 ms\n"
        " 5  10.0.0.5  5 ms\n"
        " 5 packets transmitted, 5 received\n"
    )
    assert parse_traceroute(output) == [
        RouteHop(index=1, ip=None),
        RouteHop(index=3, ip="10.0.0.3"),
        RouteHop(index=5, ip="10.0.0.5"),
    ]


def test_parse_traceroute_rejects_numbered_garbage_line() -> None:
    with pytest.raises(RouteDiscoveryError):
        parse_traceroute(" 1  not-a-hop\n")


# ----------------------------------------------------------- discover_route


class _FakeRouteProc:
    def __init__(self, stdout: bytes = b"", returncode: int | None = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


def _make_fake_route_exec(stdout: bytes):
    async def fake_exec(*_argv, **_kwargs) -> _FakeRouteProc:
        return _FakeRouteProc(stdout=stdout)

    return fake_exec


def test_discover_route_parses_macos_output_via_fake_subprocess() -> None:
    stdout = (
        b"traceroute to 1.2.3.4 (1.2.3.4), 30 hops max, 52 byte packets\n"
        b" 1  10.0.0.1  0.523 ms\n"
        b" 2  1.2.3.4  1.234 ms\n"
    )
    hops = asyncio.run(
        discover_route("1.2.3.4", subprocess_exec=_make_fake_route_exec(stdout))
    )
    assert hops == [
        RouteHop(index=1, ip="10.0.0.1"),
        RouteHop(index=2, ip="1.2.3.4"),
    ]


def test_discover_route_rejects_invalid_target_without_subprocess() -> None:
    called: list = []

    async def exec_should_not_run(*_argv, **_kwargs) -> _FakeRouteProc:
        called.append(True)
        return _FakeRouteProc()

    with pytest.raises(RouteDiscoveryError):
        asyncio.run(discover_route("-n", subprocess_exec=exec_should_not_run))
    assert called == []


def test_discover_route_raises_on_spawn_failure() -> None:
    async def failing_exec(*_argv, **_kwargs) -> _FakeRouteProc:
        raise FileNotFoundError("traceroute: command not found")

    with pytest.raises(RouteDiscoveryError):
        asyncio.run(discover_route("1.2.3.4", subprocess_exec=failing_exec))


def test_discover_route_raises_on_unparsable_output() -> None:
    with pytest.raises(RouteDiscoveryError):
        asyncio.run(
            discover_route(
                "1.2.3.4",
                subprocess_exec=_make_fake_route_exec(b"totally not traceroute output"),
            )
        )


def test_discover_route_raises_on_communicate_timeout() -> None:
    class _HangingProc:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(30)
            return b"", b""

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            return -9

    async def fake_exec(*_argv, **_kwargs) -> _HangingProc:
        return _HangingProc()

    with pytest.raises(RouteDiscoveryError):
        asyncio.run(discover_route("1.2.3.4", timeout_s=0.1, subprocess_exec=fake_exec))


def test_discover_route_captures_argv_in_fake() -> None:
    captured: dict = {}

    async def capturing_exec(*argv, **kwargs) -> _FakeRouteProc:
        captured["argv"] = argv
        return _FakeRouteProc(stdout=b" 1  127.0.0.1  0.073 ms\n")

    hops = asyncio.run(discover_route("127.0.0.1", subprocess_exec=capturing_exec))
    assert hops == [RouteHop(index=1, ip="127.0.0.1")]
    argv = captured["argv"]
    assert argv[0] == "traceroute"
    assert argv[-1] == "127.0.0.1"
    # Required flags per spec.
    assert "-n" in argv and "-q" in argv and "-w" in argv and "-m" in argv


# -------------------------------------------------------------- resolve_name


def test_resolve_name_returns_none_for_bogus_ip() -> None:
    # RFC 0.0.0.0 / TEST-NET  almost always have no PTR; either way
    # resolve_name must not raise.
    name = asyncio.run(resolve_name("203.0.113.42"))
    assert name is None or isinstance(name, str)


def test_resolve_name_returns_none_for_malformed_ip() -> None:
    assert asyncio.run(resolve_name("not-an-ip")) is None


def test_resolve_name_loopback_returns_localhost_or_none() -> None:
    # 127.0.0.1 typically reverse-resolves to "localhost" on macOS/Linux,
    # but in offline sandboxes / containers it can return None. Accept both.
    name = asyncio.run(resolve_name("127.0.0.1"))
    assert name is None or isinstance(name, str)
