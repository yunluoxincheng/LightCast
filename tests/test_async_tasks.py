from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import textwrap

import pytest
from PySide6.QtCore import QObject

from ydlna.app import (
    _change_service_state,
    _connect_shutdown_requests,
    _start_configured_service,
)
from ydlna.async_tasks import BackgroundTasks
from ydlna.dlna.renderer_bridge import RendererBridge
from ydlna.dlna.server import DlnaServer
from ydlna.player import hls_rewriter


class _FakeSignal:
    def __init__(self) -> None:
        self.callback = None

    def connect(self, callback, *_args) -> None:  # noqa: ANN001
        self.callback = callback

    def emit(self, *args) -> None:  # noqa: ANN001
        assert self.callback is not None
        self.callback(*args)


class _QuitSources(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.commitDataRequest = _FakeSignal()
        self.quitRequested = _FakeSignal()
        self.applicationQuitRequested = _FakeSignal()
        self.actQuit = type("Action", (), {"triggered": _FakeSignal()})()
        self.settingsInterface = type(
            "Settings",
            (),
            {"applicationQuitRequested": self.applicationQuitRequested},
        )()
        self.event_filter = None

    def installEventFilter(self, event_filter) -> None:  # noqa: ANN001, N802
        self.event_filter = event_filter


def test_tray_quit_requests_async_shutdown_without_stopping_event_loop() -> None:
    async def scenario() -> None:
        app = _QuitSources()
        tray = _QuitSources()
        window = _QuitSources()
        stop_event = asyncio.Event()

        _connect_shutdown_requests(app, tray, window, stop_event)
        tray.actQuit.triggered.emit(False)

        assert stop_event.is_set()
        assert asyncio.get_running_loop().is_running()

    asyncio.run(scenario())


def test_system_close_requests_same_async_shutdown_path() -> None:
    async def scenario() -> None:
        app = _QuitSources()
        tray = _QuitSources()
        window = _QuitSources()
        stop_event = asyncio.Event()

        _connect_shutdown_requests(app, tray, window, stop_event)
        window.quitRequested.emit()

        assert stop_event.is_set()
        assert asyncio.get_running_loop().is_running()

    asyncio.run(scenario())


def test_session_commit_requests_shutdown_before_qt_quit() -> None:
    async def scenario() -> None:
        app = _QuitSources()
        tray = _QuitSources()
        window = _QuitSources()
        stop_event = asyncio.Event()

        _connect_shutdown_requests(app, tray, window, stop_event)
        app.commitDataRequest.emit(object())

        assert stop_event.is_set()
        assert asyncio.get_running_loop().is_running()

    asyncio.run(scenario())


def test_installer_launch_requests_same_async_shutdown_path() -> None:
    async def scenario() -> None:
        app = _QuitSources()
        tray = _QuitSources()
        window = _QuitSources()
        stop_event = asyncio.Event()

        _connect_shutdown_requests(app, tray, window, stop_event)
        window.settingsInterface.applicationQuitRequested.emit()

        assert stop_event.is_set()
        assert asyncio.get_running_loop().is_running()

    asyncio.run(scenario())


def test_qt_quit_is_gated_until_qasync_cleanup_completes() -> None:
    """真实 QApplication.quit 不得抢先停止 qasync 的清理协程。"""
    script = textwrap.dedent(
        """
        import asyncio
        from types import SimpleNamespace

        from PySide6.QtCore import QTimer, Signal, QObject
        from PySide6.QtWidgets import QApplication
        from qasync import QEventLoop

        from ydlna.app import _connect_shutdown_requests


        class Source(QObject):
            triggered = Signal(bool)
            quitRequested = Signal()
            applicationQuitRequested = Signal()


        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        loop = QEventLoop(app)
        asyncio.set_event_loop(loop)
        source = Source()
        tray = SimpleNamespace(actQuit=SimpleNamespace(triggered=source.triggered))
        settings = SimpleNamespace(applicationQuitRequested=source.applicationQuitRequested)
        window = SimpleNamespace(quitRequested=source.quitRequested, settingsInterface=settings)
        cleaned = []


        async def scenario():
            stop_event = asyncio.Event()
            gate = _connect_shutdown_requests(app, tray, window, stop_event)
            QTimer.singleShot(0, app.quit)
            await stop_event.wait()
            await asyncio.sleep(0)
            cleaned.append(True)
            app.removeEventFilter(gate)


        with loop:
            loop.run_until_complete(scenario())
        assert cleaned == [True]
        """
    )
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr


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

        async def buffer(index: int, _request=None, **_kwargs) -> bool:  # noqa: ANN001
            started.add(index)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(index)
            return True

        monkeypatch.setattr(proxy, "_buffer_hybrid", buffer)
        proxy._schedule_warm(0)
        await asyncio.sleep(0)

        assert started == {1, 2, 3}
        assert len(proxy._background_tasks) == 3

        await proxy.stop()

        assert cancelled == {1, 2, 3}
        assert len(proxy._background_tasks) == 0
        assert proxy._inflight == {}

    asyncio.run(scenario())


def test_hybrid_startup_waits_only_for_first_segment(monkeypatch) -> None:
    async def scenario() -> None:
        proxy = hls_rewriter.HlsProxy()
        proxy._segments = [f"seg-{index}" for index in range(6)]
        calls: list[int] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        background_started = asyncio.Event()
        started_in_background: set[int] = set()
        cancelled: set[int] = set()

        async def buffer(index: int, _request=None, **_kwargs) -> bool:  # noqa: ANN001
            calls.append(index)
            if index == 0:
                first_started.set()
                await release_first.wait()
                return True
            started_in_background.add(index)
            if started_in_background == {1, 2, 3}:
                background_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(index)
            return True

        monkeypatch.setattr(proxy, "_buffer_hybrid", buffer)

        startup_task = asyncio.create_task(proxy._warm_hybrid_startup())
        await asyncio.wait_for(first_started.wait(), timeout=2.0)
        await asyncio.wait_for(background_started.wait(), timeout=2.0)

        # 首片仍未完成时，后续 1-3 已利用它的网络等待并发开始。
        assert not startup_task.done()
        assert set(calls) == {0, 1, 2, 3}
        assert len(proxy._background_tasks) == 4

        # startup 仍只等待首片；后台 1-3 无需完成即可返回。
        release_first.set()
        await asyncio.wait_for(startup_task, timeout=2.0)
        assert len(proxy._background_tasks) == 3

        await proxy.stop()
        assert cancelled == {1, 2, 3}
        assert len(proxy._background_tasks) == 0
        assert proxy._inflight == {}

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
        bridge._poll_tasks = BackgroundTasks()
        bridge._hls_proxy = proxy
        bridge._direct_proxy = None

        await bridge.shutdown_all()

        assert warm_task.cancelled()
        assert response.closed is True
        assert session.closed is True
        assert proxy._inflight == {}
        assert bridge._hls_proxy is None

    asyncio.run(scenario())


def test_server_shutdown_then_application_shutdown_waits_for_poll_task() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        finished = asyncio.Event()

        async def poll() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        bridge = RendererBridge.__new__(RendererBridge)
        bridge._poll_tasks = BackgroundTasks()
        bridge._poll_task = bridge._poll_tasks.create(
            poll(), name="active-renderer-poll"
        )
        bridge._hls_proxy = None
        bridge._direct_proxy = None

        class UpnpServer:
            async def async_stop(self) -> None:
                pass

        server = DlnaServer.__new__(DlnaServer)
        server._running = True
        server._server = UpnpServer()
        server._ssdp = None
        server._bridge = bridge

        await started.wait()
        poll_task = bridge._poll_task

        # 与 app.run() 相同顺序：server stop 先同步取消轮询，随后应用级清理等待。
        await server.async_stop()
        assert bridge._poll_task is None

        await bridge.shutdown_all()

        assert poll_task is not None and poll_task.done()
        assert finished.is_set()
        assert len(bridge._poll_tasks) == 0

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
