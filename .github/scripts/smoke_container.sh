#!/usr/bin/env bash
#
# netplot container runtime smoke test.
#
# Usage:
#   smoke_container.sh IMAGE [HOST_PORT]
#
# Environment:
#   SMOKE_TIMEOUT  per-wait deadline in seconds (default 90; use ~180 when
#                  the image runs under QEMU emulation)
#
# Runs the given locally-built image as a REAL container (`--pull=never`
# refuses to fall back to any registry image) and proves the runtime
# contract end-to-end:
#
#   1. Health — GET /healthz answers status=ok and is not
#      persistence-degraded.
#   2. Packaging — the dashboard HTML and /static/{app.js,style.css} are
#      served with HTTP 2xx, the expected content types, and non-empty
#      bodies (checked over real HTTP, never by grepping shipped source
#      text or HTML element ids).
#   3. Real loopback probing — POST /api/traces against 127.0.0.1 and poll
#      the snapshot until a sample with a non-null rtt_ms arrives: the
#      container's ping subprocess genuinely executes and receives ICMP
#      replies on loopback (numeric target, so no external DNS or lab
#      dependency is involved).
#   4. Persistent state — with a Docker-generated anonymous volume at /data,
#      a settings write and a persistent (background) session definition
#      survive a full container recreation (docker rm -f + docker run with
#      the same volume), and the restored session resumes probing.
#   5. Non-root runtime — the image runs as the "netplot" user (uid 1000)
#      and the container never restarts (no crash loop).
#
# Resource isolation: nothing pre-existing on the host is ever touched and
# concurrent smoke runs cannot collide. The container is started WITHOUT
# --name (the daemon assigns a unique ID) and tagged with a per-invocation
# label; the volume is an anonymous Docker-generated volume whose name is
# discovered after the first start and re-mounted by name for the
# recreation; the host port defaults to an ephemeral 127.0.0.1 binding
# (pass HOST_PORT to pin one). Cleanup removes ONLY the container ID and
# volume this invocation created (plus any container still carrying this
# run's unique label) — no docker prune, no daemon-wide changes, and there
# is deliberately NO startup sweep of leftovers: another run's or a
# leftover's resources are never removed by us.
#
# Every HTTP call carries its own connect/request deadline, so each
# SMOKE_TIMEOUT wait loop below is a genuine bounded deadline.
#
# Exits non-zero on the first failure and always removes its container and
# volume. Requires docker, curl, and jq (all present on GitHub Actions
# ubuntu runners).
#
set -euo pipefail

IMAGE="${1:?usage: smoke_container.sh IMAGE [HOST_PORT]}"
HOST_PORT="${2:-}"  # empty => ephemeral host port chosen by the daemon
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-90}"

# Unique per-invocation tag. Only resources this invocation creates carry
# it (plus the captured IDs below); cleanup can never match anything else.
RUN_ID="netplot-smoke-run-$(date +%s)-$$-$RANDOM"
SMOKE_LABEL="netplot.smoke-run=${RUN_ID}"

CID=""    # Docker-generated container ID of the current container.
VOLUME="" # Docker-generated anonymous volume name (set after first start).
BASE=""   # http://127.0.0.1:<assigned port>, rebuilt after each start.

# Bounded HTTP: every request carries its own connect + total deadline, so
# no single curl can hang past the outer SMOKE_TIMEOUT loops.
CURL_CONNECT_TIMEOUT=5
CURL_MAX_TIME=30

http() {
  curl -sS --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIME" "$@"
}

cleanup() {
  # Remove ONLY what this invocation created: the captured container ID,
  # the captured anonymous volume, and (belt-and-braces, e.g. if the ID
  # capture itself failed) any container still carrying this run's unique
  # label. Pre-existing containers/volumes can never match. No prune.
  if [ -n "$CID" ]; then
    docker rm -f -v "$CID" >/dev/null 2>&1 || true
  fi
  if [ -n "$VOLUME" ]; then
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
  fi
  local c
  for c in $(docker ps -aq --filter "label=${SMOKE_LABEL}" 2>/dev/null); do
    docker rm -f -v "$c" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

fail() {
  echo "smoke: FAIL: $*" >&2
  if [ -n "$CID" ] && docker container inspect "$CID" >/dev/null 2>&1; then
    echo "----- last container logs -----" >&2
    docker logs --tail 40 "$CID" >&2 2>&1 || true
  fi
  exit 1
}

step() { echo; echo "smoke: ==> $*"; }

wait_healthy() {
  local deadline=$(( $(date +%s) + SMOKE_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local code
    # Short per-poll deadline (overrides the default --max-time; curl uses
    # the last one): a down/refused port fails fast instead of eating the
    # whole outer budget.
    code="$(http --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/healthz" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then return 0; fi
    sleep 1
  done
  fail "container did not answer GET /healthz within ${SMOKE_TIMEOUT}s"
}

run_container() {
  # Plain defaults: no --privileged, no --cap-add — the same unmodified
  # runtime shape end users get from `docker compose up`. No --name: the
  # daemon generates a unique container ID, so concurrent smoke runs can
  # never collide or delete each other's containers. The first start
  # mounts an anonymous Docker-generated volume at /data; the recreation
  # re-mounts that same volume by its discovered name.
  local args=(run -d --pull=never --label "$SMOKE_LABEL")
  if [ -n "$HOST_PORT" ]; then
    args+=("-p" "127.0.0.1:${HOST_PORT}:8000")
  else
    args+=("-p" "127.0.0.1::8000")  # ephemeral host port, loopback only
  fi
  if [ -n "$VOLUME" ]; then
    args+=("-v" "${VOLUME}:/data")
  else
    args+=("-v" "/data")  # anonymous volume, name generated by the daemon
  fi
  args+=("$IMAGE")
  CID="$(docker "${args[@]}")" \
    || fail "docker run failed for image '$IMAGE' (must be locally built/present; this smoke never pulls)"
  local publish
  publish="$(docker port "$CID" 8000/tcp | head -n1)"
  [ -n "$publish" ] || fail "no host port published for container $CID"
  BASE="http://${publish%:*}:${publish##*:}"
}

# True when the session's snapshot carries at least one successful sample.
snapshot_has_rtt() {
  local body
  body="$(http "$BASE/api/traces/$1/snapshot" 2>/dev/null || true)"
  [ -n "$body" ] || return 1
  jq -e '[.samples[]? | select(.rtt_ms != null)] | length > 0' >/dev/null 2>&1 <<<"$body"
}

# GET path + one or more content-type shell patterns. Proves real packaging
# behavior over HTTP: 2xx status, the served content type, and a non-empty
# body — never shipped source text or HTML element ids.
check_asset() {
  local meta type size pattern matched=false
  meta="$(http -f -o /dev/null -w '%{content_type} %{size_download}' "$BASE$1")" \
    || fail "GET $1 did not answer HTTP 2xx"
  type="${meta% *}"   # size_download is the final space-separated field
  size="${meta##* }"
  for pattern in "${@:2}"; do
    case "$type" in
      $pattern) matched=true; break ;;
    esac
  done
  [ "$matched" = true ] \
    || fail "GET $1 content-type is '${type}', expected one of: ${*:2}"
  [ "$size" -gt 0 ] 2>/dev/null \
    || fail "GET $1 returned an empty body"
  echo "  GET $1 -> 200 ${type} (${size} bytes)"
}

# ------------------------------------------------------------- pre-flight

[ "$(docker image inspect -f '{{.Config.User}}' "$IMAGE" 2>/dev/null || true)" = "netplot" ] \
  || fail "image Config.User is not 'netplot' (non-root contract broken)"

# ---------------------------------------------------------- first start

step "first container start"
run_container
# Discover the anonymous volume the daemon generated for /data so the
# recreation below (and cleanup) reuse exactly this run's volume.
VOLUME="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$CID")"
[ -n "$VOLUME" ] || fail "no Docker volume is mounted at /data for container $CID"
echo "  container ${CID:0:12} at $BASE (anonymous volume ${VOLUME:0:12})"
wait_healthy

step "health endpoint"
health="$(http -fsS "$BASE/healthz")" || fail "GET /healthz failed"
jq -e '.status == "ok" and (.persistence // "") != "degraded"' >/dev/null 2>&1 <<<"$health" \
  || fail "unexpected /healthz body: $health (expected status=ok, not persistence-degraded)"
echo "  /healthz -> $health"

step "static assets"
check_asset "/" 'text/html*'
check_asset "/static/style.css" 'text/css*'
check_asset "/static/app.js" 'text/javascript*' 'application/javascript*'
echo "  dashboard + /static assets packaged and served"

step "real loopback ping through the API"
trace="$(http -fsS -X POST "$BASE/api/traces" \
  -H 'Content-Type: application/json' \
  -d '{"target":"127.0.0.1","interval_s":1.0,"name":"smoke-live"}')" \
  || fail "POST /api/traces failed"
sid="$(jq -r '.id' <<<"$trace")"
[ -n "$sid" ] && [ "$sid" != "null" ] || fail "POST /api/traces returned no id: $trace"

deadline=$(( $(date +%s) + SMOKE_TIMEOUT ))
until snapshot_has_rtt "$sid"; do
  [ "$(date +%s)" -lt "$deadline" ] \
    || fail "no successful ping sample for 127.0.0.1 within ${SMOKE_TIMEOUT}s (probe engine broken in this image?)"
  sleep 1
done
echo "  snapshot carries non-null rtt_ms samples for 127.0.0.1"

http -fsS -X DELETE "$BASE/api/traces/$sid" -o /dev/null \
  || fail "DELETE /api/traces/$sid failed"

step "durable writes land in the volume"
settings_put="$(http -fsS -X PUT "$BASE/api/settings/internal" \
  -H 'Content-Type: application/json' \
  -d '{"cidrs":["10.0.0.0/8"]}')" \
  || fail "PUT /api/settings/internal failed"
jq -e '.cidrs == ["10.0.0.0/8"]' >/dev/null <<<"$settings_put" \
  || fail "PUT /api/settings/internal did not round-trip: $settings_put"

persist="$(http -fsS -X POST "$BASE/api/traces" \
  -H 'Content-Type: application/json' \
  -d '{"target":"127.0.0.1","interval_s":1.0,"name":"smoke-persist","persistent":true}')" \
  || fail "POST /api/traces (persistent) failed"
jq -e '.persistent == true' >/dev/null <<<"$persist" \
  || fail "POST persistent trace did not return persistent=true: $persist"

deadline=$(( $(date +%s) + SMOKE_TIMEOUT ))
until docker exec "$CID" test -s /data/sessions.json -a -s /data/settings.json 2>/dev/null; do
  [ "$(date +%s)" -lt "$deadline" ] \
    || fail "sessions.json/settings.json not present in /data after durable writes"
  sleep 1
done
# Bodies are captured first and parsed separately: with no curl|jq pipe
# there is no broken-pipe hazard under `set -o pipefail`.
sessions_json="$(docker exec "$CID" cat /data/sessions.json)"
jq -e '.sessions[] | select(.name == "smoke-persist" and .persistent == true)' >/dev/null <<<"$sessions_json" \
  || fail "sessions.json does not contain the smoke-persist definition"
settings_json="$(docker exec "$CID" cat /data/settings.json)"
jq -e '.internal_cidrs == ["10.0.0.0/8"]' >/dev/null <<<"$settings_json" \
  || fail "settings.json does not contain the written CIDR"
echo "  sessions.json + settings.json written to the volume"

step "non-root runtime + no crash restarts"
[ "$(docker exec "$CID" id -u)" = "1000" ] \
  || fail "container process is not running as uid 1000"
[ "$(docker inspect -f '{{.RestartCount}}' "$CID")" = "0" ] \
  || fail "container restarted (crash loop?)"

step "recreate the container (docker rm -f + run)"
docker rm -f "$CID" >/dev/null
CID=""
run_container  # re-mounts the SAME anonymous volume at /data
wait_healthy

step "state survives recreation"
listing="$(http -fsS "$BASE/api/traces")" || fail "GET /api/traces failed after recreation"
jq -e 'map(select(.name == "smoke-persist" and .persistent == true)) | length == 1' >/dev/null <<<"$listing" \
  || fail "persistent session definition did not survive recreation: $listing"
cidrs_body="$(http -fsS "$BASE/api/settings/internal")" \
  || fail "GET /api/settings/internal failed after recreation"
cidrs="$(jq -c '.cidrs' <<<"$cidrs_body")"
[ "$cidrs" = '["10.0.0.0/8"]' ] \
  || fail "settings did not survive recreation: $cidrs"
echo "  persistent session + settings restored"

step "restored session resumes probing"
restored_id="$(jq -r '.[] | select(.name == "smoke-persist") | .id' <<<"$listing")"
[ -n "$restored_id" ] && [ "$restored_id" != "null" ] \
  || fail "restored session id missing: $listing"
deadline=$(( $(date +%s) + SMOKE_TIMEOUT ))
until snapshot_has_rtt "$restored_id"; do
  [ "$(date +%s)" -lt "$deadline" ] \
    || fail "restored session produced no successful ping sample within ${SMOKE_TIMEOUT}s"
  sleep 1
done
[ "$(docker inspect -f '{{.RestartCount}}' "$CID")" = "0" ] \
  || fail "recreated container restarted (crash loop?)"
echo "  restored session is producing samples again"

echo
echo "smoke: PASS ($IMAGE)"
echo "smoke: proved health, packaged static assets, real loopback probing via the API,"
echo "smoke: persistent state across container recreation, and non-root runtime."
