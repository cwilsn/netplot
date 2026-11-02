"""SSE tests.

The stack in use (starlette 1.3 + httpx 0.28) cannot stream an SSE response
through ``TestClient`` or ``httpx.ASGITransport``: both buffer the entire
response body before returning it to the client (see ``httpx/_transports/
asgi.py`` ``handle_async_request`` and ``starlette/testclient.py``
``_TestClientTransport.handle_request``). So we cover SSE two ways:

- ``test_sse_generator_*`` drive the ``_sse_stream`` generator directly,
  proving queue subscribe/publish/unsubscribe wiring and the exact wire
  format of every emitted frame, with two concurrent subscribers.

- ``test_sse_over_real_http_*`` spin up a real uvicorn server on a free
  localhost port and read the stream with a network ``httpx.AsyncClient``,
  proving the wire format survives the full ASGI -> HTTP -> client path.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn

from netplot.session import (
    Hop,
    ProbeResult,
    SessionManager,
)
from netplot.web import _sse_stream, create_app

# --------------------------------------------------- generator-level helpers


def _make_session():
    mgr = SessionManager()
    return mgr.create("127.0.0.1", interval_s=1.0)


async def _drain_until_two_data_frames(gen: AsyncIterator[str]) -> list[str]:
    """Drive an SSE async generator and collect output until 2 sample frames.

    Excludes the ``data: {}`` ready-payload frame from the count so we wait
    for two real round-event frames.
    """
    out: list[str] = []
    async for chunk in gen:
        # One yield can contain multiple lines; split on newline boundaries.
        out.extend(chunk.split("\n"))
        sample_lines = [
            l
            for l in out
            if l.startswith("data:") and l.strip() != "data: {}" and '"results"' in l
        ]
        if len(sample_lines) >= 2:
            return out
    return out


# ------------------------------------------------------- generator unit tests


def test_sse_generator_prelude_is_retry_and_ready() -> None:
    """The first frames must set retry and fire the ready event."""

    async def run() -> list[str]:
        session = _make_session()
        # Pull the prelude by awaiting the first few chunks, then unsubscribe.
        gen = _sse_stream(session)
        chunks: list[str] = []
        try:
            for _ in range(2):
                chunk = await asyncio.wait_for(gen.__anext__(), timeout=2)
                chunks.append(chunk)
        finally:
            await gen.aclose()
        return chunks

    chunks = asyncio.run(run())
    # First chunk sets the retry interval.
    assert chunks[0] == "retry: 1000\n\n"
    # Second chunk fires the ready event with a data payload so EventSource
    # actually dispatches it.
    assert chunks[1] == "event: ready\ndata: {}\n\n"


def test_sse_generator_emits_round_event_with_results() -> None:
    """Each published round produces one ``data:`` frame in the new format."""

    async def run() -> list[str]:
        session = _make_session()
        gen = _sse_stream(session)
        try:
            # skip prelude (2 yields)
            await gen.__anext__()
            await gen.__anext__()
            # publish round events
            session.append_round(
                t=1000.0,
                results=[ProbeResult(hop=0, rtt_ms=12.3)],
            )
            session.append_round(
                t=1001.0,
                results=[ProbeResult(hop=0, rtt_ms=45.6)],
            )
            chunk1 = await asyncio.wait_for(gen.__anext__(), timeout=2)
            chunk2 = await asyncio.wait_for(gen.__anext__(), timeout=2)
            return [chunk1, chunk2]
        finally:
            await gen.aclose()

    chunks = asyncio.run(run())
    # Exact wire format of an emitted round event.
    assert chunks[0] == (
        'data: {"t": 1000.0, "results": [{"hop": 0, "rtt_ms": 12.3}]}\n\n'
    )
    assert chunks[1] == (
        'data: {"t": 1001.0, "results": [{"hop": 0, "rtt_ms": 45.6}]}\n\n'
    )


def test_sse_generator_replays_route_event_on_attach() -> None:
    """A late subscriber learns the hop list via a replayed route event."""

    async def run() -> str:
        session = _make_session()
        session.hops = [Hop(index=1, ip="10.0.0.1", dns="router.lan")]
        session.route_state = "ready"
        gen = _sse_stream(session)
        try:
            await gen.__anext__()  # retry
            await gen.__anext__()  # ready
            return await asyncio.wait_for(gen.__anext__(), timeout=2)
        finally:
            await gen.aclose()

    chunk = asyncio.run(run())
    assert chunk == (
        "event: route\n"
        'data: {"hops": [{"index": 1, "ip": "10.0.0.1", "dns": "router.lan"}]}\n\n'
    )


def test_sse_generator_emits_live_route_event() -> None:
    """A subscriber attached before discovery sees the route event live."""

    async def run() -> str:
        session = _make_session()
        gen = _sse_stream(session)
        try:
            await gen.__anext__()  # retry
            await gen.__anext__()  # ready
            # Now route completes.
            session.hops = [Hop(index=1, ip="10.0.0.1", dns=None)]
            session.route_state = "ready"
            session.publish_route_event()
            return await asyncio.wait_for(gen.__anext__(), timeout=2)
        finally:
            await gen.aclose()

    chunk = asyncio.run(run())
    assert chunk == (
        "event: route\n"
        'data: {"hops": [{"index": 1, "ip": "10.0.0.1", "dns": null}]}\n\n'
    )


def test_sse_generator_two_subscribers_both_receive_round_events() -> None:
    """Two concurrent subscribers each receive every published round event."""

    async def run() -> tuple[list[str], list[str]]:
        session = _make_session()
        gen_a = _sse_stream(session)
        gen_b = _sse_stream(session)
        # Subscriber bookkeeping is set up lazily on first iteration.
        try:
            task_a = asyncio.create_task(_drain_until_two_data_frames(gen_a))
            task_b = asyncio.create_task(_drain_until_two_data_frames(gen_b))
            # Publish.
            await asyncio.sleep(0)  # let subscribers start
            session.append_round(t=1000.0, results=[ProbeResult(hop=0, rtt_ms=12.3)])
            session.append_round(t=1001.0, results=[ProbeResult(hop=0, rtt_ms=45.6)])
            a = await asyncio.wait_for(task_a, timeout=3)
            b = await asyncio.wait_for(task_b, timeout=3)
            return a, b
        finally:
            await gen_a.aclose()
            await gen_b.aclose()

    out_a, out_b = asyncio.run(run())
    # Filter out the prelude's ready-payload frame to isolate round frames.
    a_data = [
        l
        for l in out_a
        if l.startswith("data:") and l.strip() != "data: {}" and '"results"' in l
    ]
    b_data = [
        l
        for l in out_b
        if l.startswith("data:") and l.strip() != "data: {}" and '"results"' in l
    ]
    assert len(a_data) == 2
    assert len(b_data) == 2
    expected = [
        {"t": 1000.0, "results": [{"hop": 0, "rtt_ms": 12.3}]},
        {"t": 1001.0, "results": [{"hop": 0, "rtt_ms": 45.6}]},
    ]
    for data in (a_data, b_data):
        decoded = [json.loads(l[len("data:") :].strip()) for l in data]
        assert decoded == expected


def test_sse_generator_unsubscribes_on_close() -> None:
    """Closing the generator removes the subscriber queue from the session."""

    async def run() -> None:
        session = _make_session()
        gen = _sse_stream(session)
        await gen.__anext__()  # prelude causes subscribe()
        assert len(session._subscribers) == 1
        await gen.aclose()
        assert len(session._subscribers) == 0

    asyncio.run(run())


def test_sse_generator_ends_when_session_is_deleted() -> None:
    """Deleting a session must not leave connected streams waiting forever."""

    async def run() -> None:
        manager = SessionManager()
        session = manager.create("127.0.0.1")
        gen = _sse_stream(session)
        try:
            await gen.__anext__()  # retry
            await gen.__anext__()  # ready
            pending = asyncio.create_task(gen.__anext__())
            await asyncio.sleep(0)

            await manager.delete(session)

            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(pending, timeout=1)
        finally:
            await gen.aclose()

    asyncio.run(run())


def test_sse_generators_end_when_manager_shuts_down() -> None:
    """Application shutdown must terminate every connected stream."""

    async def run() -> None:
        manager = SessionManager()
        sessions = [manager.create("127.0.0.1"), manager.create("localhost")]
        generators = [_sse_stream(session) for session in sessions]
        pending = []
        try:
            for generator in generators:
                await generator.__anext__()
                await generator.__anext__()
                pending.append(asyncio.create_task(generator.__anext__()))
            await asyncio.sleep(0)

            await manager.shutdown()

            for task in pending:
                with pytest.raises(StopAsyncIteration):
                    await asyncio.wait_for(task, timeout=1)
        finally:
            for generator in generators:
                await generator.aclose()

    asyncio.run(run())


def test_sse_generator_emits_round_event_when_idle_injected() -> None:
    """Injecting a round event after the prelude yields a data frame."""

    async def run() -> str:
        session = _make_session()
        gen = _sse_stream(session)
        try:
            await gen.__anext__()  # retry
            await gen.__anext__()  # ready
            session.append_round(t=1.0, results=[ProbeResult(hop=0, rtt_ms=1.0)])
            return await asyncio.wait_for(gen.__anext__(), timeout=2)
        finally:
            await gen.aclose()

    chunk = asyncio.run(run())
    assert chunk == ('data: {"t": 1.0, "results": [{"hop": 0, "rtt_ms": 1.0}]}\n\n')


# ------------------------------------------------------- real-HTTP SSE test


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class _UvicornThread(threading.Thread):
    """Run uvicorn on a free port in a background thread until ``stop()``."""

    def __init__(self, app) -> None:
        super().__init__(daemon=True)
        self.app = app
        self.port = _free_port()
        self._server: uvicorn.Server | None = None

    def run(self) -> None:
        config = uvicorn.Config(
            app=self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._server.run()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        self.join(timeout=5)


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server on port {port} did not come up")


@pytest.mark.integration
def test_sse_over_real_http_prelude_and_data() -> None:
    """End-to-end SSE wire-format test through a real uvicorn server.

    Requires network binding to 127.0.0.1; skipped if the binding fails.
    """
    app = create_app()
    thread = _UvicornThread(app)
    try:
        thread.start()
        try:
            _wait_for_port(thread.port)
        except RuntimeError:
            pytest.skip("could not bind 127.0.0.1 for SSE integration test")

        async def run_with_injection() -> list[str]:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{thread.port}",
                timeout=httpx.Timeout(10.0),
            ) as client:
                r = await client.post(
                    "/api/traces",
                    json={"target": "127.0.0.1", "interval_s": 1.0},
                )
                sid = r.json()["id"]
                await client.post(f"/api/traces/{sid}/pause")
                session = app.state.session_manager.get(sid)
                assert session is not None

                lines: list[str] = []

                async def consume() -> None:
                    async with client.stream(
                        "GET", f"/api/traces/{sid}/events"
                    ) as resp:
                        async for line in resp.aiter_lines():
                            lines.append(line)
                            # Count only real round-event frames (not the
                            # ready prelude's `data: {}`).
                            rounds = [
                                l
                                for l in lines
                                if l.startswith("data:")
                                and l.strip() != "data: {}"
                                and '"results"' in l
                            ]
                            if len(rounds) >= 2:
                                return

                consumer = asyncio.create_task(consume())
                # Give the subscriber a moment to attach, then publish.
                await asyncio.sleep(0.3)
                session.append_round(
                    t=1000.0, results=[ProbeResult(hop=0, rtt_ms=12.3)]
                )
                await asyncio.sleep(0.1)
                session.append_round(
                    t=1001.0, results=[ProbeResult(hop=0, rtt_ms=45.6)]
                )
                await asyncio.wait_for(consumer, timeout=5)
                return lines

        lines = asyncio.run(run_with_injection())
    finally:
        thread.stop()

    # Prelude lines must arrive in order.
    assert "retry: 1000" in lines
    assert "event: ready" in lines
    ready_idx = lines.index("event: ready")
    # The ready event must be followed by a data line so EventSource dispatches it.
    assert any(l.startswith("data:") for l in lines[ready_idx + 1 : ready_idx + 3])

    data_lines = [l for l in lines if l.startswith("data:") and l.strip() != "data: {}"]
    assert len(data_lines) >= 2
    payloads = [json.loads(l[len("data:") :].strip()) for l in data_lines]
    expected = [
        {"t": 1000.0, "results": [{"hop": 0, "rtt_ms": 12.3}]},
        {"t": 1001.0, "results": [{"hop": 0, "rtt_ms": 45.6}]},
    ]
    for payload in expected:
        assert payload in payloads, f"missing payload {payload!r}; got {payloads!r}"
