"""投屏确认门控（局域网任意投屏防护）回归测试。

覆盖 RendererBridge 的确认门控：同意 / 拒绝 / 门控异常、同来源主机的
会话记忆、被安全策略拦截的 URL 不触发弹窗、配置关闭与未注入门控时的
降级路径，以及 url_source_host 的来源主机提取规则。
"""
from __future__ import annotations

import asyncio

import pytest

from ydlna.config import Config as ConfigClass
from ydlna.config import DEFAULTS
from ydlna.dlna.renderer_bridge import RendererBridge, url_source_host


class _FakePlayer:
    def __init__(self) -> None:
        self.played: list[tuple[str, str]] = []
        self.state = "stopped"
        self.url = ""

    def play(self, url: str, title: str) -> None:
        self.played.append((url, title))
        self.state = "playing"
        self.url = url

    def get_state(self) -> str:
        return self.state

    def get_url(self) -> str:
        return self.url


class _StateVariable:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeProxy:
    def __init__(self, playlist_url: str) -> None:
        self.playlist_url = playlist_url
        self.running = True
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False


class _AvTransport:
    def __init__(self, uri: str = "", meta: str = "") -> None:
        self.variables = {
            "TransportState": _StateVariable("STOPPED"),
            "TransportStatus": _StateVariable("OK"),
            "AVTransportURI": _StateVariable(uri),
            "AVTransportURIMetaData": _StateVariable(meta),
            "CurrentTrackURI": _StateVariable(uri),
            "CurrentTrackMetaData": _StateVariable(meta),
            "CurrentTrack": _StateVariable(1 if uri else 0),
            "NumberOfTracks": _StateVariable(1 if uri else 0),
        }

    def state_variable(self, name: str) -> _StateVariable:
        return self.variables[name]


class _FakeConfig:
    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}

    def get(self, key: str, default=None):  # noqa: ANN001, ANN202
        return self.values.get(key, default)


def _patch_config(monkeypatch, values: dict | None = None) -> _FakeConfig:
    """把 Config.instance() 指向受控 fake（bridge 在调用点才 import）。"""
    cfg = _FakeConfig(values)
    monkeypatch.setattr(
        ConfigClass, "instance", classmethod(lambda cls: cfg)
    )
    return cfg


def _gated_bridge(gate=None, *, require_confirm: bool = True):  # noqa: ANN001, ANN202
    """构造带门控的 bridge；gate=None 表示未注入（降级路径）。

    ``require_confirm`` 直接注入「投屏需本机确认」的开关值；配置读取到
    bool 的接线由 test_require_cast_confirm_reads_config 单独覆盖
    （与现有安全测试一致，不触碰真实 Config 单例）。
    """
    previous_uri = "https://previous.example/video.mp4"
    previous_meta = "<DIDL-Lite>previous</DIDL-Lite>"
    bridge = RendererBridge.__new__(RendererBridge)
    bridge._player = _FakePlayer()
    bridge._avt = _AvTransport(previous_uri, previous_meta)
    bridge._set_uri_lock = asyncio.Lock()
    bridge._transport_status = "OK"
    bridge._current_uri = previous_uri
    bridge._current_meta = previous_meta
    bridge._hls_proxy = None
    bridge._direct_proxy = None
    bridge.on_cast_started = None
    bridge._allow_intranet = lambda: True
    bridge._require_cast_confirm = lambda: require_confirm
    bridge.cast_gate = gate
    bridge._confirmed_hosts = set()

    candidate = _FakeProxy("http://127.0.0.1:54321/stream")

    async def setup_candidate(url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        attr = "_hls_proxy" if ".m3u8" in url.lower() else "_direct_proxy"
        return attr, candidate

    bridge._setup_proxy_candidate = setup_candidate
    return bridge


def test_default_config_enables_cast_confirmation() -> None:
    assert DEFAULTS["require_cast_confirm"] is True
    assert DEFAULTS["bind_default_interface_only"] is False


def test_first_cast_from_new_host_requires_confirmation() -> None:
    gate_calls: list[tuple[str, str]] = []
    ui_calls: list[int] = []

    async def gate(url: str, title: str) -> bool:
        gate_calls.append((url, title))
        return True

    bridge = _gated_bridge(gate)
    bridge.on_cast_started = lambda: ui_calls.append(1)
    asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))

    assert gate_calls == [("https://public.example/video.mp4", "https://public.example/video.mp4")]
    assert ui_calls == [1]
    assert bridge._player.played != []
    assert bridge._confirmed_hosts == {"public.example"}
    assert bridge._avt.state_variable("TransportStatus").value == "OK"


def test_gate_receives_didl_title() -> None:
    gate_calls: list[tuple[str, str]] = []

    async def gate(url: str, title: str) -> bool:  # noqa: ARG001
        gate_calls.append((url, title))
        return True

    bridge = _gated_bridge(gate)
    meta = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<item><dc:title>番剧 第01话</dc:title></item></DIDL-Lite>"
    )
    asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", meta))
    assert gate_calls == [("https://public.example/video.mp4", "番剧 第01话")]


def test_rejected_cast_sets_error_and_keeps_previous_media() -> None:
    gate_calls: list[tuple[str, str]] = []
    ui_calls: list[int] = []

    async def gate(url: str, title: str) -> bool:  # noqa: ARG001
        gate_calls.append((url, title))
        return False

    bridge = _gated_bridge(gate)
    bridge.on_cast_started = lambda: ui_calls.append(1)
    asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))

    assert len(gate_calls) == 1
    assert ui_calls == []
    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"
    assert bridge._avt.state_variable("TransportState").value == "STOPPED"
    assert bridge._avt.state_variable("AVTransportURI").value == "https://previous.example/video.mp4"


def test_gate_exception_rejects_cast() -> None:
    async def gate(url: str, title: str) -> bool:  # noqa: ARG001
        raise RuntimeError("dialog crashed")

    bridge = _gated_bridge(gate)
    asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))

    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"
    # 异常按拒绝处理：来源主机不会被记入会话集合
    assert bridge._confirmed_hosts == set()


def test_same_host_second_cast_skips_gate() -> None:
    gate_calls: list[tuple[str, str]] = []

    async def gate(url: str, title: str) -> bool:  # noqa: ARG001
        gate_calls.append((url, title))
        return True

    bridge = _gated_bridge(gate)
    asyncio.run(bridge.on_set_uri("https://public.example/first.mp4", ""))
    asyncio.run(bridge.on_set_uri("https://public.example/second.mp4", ""))

    assert len(gate_calls) == 1
    assert len(bridge._player.played) == 2


def test_same_host_with_explicit_port_is_a_different_source() -> None:
    gate_calls: list[tuple[str, str]] = []

    async def gate(url: str, title: str) -> bool:  # noqa: ARG001
        gate_calls.append((url, title))
        return True

    bridge = _gated_bridge(gate)
    asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))
    asyncio.run(bridge.on_set_uri("https://public.example:8443/video.mp4", ""))

    assert len(gate_calls) == 2
    assert bridge._confirmed_hosts == {"public.example", "public.example:8443"}


def test_blocked_url_never_triggers_gate() -> None:
    async def gate(url: str, title: str) -> bool:  # noqa: ARG001
        raise AssertionError("被安全策略拦截的 URL 不应触发确认弹窗")

    bridge = _gated_bridge(gate)
    asyncio.run(bridge.on_set_uri("http://127.0.0.1/private", ""))

    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"
    assert bridge._confirmed_hosts == set()


def test_gate_disabled_when_confirmation_off() -> None:
    gate_calls: list[tuple[str, str]] = []

    async def gate(url: str, title: str) -> bool:  # noqa: ARG001
        gate_calls.append((url, title))
        return False

    bridge = _gated_bridge(gate, require_confirm=False)
    asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))

    assert gate_calls == []
    assert len(bridge._player.played) == 1


def test_require_cast_confirm_reads_config(monkeypatch) -> None:
    cfg = _patch_config(monkeypatch)
    probe = RendererBridge.__new__(RendererBridge)
    # 未配置时默认开启
    assert probe._require_cast_confirm() is True
    cfg.values["require_cast_confirm"] = False
    assert probe._require_cast_confirm() is False


def test_no_gate_injected_allows_cast() -> None:
    bridge = _gated_bridge(None)
    asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))

    assert len(bridge._player.played) == 1
    assert bridge._avt.state_variable("TransportStatus").value == "OK"


def test_url_source_host_rules() -> None:
    assert url_source_host("https://Public.Example/video.mp4") == "public.example"
    assert url_source_host("https://public.example:8443/v.mp4") == "public.example:8443"
    assert url_source_host("http://user:pass@192.168.1.5:9000/v.mp4") == "192.168.1.5:9000"
    assert url_source_host("https://[2001:db8::1]:8080/v.mp4") == "[2001:db8::1]:8080"
    # 非法 / 无 host 的输入按原串返回，门控仍能按整体 URL 记忆
    assert url_source_host("not a url") == "not a url"
    assert url_source_host("http:///path") == "http:///path"
