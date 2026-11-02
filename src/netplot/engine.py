"""Probe engine: async system-ping and traceroute wrappers.

The engine is the only place that knows how to shell out to ``ping`` and
``traceroute``. It sits behind a narrow async interface (``probe_once``,
``discover_route``, ``resolve_name``) so that a future raw-socket engine can
replace it without touching sessions or the API.

Design notes:

- Host arguments are passed as a real argv list (no shell), and validated to
  reject flags and shell metacharacters.
- Platform flag selection is factored into ``build_ping_argv`` and
  ``build_traceroute_argv`` so they can be unit-tested without spawning
  anything.
- ``probe_once`` accepts a dependency-injected ``subprocess_exec`` callable so
  tests can drive the parsing/timeout logic with synthetic subprocess output.
  ``discover_route`` follows the same pattern.
- All ``probe_once`` failure modes (bad host, timeout, unreachable host, parse
  failure, subprocess error, cancellation) normalize to ``None`` so the
  session loop never has to defend against probe errors.
  ``discover_route`` is the opposite: it raises a typed
  ``RouteDiscoveryError`` because callers need to know when route discovery
  failed so they can fall back to target-only mode. Cancellation still
  propagates so the session task can be paused/stopped cleanly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import socket
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = [
    "RouteDiscoveryError",
    "RouteHop",
    "SubprocessExec",
    "build_ping_argv",
    "build_traceroute_argv",
    "discover_route",
    "parse_rtt",
    "parse_traceroute",
    "probe_once",
    "resolve_name",
    "validate_host",
]


# Hostname / IP validation.
#
# Allowed characters: letters, digits, dot, hyphen, colon (IPv6), square
# brackets (IPv6 literal form), percent (IPv6 zone identifiers). Anything
# outside this set — spaces, semicolons, ampersands, pipes, quotes, slashes —
# is rejected, which denies shell metacharacters and ping flag injection.
# Strings starting with ``-`` are rejected explicitly even though the regex
# would already exclude most flag characters, so the rule is unambiguous.
_HOST_RE = re.compile(r"[A-Za-z0-9._:\-\[\]%]{1,255}")

# Both macOS ``ping`` and Linux ``iputils-ping`` print ``time=<float> ms`` for
# a successful reply. The regex is tolerant of varying precision and of
# different locale separators (always ``.``).
_RTT_RE = re.compile(r"time=([0-9]+(?:\.[0-9]+)?)\s*ms", re.IGNORECASE)

# Outer time budget added on top of the requested reply timeout. The extra
# slack covers process startup and final stdout flush so we do not race the
# subprocess at the exact reply-timeout boundary.
_PROC_SLOP_S = 2.0

# Type alias for the dependency-injected subprocess exec callable.
# Matches asyncio.create_subprocess_exec's positional+keyword shape. The
# return type is left as ``Any`` because the engine only requires the result
# to have an awaitable ``communicate()`` method and a ``returncode``
# attribute (duck-typed in tests by lightweight fakes).
SubprocessExec = Callable[..., "Awaitable[object]"]


def validate_host(host: object) -> str | None:
    """Return ``host`` if it is a safe ping target, otherwise ``None``.

    Rejects non-strings, empty values, strings beginning with ``-`` (flag
    injection), and any character outside the small allowlist.
    """
    if not isinstance(host, str):
        return None
    if host.startswith("-"):
        return None
    # ``fullmatch`` is intentional: ``$`` would also match immediately before
    # a trailing newline, allowing a control character into the argv element.
    if _HOST_RE.fullmatch(host) is None:
        return None
    return host


def build_ping_argv(
    host: str, timeout_s: float, platform: str | None = None
) -> list[str]:
    """Build the ping argv for the given platform.

    Linux ``iputils-ping``: ``-W`` is the reply timeout in *seconds* (the
    minimum useful value is 1). We round the requested timeout up so a 2.5 s
    request becomes a 3 s reply window rather than a too-short 2 s window.

    macOS ``ping``: ``-W`` is the *waittime in milliseconds* before giving up
    on a reply. We convert the requested timeout to ms and round up to keep
    sub-second resolution. ``-t`` (deadline in seconds) is not used because
    ``-c 1 -W`` is enough to bound a single-probe call.

    The host is passed as a standalone argv element so there is no shell
    interpretation regardless of its content (it has already passed
    ``validate_host``).
    """
    if platform is None:
        platform = sys.platform

    # Clamp to a strictly positive value; defensive against NaN/zero/negative.
    try:
        timeout_s = float(timeout_s)
    except (TypeError, ValueError):
        timeout_s = 1.0
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        timeout_s = 1.0

    if platform == "darwin":
        wait_ms = max(1, math.ceil(timeout_s * 1000))
        return ["ping", "-c", "1", "-W", str(wait_ms), host]

    # Default branch covers Linux iputils-ping and any System V-ish ping that
    # accepts the same flag shape. BSDs other than Darwin differ; if we ever
    # target them we can branch here.
    wait_s = max(1, math.ceil(timeout_s))
    return ["ping", "-c", "1", "-W", str(wait_s), host]


def parse_rtt(output: str) -> float | None:
    """Extract the RTT in milliseconds from ping stdout.

    Returns ``None`` if no ``time=<float> ms`` token is present (timeout,
    unreachable, or any output shape we do not recognize).
    """
    match = _RTT_RE.search(output)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- route engine
#
# Traceroute output shape with ``-n -q 1`` on both macOS and Linux looks like:
#
#     traceroute to 1.2.3.4 (1.2.3.4), 30 hops max, 60 byte packets
#      1  10.0.0.1  0.523 ms
#      2  *
#      3  192.168.0.1  1.234 ms !H
#      4  1.2.3.4  2.345 ms
#
# - Lines start with a hop index, then either ``*`` (silent) or an IP token
#   optionally followed by ``(<ip>)`` repeats, RTT tokens, and ``!X``
#   annotations (host/network/protocol unreachable etc.).
# - Multi-IP lines (load-balanced paths) include several IPs across probe
#   attempts; we keep the first IP seen.
# - ``!H``/``!N``/``!X``/``!P`` annotations indicate ICMP error codes; they
#   accompany a real IP and must not change parsing of the IP.


class RouteDiscoveryError(Exception):
    """Raised when route discovery cannot produce a usable hop list."""


@dataclass(slots=True, frozen=True)
class RouteHop:
    """One parsed hop line. ``ip`` is ``None`` for silent hops (``*``)."""

    index: int
    ip: str | None


def build_traceroute_argv(
    target: str,
    *,
    max_hops: int = 30,
    wait_s: float = 2.0,
    platform: str | None = None,
) -> list[str]:
    """Build the traceroute argv for the given platform.

    macOS and Linux traceroute share the ``-n -q 1 -w <sec> -m <hops>`` flags
    in their default installations, so no platform branch is currently needed.
    The hook is kept so a System V-ish variant (e.g. some BusyBox images that
    use a different per-probe-wait flag) can branch here later without
    disturbing the call sites.

    ``target`` is appended as a standalone argv element after
    ``validate_host`` has approved it; there is no shell interpretation.
    """
    del platform  # currently unused; reserved for future per-OS branches.

    hops = max(1, int(max_hops))
    wait = max(1, round(wait_s))
    return [
        "traceroute",
        "-n",  # numeric IPs, no reverse DNS during the trace itself
        "-q",
        "1",  # one probe per hop (we re-probe continuously ourselves)
        "-w",
        str(wait),
        "-m",
        str(hops),
        target,
    ]


_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_IPV6_CHARS = set("0123456789abcdefABCDEF:")


def _is_ip_token(token: str) -> bool:
    """Return True if ``token`` looks like a numeric IPv4 or IPv6 address.

    IPv4 must be four dotted octets; IPv6 must contain at least one colon and
    only hex/colon characters. Anything else (hostnames, RTT tokens like
    ``1.234``, annotation strings like ``!H``) is rejected.
    """
    if _IPV4_RE.match(token) is not None:
        try:
            ipaddress.IPv4Address(token)
            return True
        except ValueError:
            return False
    candidate = token.split("%", 1)[0]
    if ":" not in candidate or not all(c in _IPV6_CHARS for c in candidate):
        return False
    try:
        ipaddress.IPv6Address(candidate)
        return True
    except ValueError:
        return False


# A hop line begins with optional whitespace, a small integer hop index, then
# at least one more character. The ``traceroute to ...`` header line and the
# closing statistics block do not match this shape.
_HOP_LINE_RE = re.compile(r"^\s*(\d{1,3})\s+\S")


def parse_traceroute(output: str) -> list[RouteHop]:
    """Parse ``traceroute -n -q 1`` stdout into a list of :class:`RouteHop`.

    Index values come from the traceroute output itself (1-based). Silent hops
    (``*``) yield ``ip=None``. Multi-IP and ``(ip)``-parenthesised forms are
    reduced to their first IP. ``!H`` / ``!N`` / ``!X`` style annotations are
    ignored (the IP beside them is still recorded).

    Raises :class:`RouteDiscoveryError` when the output yields zero parseable
    hop lines (empty output, error-message-only output, or anything else that
    does not look like a traceroute transcript).
    """
    by_index: dict[int, RouteHop] = {}
    for line in output.splitlines():
        match = _HOP_LINE_RE.match(line)
        if match is None:
            continue
        try:
            index = int(match.group(1))
        except ValueError:
            continue
        if index < 1:
            raise RouteDiscoveryError(f"invalid traceroute hop index: {index}")
        # Tokens after the hop index. Parens are turned into spaces so the
        # common ``ip (ip)`` Linux form yields the IP twice and we pick the
        # first occurrence.
        rest = line[match.end() - 1 :].replace("(", " ").replace(")", " ")
        tokens = rest.split()
        ip: str | None = None
        for tok in tokens:
            if _is_ip_token(tok):
                ip = tok
                break
        is_silent = any(tok == "*" for tok in tokens)
        if ip is None and not is_silent:
            if re.match(r"^\s*\d+\s+packets\b", line, re.IGNORECASE):
                continue
            raise RouteDiscoveryError(f"malformed traceroute hop line: {line!r}")
        candidate = RouteHop(index=index, ip=ip)
        previous = by_index.get(index)
        if previous is None or (previous.ip is None and ip is not None):
            by_index[index] = candidate
    if not by_index:
        raise RouteDiscoveryError("traceroute produced no parseable hop lines")
    return [by_index[i] for i in sorted(by_index)]


async def discover_route(
    target: str,
    timeout_s: float = 90.0,
    *,
    subprocess_exec: SubprocessExec | None = None,
    platform: str | None = None,
) -> list[RouteHop]:
    """Discover the route to ``target`` via system ``traceroute``.

    Returns a list of :class:`RouteHop` (1-indexed, silent hops included with
    ``ip=None``). Raises :class:`RouteDiscoveryError` on invalid host, missing
    traceroute binary, subprocess timeout, or output that yields no hops.
    Cancellation propagates as ``asyncio.CancelledError`` so callers can stop
    the discovery cleanly.

    ``subprocess_exec`` is injectable for tests; it follows the
    ``asyncio.create_subprocess_exec`` calling convention.
    """
    if validate_host(target) is None:
        raise RouteDiscoveryError(f"invalid traceroute target: {target!r}")

    argv = build_traceroute_argv(target, platform=platform)
    if subprocess_exec is None:
        subprocess_exec = asyncio.create_subprocess_exec

    # Spawn gets a small slack over the communicate deadline so a slow exec
    # does not race the outer timeout.
    spawn_deadline = _PROC_SLOP_S + 5.0
    proc: object | None = None
    try:
        try:
            proc = await asyncio.wait_for(
                subprocess_exec(
                    argv[0],
                    *argv[1:],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=spawn_deadline,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            raise RouteDiscoveryError(
                f"traceroute spawn failed: {exc.__class__.__name__}: {exc}"
            ) from exc

        try:
            stdout_bytes, _stderr = await asyncio.wait_for(
                _communicate(proc), timeout=timeout_s
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            raise RouteDiscoveryError(
                f"traceroute did not finish within {timeout_s}s "
                f"({exc.__class__.__name__}: {exc})"
            ) from exc

        if stdout_bytes is None:
            raise RouteDiscoveryError("traceroute produced no stdout")
        output = stdout_bytes.decode("utf-8", errors="replace")
        return parse_traceroute(output)
    finally:
        returncode = getattr(proc, "returncode", 0)
        if returncode is None:
            kill = getattr(proc, "kill", None)
            if callable(kill):
                try:
                    kill()
                except BaseException:  # noqa: BLE001, S110 - best-effort cleanup
                    pass
            wait = getattr(proc, "wait", None)
            if callable(wait):
                try:
                    # Reap a killed child as well as terminating it.
                    await asyncio.wait_for(wait(), timeout=1.0)  # type: ignore[arg-type]
                except BaseException:  # noqa: BLE001, S110 - cleanup is best effort
                    pass


async def resolve_name(ip: str) -> str | None:
    """Reverse-lookup ``ip`` to a hostname.

    Uses ``socket.gethostbyaddr`` in the default executor so it never blocks
    the event loop. Returns ``None`` on any failure (no PTR record, resolver
    timeout, AFNOSUPPORT, etc.). Never raises.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    try:
        result = await loop.run_in_executor(None, socket.gethostbyaddr, ip)
    except BaseException:  # noqa: BLE001 - reverse lookup is best effort
        return None
    if result and result[0]:
        return result[0]
    return None


async def probe_once(
    host: str,
    timeout_s: float,
    *,
    subprocess_exec: SubprocessExec | None = None,
    platform: str | None = None,
) -> float | None:
    """Probe ``host`` once via system ``ping``; return RTT in ms or ``None``.

    Never raises except for ``asyncio.CancelledError`` (so callers can be
    stopped cleanly). All probe-level failure modes — invalid host, subprocess
    spawn failure, reply timeout, parse failure — normalize to ``None``.

    ``subprocess_exec`` is injectable for tests; it must follow the
    ``asyncio.create_subprocess_exec`` calling convention and return an object
    with an awaitable ``communicate()`` method returning ``(stdout, stderr)``
    bytes and a ``returncode`` attribute.
    """
    if validate_host(host) is None:
        return None

    argv = build_ping_argv(host, timeout_s, platform=platform)
    if subprocess_exec is None:
        subprocess_exec = asyncio.create_subprocess_exec

    deadline = timeout_s + _PROC_SLOP_S
    proc: object | None = None
    try:
        try:
            proc = await asyncio.wait_for(
                subprocess_exec(
                    argv[0],
                    *argv[1:],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=deadline,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - normalize all spawn failures to None
            # Spawn timeout, OSError, anything from the injected callable —
            # none of these should break the session loop.
            return None

        try:
            stdout_bytes, _stderr = await asyncio.wait_for(
                _communicate(proc), timeout=deadline
            )
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - normalize all probe failures to None
            return None

        if stdout_bytes is None:
            return None
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        return parse_rtt(stdout)
    finally:
        # If the subprocess is still alive (we timed out or were cancelled
        # mid-probe), make sure we do not leak it.
        returncode = getattr(proc, "returncode", 0)
        if returncode is None:
            kill = getattr(proc, "kill", None)
            if callable(kill):
                try:
                    kill()
                except BaseException:  # noqa: BLE001, S110 - best-effort cleanup
                    pass
            wait = getattr(proc, "wait", None)
            if callable(wait):
                try:
                    # Reap a killed child as well as terminating it.  The
                    # timeout prevents a broken test double or subprocess
                    # implementation from holding cancellation hostage.
                    await asyncio.wait_for(wait(), timeout=1.0)  # type: ignore[arg-type]
                except BaseException:  # noqa: BLE001, S110 - cleanup is best effort
                    pass


async def _communicate(proc: object) -> tuple[bytes, bytes]:
    """Best-effort wrapper around proc.communicate() that satisfies type hints."""
    communicate = getattr(proc, "communicate", None)
    if not callable(communicate):
        return b"", b""
    return await communicate()  # type: ignore[misc]
