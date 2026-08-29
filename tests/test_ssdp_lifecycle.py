from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from ydlna.dlna import server as server_module
from ydlna.dlna.server import DlnaServer
from ydlna.dlna.ssdp_listener import SsdpListener, filter_interfaces


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


def test_ssdp_stop_prevents_waiting_alive_after_byebye(monkeypatch) -> None:
    listener = _listener()

    class Sender:
        ip = "192.0.2.10"

        def __init__(self) -> None:
            self.packets: list[bytes] = []

        def send(self, data: bytes) -> None:
            self.packets.append(data)

        def close(self) -> None:
            pass

    sender = Sender()
    listener._senders = [sender]  # type: ignore[list-item]  # noqa: SLF001

    stop_has_lock = threading.Event()
    release_stop = threading.Event()
    notify_attempting = threading.Event()
    errors: list[BaseException] = []
    original_build_notify = listener._build_notify  # noqa: SLF001

    def gated_build_notify(st: str, alive: bool) -> bytes:
        if not alive and not stop_has_lock.is_set():
            stop_has_lock.set()
            if not release_stop.wait(timeout=1.0):
                raise TimeoutError("test did not release stop")
        return original_build_notify(st, alive)

    def run_stop() -> None:
        try:
            listener.stop(timeout=1.0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def run_notify() -> None:
        notify_attempting.set()
        try:
            listener._notify_alive()  # noqa: SLF001
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    monkeypatch.setattr(listener, "_build_notify", gated_build_notify)
    stop_thread = threading.Thread(target=run_stop)
    notify_thread = threading.Thread(target=run_notify)

    stop_thread.start()
    assert stop_has_lock.wait(timeout=1.0)
    notify_thread.start()
    assert notify_attempting.wait(timeout=1.0)
    release_stop.set()
    stop_thread.join(timeout=1.0)
    notify_thread.join(timeout=1.0)

    assert not stop_thread.is_alive()
    assert not notify_thread.is_alive()
    assert errors == []
    assert any(b"NTS: ssdp:byebye" in packet for packet in sender.packets)
    assert not any(b"NTS: ssdp:alive" in packet for packet in sender.packets)


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


def test_dlna_start_ssdp_wait_does_not_block_event_loop(monkeypatch) -> None:
    entered_wait = threading.Event()
    release_wait = threading.Event()

    class Config:
        def get(self, key: str, default=None):  # noqa: ANN001, ANN202
            values = {
                "friendly_name": "Test Renderer",
                "udn": "uuid:test-device",
                "http_port": 12345,
            }
            return values.get(key, default)

    class Bridge:
        def set_services(self, *_services) -> None:  # noqa: ANN002
            pass

        def shutdown(self) -> None:
            pass

    class UpnpServer:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            self._device = SimpleNamespace(services={})
            self.base_uri = "http://192.0.2.10:12345"

        async def async_start(self) -> None:
            pass

        async def async_stop(self) -> None:
            pass

    class SlowListener:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            pass

        def start(self) -> None:
            pass

        def wait_until_ready(self) -> None:
            entered_wait.set()
            release_wait.wait(timeout=1.0)

        def stop(self) -> None:
            pass

    monkeypatch.setattr(server_module, "_patch_upnp_server_skip_ssdp", lambda: None)
    monkeypatch.setattr(server_module, "make_device_class", lambda *_args: object)
    monkeypatch.setattr(server_module, "UpnpServer", UpnpServer)
    monkeypatch.setattr(server_module, "SsdpListener", SlowListener)

    server = DlnaServer(Bridge(), Config())  # type: ignore[arg-type]

    async def scenario() -> None:
        start_task = asyncio.create_task(server.async_start())
        try:
            for _ in range(50):
                if entered_wait.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered_wait.is_set()
            # wait_until_ready 仍在阻塞工作线程，但 asyncio/qasync 可以继续调度。
            assert not start_task.done()
        finally:
            release_wait.set()
        await start_task
        await server.async_stop()

    asyncio.run(scenario())


def test_dlna_stop_join_does_not_block_event_loop() -> None:
    entered_stop = threading.Event()
    release_stop = threading.Event()

    class Listener:
        def stop(self) -> None:
            entered_stop.set()
            release_stop.wait(timeout=1.0)

    class UpnpServer:
        async def async_stop(self) -> None:
            pass

    class Bridge:
        def shutdown(self) -> None:
            pass

    server = DlnaServer.__new__(DlnaServer)
    server._running = True
    server._server = UpnpServer()
    server._ssdp = Listener()
    server._bridge = Bridge()

    async def scenario() -> None:
        stop_task = asyncio.create_task(server.async_stop())
        try:
            for _ in range(50):
                if entered_stop.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered_stop.is_set()
            # stop/join 仍在阻塞工作线程，但 asyncio/qasync 可以继续调度。
            assert not stop_task.done()
        finally:
            release_stop.set()
        await stop_task

    asyncio.run(scenario())
    assert server._ssdp is None
    assert server._running is False


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


# ---------------------------------------------------------------------- #
# 仅默认网卡模式：网卡白名单过滤与 HTTP 绑定范围
# ---------------------------------------------------------------------- #
def test_filter_interfaces_whitelist() -> None:
    ips = [
        ("192.168.1.10", "255.255.255.0"),
        ("10.8.0.2", "255.255.255.0"),
        ("192.168.137.1", "255.255.255.0"),
    ]
    # 未启用白名单：原样返回
    assert filter_interfaces(ips, None) == ips

    filtered = filter_interfaces(ips, ["192.168.1.10"])
    assert filtered == [("192.168.1.10", "255.255.255.0")]

    # 白名单 IP 被网卡枚举漏掉（如 ICS 兜底段）时补默认掩码，
    # 保证设备至少能在指定网卡上宣告
    assert filter_interfaces([], ["192.0.2.10"]) == [("192.0.2.10", "255.255.255.0")]


def test_listener_stores_allowed_ips() -> None:
    unrestricted = _listener()
    assert unrestricted._allowed_ips is None  # noqa: SLF001
    restricted = SsdpListener(
        udn="uuid:test-device",
        http_port=12345,
        server_id="Test/1.0 UPnP/1.1 LightCast/test",
        allowed_ips=["192.0.2.10"],
    )
    assert restricted._allowed_ips == ["192.0.2.10"]  # noqa: SLF001


def test_msearch_iface_choice_stays_within_whitelist() -> None:
    listener = SsdpListener(
        udn="uuid:test-device",
        http_port=12345,
        server_id="Test/1.0 UPnP/1.1 LightCast/test",
        allowed_ips=["192.168.1.10"],
    )
    # 模拟 _setup_socket 用 filter_interfaces 过滤后的结果（不建真实 socket）
    listener._ips = filter_interfaces(  # noqa: SLF001
        [("192.168.1.10", "255.255.255.0"), ("10.8.0.2", "255.255.255.0")],
        ["192.168.1.10"],
    )
    # 与白名单同子网的请求方正常命中
    assert listener._choose_iface_for("192.168.1.77") == "192.168.1.10"
    # 异网段请求方的 fallback 也只能落在白名单内
    assert listener._choose_iface_for("10.9.9.9") == "192.168.1.10"


def test_dlna_start_bind_scope_follows_config(monkeypatch) -> None:
    """「仅默认网卡」开关决定 HTTP 绑定地址与 SSDP 宣告白名单。"""
    captured: dict[str, object] = {}

    class Config:
        def __init__(self) -> None:
            self.values = {
                "friendly_name": "Test Renderer",
                "udn": "uuid:test-device",
                "http_port": 12345,
            }

        def get(self, key: str, default=None):  # noqa: ANN001, ANN202
            return self.values.get(key, default)

    class Bridge:
        def set_services(self, *_services) -> None:  # noqa: ANN002
            pass

        def shutdown(self) -> None:
            pass

    class UpnpServer:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            captured["source"] = kwargs.get("source")
            self._device = SimpleNamespace(services={})
            self.base_uri = "http://192.0.2.10:12345"

        async def async_start(self) -> None:
            pass

        async def async_stop(self) -> None:
            pass

    class FakeListener:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            captured["allowed_ips"] = kwargs.get("allowed_ips")

        def start(self) -> None:
            pass

        def wait_until_ready(self) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(server_module, "_patch_upnp_server_skip_ssdp", lambda: None)
    monkeypatch.setattr(server_module, "make_device_class", lambda *_args: object)
    monkeypatch.setattr(server_module, "UpnpServer", UpnpServer)
    monkeypatch.setattr(server_module, "SsdpListener", FakeListener)
    monkeypatch.setattr(server_module, "get_local_ip", lambda: "192.0.2.10")

    # 默认（关闭）：绑全部网卡，SSDP 不做网卡过滤
    server = DlnaServer(Bridge(), Config())  # type: ignore[arg-type]
    asyncio.run(server.async_start())
    assert captured["source"] == ("0.0.0.0", 1900)
    assert captured["allowed_ips"] is None

    # 开启：HTTP 只绑默认网卡 IP，SSDP 宣告白名单同步收窄
    captured.clear()
    cfg = Config()
    cfg.values["bind_default_interface_only"] = True
    server = DlnaServer(Bridge(), cfg)  # type: ignore[arg-type]
    asyncio.run(server.async_start())
    assert captured["source"] == ("192.0.2.10", 1900)
    assert captured["allowed_ips"] == ["192.0.2.10"]
