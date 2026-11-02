"""InfluxDB 1.x interface-throughput query (backend-side, stdlib HTTP).

The optional timeline overlay fetches cumulative interface counters
(``bytes_recv`` / ``bytes_sent`` on measurement ``net``, tagged by
``interface`` and optionally ``host``) from a user-configured InfluxDB 1.x
instance and returns per-bucket inbound/outbound Mbps. The server does the
querying so the browser never talks to the database directly.

Design points (bounded first version):

- **No credentials.** Only no-auth HTTP(S) sources are supported; the URL
  validator in :mod:`netplot.settings` rejects userinfo. Nothing is
  created, stored, or sent that resembles a secret.
- **No arbitrary queries.** The InfluxQL statement is built entirely from
  a fixed template plus a validated identifier (charset-restricted at
  settings time) and escaped again here via :func:`_quote`.
- **No redirects.** A redirect refusal opener ensures a misconfigured or
  malicious endpoint cannot bounce the request elsewhere (and never to a
  different scheme).
- **Counter-reset safe.** ``non_negative_derivative`` drops the negative
  delta produced by a counter reset instead of spiking.
- **Bytes per second at every step.** The derivative unit is fixed at
  ``1s`` — ``non_negative_derivative(last(field), 1s)`` — so a bucket's
  value is a per-second rate regardless of the aggregation step (a
  step-sized unit would inflate it 5/60/300×). ``last()`` reads the
  counter's end-of-bucket value (the proven telegraf/Grafana form), not
  a mean.
- **Series preserved.** ``GROUP BY time()`` alone would silently merge
  multiple hosts' counters into one series; grouping by the tag keys
  (``interface``, ``host``) keeps them separate, so an ambiguous
  host-less config surfaces as an error (multiple series → set a host
  tag) instead of merged garbage.
- **Missing data stays missing.** ``fill(none)`` omits empty buckets so
  gaps are never rendered as zero throughput.
- **Bounded.** The request runs in a worker thread with a short timeout,
  the time range is finite and span-capped (24 h, matching the largest
  timeline range), the bucket ladder bounds the point count (≤ 288
  points at the 24 h / 300 s step), and the response body is capped at
  :data:`MAX_RESPONSE_BYTES` (1 MiB).
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "INFLUX_TIMEOUT_S",
    "InfluxError",
    "build_query",
    "fetch_query",
    "parse_points",
    "query_throughput",
    "step_for_span",
]

log = logging.getLogger("netplot.influx")

#: Request timeout (seconds). Bounded so a dead endpoint cannot pin a
#: worker thread; the overlay surfaces the failure instead.
INFLUX_TIMEOUT_S = 5.0

#: Largest accepted response body (1 MiB). A real /query answer for the
#: bounded bucket ladder is a few KiB; anything larger is a misbehaving
#: endpoint and is refused rather than buffered.
MAX_RESPONSE_BYTES = 1_048_576

#: Largest accepted query span (24 h — the largest timeline range).
MAX_SPAN_S = 86_400

#: Measurement / field names (fixed template; not user-configurable).
_MEASUREMENT = "net"
_FIELD_RECV = "bytes_recv"
_FIELD_SENT = "bytes_sent"

_BYTES_PER_S_TO_MBPS = 8.0 / 1_000_000.0


class InfluxError(Exception):
    """Raised for any unreachable/invalid InfluxDB interaction.

    The message is safe to surface to the dashboard (it never includes
    the query body or credentials — none exist).
    """


def step_for_span(span_s: float) -> int:
    """Bucket step (seconds) for a query span.

    Short zooms use the source's native 5 s cadence; longer windows
    aggregate so the point count stays small (24 h → 288 points at 300 s).
    """
    if span_s <= 1_800:
        return 5
    if span_s <= 21_600:
        return 60
    return 300


def _quote(value: str) -> str:
    """Escape ``value`` as an InfluxQL string literal (defense in depth).

    Values are charset-validated at settings time; this backslash/quote
    escape makes a hand-edited settings file unable to break out of the
    literal even if that validation were bypassed.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def build_query(interface: str, host_tag: str, step_s: int, start_s: float, end_s: float) -> str:
    """Build the InfluxQL SELECT for inbound/outbound derivative buckets.

    Pure function of its arguments so it is unit-testable without a
    server. ``GROUP BY time()`` buckets are wall-clock aligned by
    InfluxDB; the derivative unit is fixed at ``1s`` so every bucket's
    value is bytes per SECOND regardless of the bucket step. Grouping
    by the tag keys (not time alone) preserves per-series output: a
    host-less config over multi-host data returns multiple series
    (rejected by :func:`parse_points`) instead of silently merged
    counters.
    """
    clauses = [f"interface = {_quote(interface)}"]
    if host_tag:
        clauses.append(f"host = {_quote(host_tag)}")
    clauses.append(f"time >= {int(start_s)}s")
    clauses.append(f"time <= {int(end_s)}s")
    fields = ", ".join(
        f'non_negative_derivative(last("{field}"), 1s) AS {alias}'
        for field, alias in ((_FIELD_RECV, "b_in"), (_FIELD_SENT, "b_out"))
    )
    return (
        f"SELECT {fields} FROM \"{_MEASUREMENT}\" "
        f"WHERE {' AND '.join(clauses)} "
        f'GROUP BY time({step_s}s), "interface", "host" fill(none)'
    )


def parse_points(payload: object) -> list[dict]:
    """Normalize an InfluxDB query response into ``[{t, in_mbps, out_mbps}]``.

    - Exactly one series is expected (the ``interface`` — and optional
      ``host`` — tag filters should pin one). More than one series means
      the config is ambiguous (e.g. multiple hosts with no ``host_tag``):
      :class:`InfluxError`, never silently-merged data.
    - ``null`` derivative values (bucket gaps, counter resets, first
      bucket) are preserved as ``None`` — missing data is never zeroed.
    - Byte-per-second derivatives convert to Mbps (× 8 / 1e6); times are
      nanosecond epochs → epoch seconds.
    - Malformed payloads (non-list ``series``/``values``, non-object
      series entries, non-finite timestamps, negative bandwidth — which
      a ``non_negative_derivative`` can never produce) raise
      :class:`InfluxError`, never an uncaught exception.
    """
    if not isinstance(payload, dict):
        raise InfluxError("unexpected influx response (not an object)")
    if "error" in payload:
        raise InfluxError(f"influx query error: {payload['error']}")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise InfluxError("unexpected influx response (results shape)")
    result = results[0]
    if not isinstance(result, dict):
        raise InfluxError("unexpected influx response (result shape)")
    if "error" in result:
        raise InfluxError(f"influx query error: {result['error']}")
    series = result.get("series")
    if series is None:
        return []  # no data in range — surfaced as "no data", not an error
    if not isinstance(series, list):
        raise InfluxError("unexpected influx response (series shape)")
    if len(series) == 0:
        return []
    if len(series) > 1:
        raise InfluxError(
            "influx returned multiple series; set a host tag to disambiguate"
        )
    one = series[0]
    if not isinstance(one, dict):
        raise InfluxError("unexpected influx response (series entry shape)")
    columns = one.get("columns")
    if not isinstance(columns, list):
        raise InfluxError(f"unexpected influx columns: {columns!r}")
    try:
        t_i = columns.index("time")
        in_i = columns.index("b_in")
        out_i = columns.index("b_out")
    except ValueError as exc:
        raise InfluxError(f"unexpected influx columns: {columns!r}") from exc

    def _mbps(value: object) -> float | None:
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        if f < 0:
            # non_negative_derivative can never produce this; a negative
            # rate means the payload is not what we asked for.
            raise InfluxError("influx returned negative bandwidth")
        return f * _BYTES_PER_S_TO_MBPS

    values = one.get("values")
    if values is None:
        values = []
    elif not isinstance(values, list):
        raise InfluxError("unexpected influx response (values shape)")
    points: list[dict] = []
    for row in values:
        if not isinstance(row, list) or len(row) <= max(t_i, in_i, out_i):
            continue
        t_ns = row[t_i]
        if isinstance(t_ns, bool) or not isinstance(t_ns, (int, float)):
            continue
        t_s = t_ns / 1e9
        if not math.isfinite(t_s):
            raise InfluxError("influx returned a non-finite timestamp")
        points.append(
            {
                "t": t_s,
                "in_mbps": _mbps(row[in_i]),
                "out_mbps": _mbps(row[out_i]),
            }
        )
    return points


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect (a redirect could change scheme/host)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def fetch_query(
    url: str, database: str, query: str, timeout_s: float = INFLUX_TIMEOUT_S
) -> dict:
    """POST-free GET ``/query`` against the configured InfluxDB; return parsed JSON.

    Runs blocking I/O — callers must keep it off the event loop
    (``asyncio.to_thread``). Network/HTTP/parse failures raise
    :class:`InfluxError` with a short, user-surfacing message.
    """
    endpoint = url.rstrip("/") + "/query"
    querystring = urllib.parse.urlencode({"db": database, "q": query, "epoch": "ns"})
    request = urllib.request.Request(
        endpoint + "?" + querystring, headers={"Accept": "application/json"}
    )
    try:
        with _OPENER.open(request, timeout=timeout_s) as response:
            # Bounded read: cap + 1 bytes so an oversized body is detected
            # without ever buffering it.
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise InfluxError(f"influx http error {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise InfluxError(f"influx unreachable: {exc}") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise InfluxError("influx response too large")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InfluxError("influx returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise InfluxError("influx returned an unexpected payload")
    return parsed


def query_throughput(influx_cfg: dict, start_s: float, end_s: float) -> dict:
    """End-to-end overlay query: validate range, fetch, normalize.

    Returns ``{"step_s": <int>, "points": [{t, in_mbps, out_mbps}]}``.
    Raises :class:`InfluxError` on any failure. Blocking; the API layer
    wraps it in ``asyncio.to_thread``.
    """
    if not (math.isfinite(start_s) and math.isfinite(end_s)) or end_s < start_s:
        raise InfluxError("invalid time range")
    if (end_s - start_s) > MAX_SPAN_S + 1:
        raise InfluxError(f"time range too large (max {MAX_SPAN_S}s)")
    step = step_for_span(end_s - start_s)
    query = build_query(
        influx_cfg["interface"], influx_cfg.get("host_tag") or "", step, start_s, end_s
    )
    payload = fetch_query(influx_cfg["url"], influx_cfg["database"], query)
    return {"step_s": step, "points": parse_points(payload)}
