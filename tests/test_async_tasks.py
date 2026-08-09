from __future__ import annotations

import asyncio
import logging

import pytest

from ydlna.app import _change_service_state
from ydlna.async_tasks import BackgroundTasks
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


def test_service_start_failure_cleans_up_and_refreshes_ui_state() -> None:
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
        def get(self, _key: str, default=None):  # noqa: ANN001, ANN202
            return default

    server = Server()
    window = Window()
    asyncio.run(
        _change_service_state(  # type: ignore[arg-type]
            server, window, Config(), True,
        )
    )

    assert server.stop_calls == 1
    assert window.homeInterface.states == [False]
    assert window.refresh_calls == []


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
