from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from ydlna.dlna import server as server_module
from ydlna.dlna.server import DlnaServer
from ydlna.dlna.ssdp_listener import SsdpListener


def _listener() -> SsdpListener:
    return SsdpListener(
        udn="uuid:test-device",
        http_port=12345,
        server_id="Test/1.0 UPnP/1.1 LightCast/test",
    )


def test_ssdp_stop_sends_byebye_joins_thread_and_closes_resources(
    monkeypatch,
) -> None:
    listener = _listener()

    class Sender:
        ip = "192.0.2.10"

        def __init__(self) -> None:
            self.packets: list[bytes] = []
            self.closed = False

        def send(self, data: bytes) -> None:
            self.packets.append(data)

        def close(self) -> None:
            self.closed = True

    class Receiver:
        def __init__(self) -> None:
            self.closed = False

        def recvfrom(self, _size: int):  # noqa: ANN202
            while listener._running.is_set():  # noqa: SLF001
                time.sleep(0.005)
            raise OSError("listener stopped")

        def setsockopt(self, *_args) -> None:  # noqa: ANN002
            pass

        def close(self) -> None:
            self.closed = True

    sender = Sender()
    receiver = Receiver()

    def setup_socket() -> None:
        with listener._resources_lock:  # noqa: SLF001
            listener._sock = receiver  # type: ignore[assignment]  # noqa: SLF001
            listener._senders = [sender]  # type: ignore[list-item]  # noqa: SLF001

    monkeypatch.setattr(listener, "_setup_socket", setup_socket)

    listener.start()
    listener.wait_until_ready(timeout=1.0)
    listener.stop(timeout=1.0)

    assert not listener.is_alive()
    assert receiver.closed is True
    assert sender.closed is True
    assert any(b"NTS: ssdp:byebye" in packet for packet in sender.packets)


def test_ssdp_startup_failure_is_reported_and_partial_resources_are_closed(
    monkeypatch,
) -> None:
    listener = _listener()

    class PartialSocket:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    partial_socket = PartialSocket()

    def fail_setup() -> None:
        listener._sock = partial_socket  # type: ignore[assignment]  # noqa: SLF001
        raise OSError("bind failed")

    monkeypatch.setattr(listener, "_setup_socket", fail_setup)

    listener.start()
    with pytest.raises(RuntimeError, match="SSDP.*启动失败") as exc_info:
        listener.wait_until_ready(timeout=1.0)
    listener.join(timeout=1.0)

    assert isinstance(exc_info.value.__cause__, OSError)
    assert "bind failed" in str(exc_info.value.__cause__)
    assert not listener.is_alive()
    assert partial_socket.closed is True
    assert listener._sock is None  # noqa: SLF001


def test_dlna_start_waits_for_ssdp_readiness_and_propagates_failure(
    monkeypatch,
) -> None:
    class Config:
        def get(self, key: str, default=None):  # noqa: ANN001, ANN202
            values = {
                "friendly_name": "Test Renderer",
                "udn": "uuid:test-device",
                "http_port": 12345,
            }
            return values.get(key, default)

    class Bridge:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def set_services(self, *_services) -> None:  # noqa: ANN002
            pass

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    class UpnpServer:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            self._device = SimpleNamespace(services={})
            self.base_uri = "http://192.0.2.10:12345"
            self.stop_calls = 0

        async def async_start(self) -> None:
            pass

        async def async_stop(self) -> None:
            self.stop_calls += 1

    class FailingListener:
        instance = None

        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            self.started = False
            self.waited = False
            self.stopped = False
            FailingListener.instance = self

        def start(self) -> None:
            self.started = True

        def wait_until_ready(self) -> None:
            self.waited = True
            raise RuntimeError("SSDP startup failed")

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(server_module, "_patch_upnp_server_skip_ssdp", lambda: None)
    monkeypatch.setattr(server_module, "make_device_class", lambda *_args: object)
    monkeypatch.setattr(server_module, "UpnpServer", UpnpServer)
    monkeypatch.setattr(server_module, "SsdpListener", FailingListener)

    bridge = Bridge()
    server = DlnaServer(bridge, Config())  # type: ignore[arg-type]

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="SSDP startup failed"):
            await server.async_start()

        listener = FailingListener.instance
        assert listener is not None
        assert listener.started is True
        assert listener.waited is True
        assert server.running is False

        # 与应用统一失败清理链路相同：启动异常后仍能回收部分 HTTP/SSDP 资源。
        upstream = server._server  # noqa: SLF001
        await server.async_stop()
        assert listener.stopped is True
        assert upstream is not None and upstream.stop_calls == 1

    asyncio.run(scenario())
    assert bridge.shutdown_calls == 1


def test_dlna_stop_timeout_preserves_ssdp_reference_for_retry() -> None:
    class Listener:
        def stop(self) -> None:
            raise TimeoutError("thread still alive")

    class Bridge:
        def shutdown(self) -> None:
            raise AssertionError("HTTP/bridge cleanup must wait for SSDP stop")

    listener = Listener()
    server = DlnaServer.__new__(DlnaServer)
    server._running = True
    server._server = object()
    server._ssdp = listener
    server._bridge = Bridge()

    with pytest.raises(TimeoutError, match="thread still alive"):
        asyncio.run(server.async_stop())

    assert server._ssdp is listener
    assert server._running is True
