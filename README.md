# netplot

Netplot is a PingPlotter-style network latency and packet-loss dashboard with
continuous per-hop probing, live timelines, persistent background collections,
CSV export, and Prometheus metrics.

## Source

Clone the source repository:

```bash
git clone https://github.com/jschert/netplot.git
cd netplot
```

## Development

Create an environment and install the project with its test dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Run the Python and JavaScript checks (the latter requires Node.js):

```bash
python -m pytest
node tests/test_app.js
```

Run the dashboard server:

```bash
python -m netplot
```

or use the installed console script:

```bash
netplot
```

You can override bind options:

```bash
python -m netplot --host 0.0.0.0 --port 8080
```

The CLI starts Uvicorn with the `netplot.web:app` import path, allowing
`--reload` to restart the application while preserving the selected `--host`
and `--port`.

Available endpoints:

- `GET /` serves the dashboard UI: a PingPlotter-style toolbar (Play/Pause split, target combobox with recent-targets history, interval preset dropdown, Focus window dropdown, disabled ICMP packet-type dropdown, focus caption line), a live hop table (status / Hop / Count / IP / DNS / Avg / Min / Cur / PL% / Graph), and per-hop latency timeline graphs driven by SSE. The Graph column shows shared 100/200 ms zones, min–max whiskers, average/current markers, packet-loss hatching, and an average connector. The target timeline is always shown; double-click any hop row to add or remove a timeline for that hop. Drag a region on any timeline to focus the table stats on that period (the Focus dropdown is the rolling-window size; manual drag-focus overrides it until cleared). Pausing a trace freezes the view (timelines, stats, focus caption) at the server-recorded pause time; resuming continues from current wall time.
- `GET /static/app.js`, `GET /static/style.css` serve the dashboard assets.
- `GET /healthz` returns a simple readiness payload.
- `POST /api/traces` creates and starts a continuous trace against a target (host or IP); route discovery and concurrent per-hop probing begin automatically. See `docs/spec.md` for the full snapshot/SSE contract.
- `GET /api/traces/{id}/export?format=csv` downloads the full server-held sample history as a flat CSV table (`timestamp_utc,timestamp_epoch,hop_index,ip,dns,rtt_ms`; empty `rtt_ms` for lost probes). Works while running and while paused; 404 unknown session, 400 unsupported format, **429 with `Retry-After`** when two exports are already in flight (the build runs off the event loop under a per-process concurrency guard). Note: the export returns the FULL retained history (up to 10 000 samples per stream ≈ hours), and the endpoint has no authentication — any party that can reach the dashboard can pull any session's history. The dashboard's **Export** toolbar button (next to Reset) triggers a browser download for the active tab's session, enabled once that session has at least one sample.
- `GET /api/traces/{id}/history?hop=<index>&start=<epoch_s>&end=<epoch_s>&resolution=<raw|1m|auto>` returns per-stream sample history, clamped to `[start, end]`. `resolution=raw` (default) returns the retained raw samples (`{id, hop, resolution: "raw", earliest_t, samples: [{t, rtt_ms}]}`); `resolution=1m` returns per-minute rollup buckets (`{..., resolution: "1m", earliest_t, buckets: [{t, avg_ms, min_ms, max_ms, count, loss_count}]}`, full-loss buckets carry null avg/min/max, empty buckets are omitted); `resolution=auto` picks raw when the requested window fits raw retention (`SAMPLE_CAP * interval_s`), else 1m — the response carries the actual resolution used. `hop=0` is the target stream; `hop>=1` must match a discovered route hop. `earliest_t` (null when empty) lets the client distinguish "no data" from "older than retention". Works while running and while paused; 400 on malformed params or unsupported `resolution`; 404 on unknown session/hop. The dashboard fetches this on restore/reload/reconnect/range-widen so timelines populate immediately. 1h/3h timeline ranges are served raw; 12h/24h ranges use `resolution=auto` (resolved against raw retention) and are rendered from the bucket tier (Slice D2 — the server-side 1-minute rollup keeps memory bounded independent of probe interval; cap 1440 buckets/stream = 24h).
- `GET /metrics` exposes a Prometheus exposition endpoint (`text/plain; version=0.0.4`) for scraping by a Prometheus instance. Seven gauge families: `netplot_session_info` (one series per session, carrying a `state` label for alert gating), `netplot_target_rtt_current_ms`/`_avg_ms`/`_loss_pct`/`_up`, and `netplot_hop_rtt_current_ms`/`_loss_pct`. Avg and loss are computed over the last 60 samples per stream; the current-RTT gauge is absent when the last probe was a loss (no stale values). Paused sessions keep emitting last-known values truthfully labeled `state="paused"` — alert rules should gate on it. See `docs/spec.md` "Metrics" for the full series/label contract.
- `GET /api/settings/internal` / `PUT /api/settings/internal {cidrs: [...]}` manage the internal IP ranges (default RFC1918 + IPv6 ULA; other CIDRs editable, one per line in the settings dialog). Hops and the resolved target are classified server-side with the stdlib `ipaddress` module and annotated on the snapshot (`hops[].internal`, `target_internal`). The dashboard's **Mask internal DNS** toolbar toggle then replaces the DNS names of internal hosts with a neutral alias (`Internal host`) everywhere they are displayed — hop table, timeline labels, tab label/tooltips. The mask is display-only: IP addresses are never masked, and API/CSV payloads keep the real names. Persisted to `<data_dir>/settings.json` (atomic writes).
- `GET /api/settings/influx` / `PUT /api/settings/influx {url, database, interface, host_tag}` configure the optional InfluxDB 1.x throughput overlay (generic, settings-only — no presets: url/database/interface all blank by default, and a non-empty URL requires an explicit database and interface; the dialog placeholders are just examples). http(s) only, no userinfo/query/fragments; identifiers are charset-restricted so they cannot break out of the InfluxQL string literal. No credentials are created, stored, or sent; only existing no-auth sources are supported in this bounded first version.
- `PUT /api/settings {cidrs, influx}` is the combined transactional save the settings dialog's Save button sends: both sections validate, persist, and go live together — an invalid section (422) or a failed persist leaves runtime and `settings.json` untouched. The individual `GET/PUT /api/settings/internal|influx` endpoints remain; all settings writes are serialized server-side.
- `GET /api/throughput?start=<epoch_s>&end=<epoch_s>` returns normalized interface throughput for the span: `{start, end, step_s, points: [{t, in_mbps, out_mbps}]}` (null Mbps = missing bucket, never zero). The server queries backend-side via stdlib HTTP (bounded timeout, off the event loop, redirects refused, response bodies capped at 1 MiB) using `non_negative_derivative(last(field), 1s)` over the `net` measurement's `bytes_recv`/`bytes_sent` counters — the fixed `1s` unit makes every bucket bytes/second regardless of the aggregation step, and `last()` reads the counter's end-of-bucket value (counter-reset safe). The counters are grouped by `time(step)` plus the interface/host tags so multiple hosts surface as an error instead of silently merging, bucketed by a step ladder (5 s native for short zooms, aggregated up to 300 s for 24 h; span capped at 24 h). 400 on malformed/unconfigured requests, 502 when InfluxDB is unreachable, ambiguous (set a host tag), or returns a malformed payload. The dashboard overlays it on the primary/target timeline only, behind a per-tab **Throughput** checkbox, on its own labeled Mbps axis with two distinguishable direction lines that break at missing buckets and cadence-sized gaps (never bridged, never zero); latency/loss rendering and focus drag are unchanged, and a missing/unreachable source surfaces in the legend without breaking the chart. Saving the Influx settings clears every tab's overlay data, invalidates in-flight responses, and refetches (or disables the toggle) accordingly.

## Notes

Route discovery (`traceroute`), per-hop probing (`ping`), reverse DNS, the
live hop table API, the live dashboard UI (Slices 4 and 6), the timeline
graph with range/focus controls (Slice 5), and the PingPlotter-style
toolbar (Slice 6b) are implemented.

### Slice 6b toolbar

The toolbar clones the PingPlotter Cloud layout. Play and Pause are
separate buttons (Play is the green primary action, Pause is enabled only
while running). The Target input is a free-text combobox with a dropdown
of recent targets (most-recent-first, deduped, capped at 10, persisted in
`localStorage`). The Interval control is a preset dropdown (1 / 2.5 / 5 /
10 / 30 / 60 s, 2.5 preselected). The Focus dropdown (Auto / 1 minute /
5 minutes / 10 minutes) drives the table stats window, the snapshot fetch,
and the seed-baseline expiry; manual drag-focus overrides it until
cleared. A disabled ICMP-only packet-type dropdown and a Focus caption
line with live local-time bounds round out the row.

### Slice 6 graph column

The graph column uses one live axis shared by all hop rows. Its green, yellow,
and red zones are bounded at 100 ms and 200 ms; the axis ceiling is shown in
the header and recomputed from the largest observed hop latency. Each row has
a status dot, min–max whisker with end caps, navy average marker, open current
marker, and proportional packet-loss hatch. A red SVG connector joins average
markers after each table render and after resize; silent and loss-only rows do
not contribute points.

### Persistent background collections (Slice B)

A trace can be marked as a **background collection** via the
**Background** toolbar checkbox. Background sessions are saved to
`$NETPLOT_DATA_DIR/sessions.json` (default `<cwd>/data/sessions.json`)
and auto-restored when the server restarts, surviving container
restarts and redeploys. Only the session *definition* is saved
(target, interval, name, desired run/pause state) — sample history
restarts empty. Persistent tabs carry a `⇄` marker in the tab bar.
Mark a trace background, restart the server, and the trace re-attaches
in the browser on next load with probing resumed (or stays paused if
it was paused). The state file is atomic-write, schema-versioned, and
ignored-with-warning if corrupt — boot never crashes. See
`docs/spec.md` for the full state-file format and the
`POST /api/traces/{id}/persist` endpoint.

### Configuration and private data

Keep deployment-specific configuration and runtime data outside the source
checkout. Set `NETPLOT_DATA_DIR` to an external writable directory, or mount a
volume at `/data` in the container (see below). The directory holds
`settings.json` (internal CIDRs and optional InfluxDB connection settings) and
`sessions.json` (background collection definitions, including targets and names),
plus any recovery/quarantine files.

Configure network ranges and the optional throughput source through the dashboard's
Settings dialog; connection fields start blank, with no deployment-specific presets.
Do not commit real hostnames, addresses, credentials, `.env` files, local Compose
overrides, captures, or exports. Runtime data, local configuration, and diagnostic
artifacts are excluded from Git and the Docker build context; example environment
templates may be tracked, but must contain only generic values. DNS masking is
display-only, not a sanitization step for API responses, CSV exports, or captures.

## Container

Build and run the dashboard in Docker with no privileges required:

```bash
docker build -t netplot:dev .
docker run --rm -p 8000:8000 netplot:dev
# dashboard → http://127.0.0.1:8000
```

Or with the convenience compose file:

```bash
docker compose up -d --build
```

The image (`python:3.12-slim`-based, multi-stage) installs `iputils-ping`
and `traceroute` alongside the `netplot` wheel and runs the console script
as a non-root user (uid 1000). `ping` works unprivileged under Docker's
default `ping_group_range`; `traceroute` uses UDP probes, so neither binary
needs `cap_net_raw` or `--privileged`. Port 8000 is exposed; a stdlib-based
`HEALTHCHECK` polls `GET /healthz`. See `docs/spec.md` for the packaging
contract.

Note: on Docker Desktop for Mac the container runs inside the Linux VM, so
`traceroute`/`ping` originate there — LAN hops (and your local router) will
differ from running the same trace directly on the host.

To keep persistent (background) sessions across container restarts, mount
a volume at the data dir and set `NETPLOT_DATA_DIR`:

```bash
docker run --rm -p 8000:8000 \
  -e NETPLOT_DATA_DIR=/data \
  -v netplot-data:/data \
  netplot:dev
```

## Layout

- `src/netplot/` — application package
- `tests/` — automated tests
- `docs/` — project documentation
- `Dockerfile`, `docker-compose.yml`, `.dockerignore` — container packaging
