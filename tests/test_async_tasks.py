from __future__ import annotations

import asyncio
import logging

import pytest

from ydlna.app import _change_service_state, _start_configured_service
from ydlna.async_tasks import BackgroundTasks
from ydlna.dlna.renderer_bridge import RendererBridge
from ydlna.dlna.server import DlnaServer
from ydlna.player import hls_rewriter


def test_background_tasks_keep_reference_and_cancel_on_shutdown() -> None:
    async def scenario() -> None:
        registry = BackgroundTasks()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def worker() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = registry.create(worker(), name="test-worker")
        await started.wait()
        assert len(registry) == 1

        await registry.cancel_all()

        assert task.cancelled()
        assert cancelled.is_set()
        assert len(registry) == 0

    asyncio.run(scenario())


def test_background_tasks_remove_completed_task() -> None:
    async def scenario() -> None:
        registry = BackgroundTasks()

        async def worker() -> int:
            return 42

        task = registry.create(worker(), name="short-worker")
        assert await task == 42
        await asyncio.sleep(0)
        assert len(registry) == 0

    asyncio.run(scenario())


def test_background_tasks_retrieve_and_log_unhandled_exception(caplog) -> None:
    async def scenario() -> None:
        registry = BackgroundTasks()

        async def worker() -> None:
            raise RuntimeError("background failed")

        registry.create(worker(), name="failing-worker")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(registry) == 0

    with caplog.at_level(logging.ERROR, logger="LightCast.async_tasks"):
        asyncio.run(scenario())

    assert "后台任务 failing-worker 异常: background failed" in caplog.text


def test_hls_proxy_stop_cancels_warm_tasks_before_cleanup(monkeypatch) -> None:
    async def scenario() -> None:
        proxy = hls_rewriter.HlsProxy()
        proxy._segments = ["seg-0", "seg-1", "seg-2", "seg-3"]
        started: set[int] = set()
        cancelled: set[int] = set()

        async def warm(index: int) -> None:
            started.add(index)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(index)

        monkeypatch.setattr(proxy, "_warm_hybrid_segment", warm)
        proxy._schedule_warm(0)
        await asyncio.sleep(0)

        assert started == {1, 2, 3}
        assert len(proxy._background_tasks) == 3

        await proxy.stop()

        assert cancelled == {1, 2, 3}
        assert len(proxy._background_tasks) == 0

    asyncio.run(scenario())


def test_application_shutdown_stops_active_proxy_warm_task_and_session(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class Content:
            async def iter_chunked(self, _size: int):  # noqa: ANN202
                started.set()
                await asyncio.Event().wait()
                yield b"unreachable"

        class Response:
            status = 200
            content = Content()

            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class Session:
            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        response = Response()
        session = Session()
        proxy = hls_rewriter.HlsProxy()
        proxy._segments = ["https://public.example/segment.ts"]
        proxy._session = session  # type: ignore[assignment]

        async def get(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return response

        monkeypatch.setattr(proxy, "_get", get)
        warm_task = proxy._background_tasks.create(
            proxy._warm_hybrid_segment(0), name="active-warm-task"
        )
        await started.wait()

        bridge = RendererBridge.__new__(RendererBridge)
        bridge._poll_task = None
        bridge._hls_proxy = proxy
        bridge._direct_proxy = None

        await bridge.shutdown_all()

        assert warm_task.cancelled()
        assert response.closed is True
        assert session.closed is True
        assert bridge._hls_proxy is None

    asyncio.run(scenario())


def test_cancelled_hybrid_request_closes_upstream_response(monkeypatch) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class Content:
            async def iter_chunked(self, _size: int):  # noqa: ANN202
                started.set()
                await asyncio.Event().wait()
                yield b"unreachable"

        class Response:
            status = 200
            content = Content()

            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class Request:
            headers: dict[str, str] = {}

        response = Response()
        proxy = hls_rewriter.HlsProxy()
        proxy._segments = ["https://public.example/segment.ts"]

        async def get(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return response

        monkeypatch.setattr(proxy, "_get", get)
        task = asyncio.create_task(proxy._buffer_hybrid(0, Request()))  # type: ignore[arg-type]
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert response.closed is True

    asyncio.run(scenario())


def test_read_capped_propagates_cancellation() -> None:
    class Content:
        async def iter_chunked(self, _size: int):  # noqa: ANN202
            raise asyncio.CancelledError
            yield b"unreachable"

    class Response:
        content = Content()

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await hls_rewriter._read_capped(Response(), "test")  # type: ignore[arg-type]

    asyncio.run(scenario())


def test_configured_auto_start_failure_cleans_up_and_refreshes_ui_state() -> None:
    class Server:
        running = False

        def __init__(self) -> None:
            self.stop_calls = 0

        async def async_start(self) -> None:
            raise RuntimeError("start failed")

        async def async_stop(self) -> None:
            self.stop_calls += 1

    class Home:
        def __init__(self) -> None:
            self.states: list[bool] = []

        def set_service_running(self, running: bool) -> None:
            self.states.append(running)

    class Window:
        def __init__(self) -> None:
            self.homeInterface = Home()
            self.refresh_calls: list[tuple[str, str]] = []

        def refresh_device_info(self, name: str, address: str) -> None:
            self.refresh_calls.append((name, address))

    class Config:
        def get(self, key: str, default=None):  # noqa: ANN001, ANN202
            if key == "dlna_enabled":
                return True
            return default

    server = Server()
    window = Window()
    asyncio.run(
        _start_configured_service(  # type: ignore[arg-type]
            server, window, Config(),
        )
    )

    assert server.stop_calls == 1
    assert window.homeInterface.states == [False]
    assert window.refresh_calls == []


def test_service_stop_failure_is_retried_by_state_change_wrapper() -> None:
    class Server:
        running = True

        def __init__(self) -> None:
            self.stop_calls = 0

        async def async_stop(self) -> None:
            self.stop_calls += 1
            if self.stop_calls == 1:
                raise RuntimeError("first stop failed")
            self.running = False

    class Home:
        def __init__(self) -> None:
            self.states: list[bool] = []

        def set_service_running(self, running: bool) -> None:
            self.states.append(running)

    class Window:
        def __init__(self) -> None:
            self.homeInterface = Home()

        def refresh_device_info(self, _name: str, _address: str) -> None:
            raise AssertionError("stopped service must not refresh device info")

    class Config:
        def get(self, _key: str, default=None):  # noqa: ANN001, ANN202
            return default

    server = Server()
    window = Window()
    asyncio.run(
        _change_service_state(  # type: ignore[arg-type]
            server, window, Config(), False,
        )
    )

    assert server.stop_calls == 2
    assert window.homeInterface.states == [False]


def test_dlna_stop_cleans_resources_left_by_partial_start() -> None:
    class UpnpServer:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def async_stop(self) -> None:
            self.stop_calls += 1

    class Bridge:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    server = DlnaServer.__new__(DlnaServer)
    server._running = False
    server._server = UpnpServer()
    server._ssdp = None
    server._bridge = Bridge()

    asyncio.run(server.async_stop())

    assert server._server is None
    assert server._running is False
    assert server._bridge.shutdown_calls == 1


def test_dlna_stop_failure_preserves_reference_and_running_state() -> None:
    class UpnpServer:
        def __init__(self) -> None:
            self.stop_calls = 0
            self.fail = True

        async def async_stop(self) -> None:
            self.stop_calls += 1
            if self.fail:
                raise RuntimeError("listener still active")

    class Bridge:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    upstream = UpnpServer()
    server = DlnaServer.__new__(DlnaServer)
    server._running = True
    server._server = upstream
    server._ssdp = None
    server._bridge = Bridge()

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="listener still active"):
            await server.async_stop()

        assert server._server is upstream
        assert server._running is True

        upstream.fail = False
        await server.async_stop()

    asyncio.run(scenario())

    assert upstream.stop_calls == 2
    assert server._server is None
    assert server._running is False
    assert server._bridge.shutdown_calls == 2
