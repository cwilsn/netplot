"""Prometheus ``/metrics`` support for netplot.

Exposes a capture helper (:func:`capture_metrics`) and a pure formatter
(:func:`render_metrics`) so the ``GET /metrics`` endpoint can produce
valid Prometheus 0.0.4 exposition text with no framework dependency.

Metric contract (see ``docs/spec.md`` "Metrics" for the full version):

============================================================  =========================
Metric                                                         Meaning
============================================================  =========================
``netplot_session_info``                                       One series per session
                                                               (value 1); ``state``
                                                               label lets alert rules
                                                               suppress paused sessions.
``netplot_target_rtt_current_ms``                              Most recent target RTT;
                                                               absent when the last
                                                               probe was a loss.
``netplot_target_rtt_avg_ms``                                  Mean RTT over the last
                                                               :data:`METRICS_WINDOW_SAMPLES`
                                                               target samples (losses
                                                               excluded); absent if no
                                                               successful samples.
``netplot_target_loss_pct``                                    Loss % over the last
                                                               :data:`METRICS_WINDOW_SAMPLES`
                                                               target samples.
``netplot_target_up``                                          1 if the most recent
                                                               target probe succeeded,
                                                               else 0.
``netplot_hop_rtt_current_ms``                                 Most recent RTT per route
                                                               hop (same no-stale-value
                                                               rule); ``hop`` label =
                                                               route index.
``netplot_hop_loss_pct``                                       Per-hop loss % over the
                                                               last :data:`METRICS_WINDOW_SAMPLES`
                                                               samples.
============================================================  =========================

Cardinality is bounded (``MAX_SESSIONS`` 8 x <= 30 hops), so the endpoint
iterates a small, bounded series set on every scrape.

The window is a fixed sample-count window (:data:`METRICS_WINDOW_SAMPLES`),
not a wall-clock window: avg and loss are computed over the most recent N
probes regardless of when they arrived. It is a named, documented constant
and is intentionally NOT configurable in this slice.

Phase split (load-bearing, mirrors :meth:`TraceSession.export_snapshot`):

- :func:`capture_metrics` reads live per-stream deques and MUST run on the
  event loop (no ``await`` between the call and the deque reads) so a
  concurrent probe write cannot mutate a deque mid-iteration. The returned
  :class:`SessionMetrics` list owns no references to live deques.
- :func:`render_metrics` is pure (touches only the captured data, returns a
  string) and is safe to call anywhere, including directly in unit tests
  without an HTTP server or a live :class:`~netplot.session.SessionManager`.

The average uses a divide-before-subtract incremental mean rather than a
naive sum. This avoids overflow for finite values near opposite float limits
and has the usual small floating-point accumulation error (approximately
``O(N * epsilon)`` for well-scaled inputs); that tradeoff is negligible for
millisecond-scale RTTs and preferable to emitting a non-finite gauge for
malformed injected data.

Paused sessions keep emitting last-known values; their ``state`` label is
``"paused"`` so alert rules can gate on it (documented in the spec). No
data series are emitted for a stream with zero samples yet.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from math import isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import SessionManager

__all__ = [
    "METRICS_WINDOW_SAMPLES",
    "HopMetrics",
    "SessionMetrics",
    "TargetMetrics",
    "capture_metrics",
    "render_metrics",
]

# Rolling window (by sample COUNT, not wall-clock) over which ``avg`` and
# ``loss`` are computed for every target/hop series. A fixed count window
# keeps each scrape O(window) regardless of how much history a stream has
# accumulated, and makes the avg/loss numbers directly comparable across
# streams that probe at different intervals. Named and documented; NOT
# configurable in this slice.
METRICS_WINDOW_SAMPLES = 60


# ----------------------------------------------------------------- data types
#
# Frozen dataclasses carrying only immutable scalars (str/float/int/None),
# so a captured snapshot is safe to hand to render_metrics off-loop without
# racing concurrent probe writes.


@dataclass(slots=True, frozen=True)
class TargetMetrics:
    """Target-stream rolling-window values.

    ``current_ms`` is ``None`` when the most recent probe was a loss (the
    no-stale-value rule) — the formatter then omits the current-RTT series
    rather than emitting a stale last-known-good value. ``avg_ms`` is
    ``None`` only when the window contains zero successful samples.
    """

    current_ms: float | None
    avg_ms: float | None
    loss_pct: float
    up: int


@dataclass(slots=True, frozen=True)
class HopMetrics:
    """One discovered route hop's rolling-window values."""

    hop_index: int
    current_ms: float | None
    loss_pct: float


@dataclass(slots=True, frozen=True)
class SessionMetrics:
    """Everything :func:`render_metrics` needs for one session's series."""

    session_id: str
    target: str
    name: str | None
    persistent: bool
    state: str
    # None when the session has zero target samples -> no netplot_target_*
    # series are emitted for this session.
    target_stream: TargetMetrics | None
    # One entry per discovered route hop that has at least one sample.
    # Hops with zero samples (e.g. freshly discovered, no probe round yet)
    # are omitted entirely, matching the session-level "no metrics for
    # streams with zero samples" rule.
    hops: list[HopMetrics]


# ------------------------------------------------------------- capture (on-loop)


def capture_metrics(manager: SessionManager) -> list[SessionMetrics]:
    """Snapshot every registered session into immutable :class:`SessionMetrics`.

    Reads live per-stream deques, so it MUST run on the event loop (no
    ``await`` between this call and the deque reads) to avoid racing a
    concurrent probe write — the same on-loop discipline as
    :meth:`TraceSession.export_snapshot`. The returned list owns no
    references to live deques and is safe to format anywhere (on or off
    the loop).
    """
    out: list[SessionMetrics] = []
    for session in manager.list():
        out.append(
            SessionMetrics(
                session_id=session.id,
                target=session.target,
                name=session.name,
                persistent=session.persistent,
                state=session.state,
                target_stream=_capture_target(session.samples),
                hops=_capture_hops(session.hops),
            )
        )
    return out


def _capture_target(samples: deque) -> TargetMetrics | None:
    """Build :class:`TargetMetrics` from the target stream, or ``None`` if empty."""
    win = _window(samples)
    if win is None:
        return None
    count, success_count, success_mean, last_rtt = win
    avg = success_mean if success_count else None
    loss_pct = ((count - success_count) / count * 100.0) if count else 0.0
    up = 1 if last_rtt is not None else 0
    return TargetMetrics(current_ms=last_rtt, avg_ms=avg, loss_pct=loss_pct, up=up)


def _capture_hops(hops: list) -> list[HopMetrics]:
    """Build :class:`HopMetrics` per hop with >= 1 sample; skip empty hops."""
    out: list[HopMetrics] = []
    for hop in hops:
        win = _window(hop.samples)
        if win is None:
            continue
        count, success_count, _success_mean, last_rtt = win
        loss_pct = ((count - success_count) / count * 100.0) if count else 0.0
        out.append(
            HopMetrics(hop_index=hop.index, current_ms=last_rtt, loss_pct=loss_pct)
        )
    return out


def _window(samples: deque) -> tuple[int, int, float, float | None] | None:
    """Single pass over the last :data:`METRICS_WINDOW_SAMPLES` samples.

    Returns ``(count, success_count, success_mean, last_rtt_ms)`` or ``None``
    when the deque is empty. ``count`` is the number of samples in the
    window (losses included); ``success_count``/``success_sum`` exclude
    losses. ``last_rtt_ms`` is the most recent sample's RTT (``None`` if
    that probe was a loss) — it drives the no-stale-value rule for
    ``current_ms`` and the ``up`` gauge.

    Uses ``reversed(deque)`` + ``islice`` so the pass is O(window) time and
    space regardless of how much history the deque holds (up to
    ``SAMPLE_CAP`` = 10 000): the older history is never traversed.
    """
    if not samples:
        return None
    tail = list(islice(reversed(samples), METRICS_WINDOW_SAMPLES))
    tail.reverse()
    count = len(tail)
    success_count = 0
    success_mean = 0.0
    for sample in tail:
        rtt = sample.rtt_ms
        # A malformed/overflowing ping value can still become ``inf`` when
        # converted to float (and callers can construct Samples directly).
        # Treat non-finite RTTs as losses: they are not measurements and must
        # not poison avg/current or leak as ``NaN``/``+Inf`` exposition values.
        if rtt is not None and isfinite(rtt):
            success_count += 1
            # Divide before subtracting: ``rtt - success_mean`` can overflow
            # for finite values with opposite signs near the float limit.
            # This is still an incremental mean, while keeping each operand
            # bounded by max(|rtt|, |mean|) / count.  It also preserves a
            # subnormal first value better than repeatedly dividing the whole
            # accumulated mean by count.
            if success_count == 1:
                success_mean = rtt
            else:
                success_mean += (rtt / success_count) - (success_mean / success_count)
    last_value = tail[-1].rtt_ms
    last_rtt = last_value if last_value is not None and isfinite(last_value) else None
    return count, success_count, success_mean, last_rtt


# --------------------------------------------------------- pure formatter
#
# No imports of session/web/fastapi here: render_metrics takes captured
# SessionMetrics and returns a string. Unit-testable without HTTP.


def _escape_label_value(value: str) -> str:
    """Escape a Prometheus label value per the exposition spec.

    Backslash, double-quote, and newline are the three characters that must
    be escaped inside a quoted label value. Backslash is escaped first so
    the later passes do not double-escape the backslashes they introduce.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: Iterable[tuple[str, str]]) -> str:
    """Render ``{k="v",k="v"}`` with each value escaped (deterministic order)."""
    return "{" + ",".join(f'{k}="{_escape_label_value(v)}"' for k, v in pairs) + "}"


def _fmt(value: float) -> str:
    """Render a numeric gauge value for Prometheus.

    ``str()`` on a Python float yields the shortest round-trip
    representation (e.g. ``12.5``, ``33.33333333333333``, ``0.0``), all of
    which Prometheus accepts. Ints render without a trailing ``.0``.
    """
    return str(value)


def render_metrics(sessions: Iterable[SessionMetrics]) -> str:
    """Render sessions to Prometheus 0.0.4 exposition text.

    Metric families are emitted in a fixed order; each gets one ``# HELP``
    and one ``# TYPE`` line followed by zero or more sample lines. A data
    family's sample lines are absent entirely when no session contributes a
    value (e.g. ``netplot_target_rtt_current_ms`` has no lines when every
    session's last probe was a loss) — that is the no-stale-value rule in
    exposition form. ``netplot_session_info`` is emitted for every
    registered session unconditionally; per-session data series are omitted
    until that session has samples.

    Pure: touches only the passed-in :class:`SessionMetrics` and returns a
    string. No HTTP or framework dependency.
    """
    sessions = list(sessions)
    lines: list[str] = []

    _emit_session_info(lines, sessions)
    _emit_target_current(lines, sessions)
    _emit_target_avg(lines, sessions)
    _emit_target_loss(lines, sessions)
    _emit_target_up(lines, sessions)
    _emit_hop_current(lines, sessions)
    _emit_hop_loss(lines, sessions)

    return "\n".join(lines) + "\n" if lines else ""


def _emit_session_info(lines: list[str], sessions: list[SessionMetrics]) -> None:
    lines.append(
        "# HELP netplot_session_info One series per netplot trace session "
        "(value 1); the state label is running or paused so alert rules "
        "can suppress paused sessions."
    )
    lines.append("# TYPE netplot_session_info gauge")
    for s in sessions:
        labels = _labels(
            [
                ("session_id", s.session_id),
                ("target", s.target),
                ("name", s.name or ""),
                ("persistent", "true" if s.persistent else "false"),
                ("state", s.state),
            ]
        )
        lines.append(f"netplot_session_info{labels} 1")


def _emit_target_current(lines: list[str], sessions: list[SessionMetrics]) -> None:
    lines.append(
        "# HELP netplot_target_rtt_current_ms Most recent target probe RTT "
        "in milliseconds; absent when the last probe was a loss (no stale "
        "values)."
    )
    lines.append("# TYPE netplot_target_rtt_current_ms gauge")
    for s in sessions:
        ts = s.target_stream
        if ts is not None and ts.current_ms is not None:
            labels = _labels([("session_id", s.session_id), ("target", s.target)])
            lines.append(f"netplot_target_rtt_current_ms{labels} {_fmt(ts.current_ms)}")


def _emit_target_avg(lines: list[str], sessions: list[SessionMetrics]) -> None:
    lines.append(
        "# HELP netplot_target_rtt_avg_ms Mean target RTT in milliseconds "
        "over the last 60 target samples (losses excluded); absent when no "
        "successful samples are in the window."
    )
    lines.append("# TYPE netplot_target_rtt_avg_ms gauge")
    for s in sessions:
        ts = s.target_stream
        if ts is not None and ts.avg_ms is not None:
            labels = _labels([("session_id", s.session_id), ("target", s.target)])
            lines.append(f"netplot_target_rtt_avg_ms{labels} {_fmt(ts.avg_ms)}")


def _emit_target_loss(lines: list[str], sessions: list[SessionMetrics]) -> None:
    lines.append(
        "# HELP netplot_target_loss_pct Target packet-loss percentage over "
        "the last 60 target samples."
    )
    lines.append("# TYPE netplot_target_loss_pct gauge")
    for s in sessions:
        ts = s.target_stream
        if ts is not None:
            labels = _labels([("session_id", s.session_id), ("target", s.target)])
            lines.append(f"netplot_target_loss_pct{labels} {_fmt(ts.loss_pct)}")


def _emit_target_up(lines: list[str], sessions: list[SessionMetrics]) -> None:
    lines.append(
        "# HELP netplot_target_up 1 if the most recent target probe succeeded, else 0."
    )
    lines.append("# TYPE netplot_target_up gauge")
    for s in sessions:
        ts = s.target_stream
        if ts is not None:
            labels = _labels([("session_id", s.session_id), ("target", s.target)])
            lines.append(f"netplot_target_up{labels} {_fmt(ts.up)}")


def _emit_hop_current(lines: list[str], sessions: list[SessionMetrics]) -> None:
    lines.append(
        "# HELP netplot_hop_rtt_current_ms Most recent RTT in milliseconds "
        "for each route hop (hop label = route index, final hop = target); "
        "absent when the hop's last probe was a loss."
    )
    lines.append("# TYPE netplot_hop_rtt_current_ms gauge")
    for s in sessions:
        for hop in s.hops:
            if hop.current_ms is None:
                continue
            labels = _labels(
                [
                    ("session_id", s.session_id),
                    ("target", s.target),
                    ("hop", str(hop.hop_index)),
                ]
            )
            lines.append(f"netplot_hop_rtt_current_ms{labels} {_fmt(hop.current_ms)}")


def _emit_hop_loss(lines: list[str], sessions: list[SessionMetrics]) -> None:
    lines.append(
        "# HELP netplot_hop_loss_pct Per-hop packet-loss percentage over "
        "the last 60 samples (hop label = route index)."
    )
    lines.append("# TYPE netplot_hop_loss_pct gauge")
    for s in sessions:
        for hop in s.hops:
            labels = _labels(
                [
                    ("session_id", s.session_id),
                    ("target", s.target),
                    ("hop", str(hop.hop_index)),
                ]
            )
            lines.append(f"netplot_hop_loss_pct{labels} {_fmt(hop.loss_pct)}")
