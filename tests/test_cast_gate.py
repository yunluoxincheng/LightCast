"""控制点授权与投屏确认门控（局域网任意投屏防护）回归测试。

覆盖 RendererBridge 的按控制点 IP 授权模型：
- 首次来自新控制点的 SetAVTransportURI 弹窗确认，同意后授权该控制点；
- 授权控制点的全部状态变更 action（Play/Pause/Stop/Seek/Volume/Mute）放行；
- 未授权 / 未知控制点的任何状态变更 action 一律拒绝（fail-closed）；
- 媒体 URL host 不作为设备身份：不同控制点投同一 CDN 域名仍需分别确认；
- 被安全策略拦截的 URL 不触发弹窗；配置关闭时进入无鉴权模式。
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from async_upnp_client.exceptions import UpnpActionError

from ydlna.config import Config as ConfigClass
from ydlna.config import DEFAULTS
from ydlna.dlna._control_context import (
    current_controller_ip,
    reset_controller_ip,
    set_controller_ip,
)
from ydlna.dlna.renderer_bridge import RendererBridge

CONTROLLER_A = "192.168.1.20"
CONTROLLER_B = "192.168.1.77"


@contextmanager
def _controller(ip: str | None):
    token = set_controller_ip(ip)
    try:
        yield
    finally:
        reset_controller_ip(token)


class _FakePlayer:
    def __init__(self) -> None:
        self.played: list[tuple[str, str]] = []
        self.paused: list[bool] = []
        self.stopped = 0
        self.seeks: list[float] = []
        self.volumes: list[int] = []
        self.mutes: list[bool] = []
        self.state = "stopped"
        self.url = ""

    def play(self, url: str, title: str) -> None:
        self.played.append((url, title))
        self.state = "playing"
        self.url = url

    def set_paused(self, paused: bool) -> None:
        self.paused.append(paused)

    def stop(self) -> None:
        self.stopped += 1

    def seek(self, seconds: float) -> None:
        self.seeks.append(seconds)

    def set_volume(self, volume: int) -> None:
        self.volumes.append(volume)

    def set_mute(self, muted: bool) -> None:
        self.mutes.append(muted)

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
            "RelativeTimePosition": _StateVariable("NOT_IMPLEMENTED"),
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
    """构造带门控的 bridge；gate=None 表示未注入（装配缺失，fail-closed）。

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
    bridge._poll_task = None
    bridge._hls_proxy = None
    bridge._direct_proxy = None
    bridge.on_cast_started = None
    bridge._allow_intranet = lambda: True
    bridge._require_cast_confirm = lambda: require_confirm
    bridge.cast_gate = gate
    bridge._confirmed_controllers = set()

    candidate = _FakeProxy("http://127.0.0.1:54321/stream")

    async def setup_candidate(url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        attr = "_hls_proxy" if ".m3u8" in url.lower() else "_direct_proxy"
        return attr, candidate

    bridge._setup_proxy_candidate = setup_candidate
    return bridge


def test_default_config_enables_cast_confirmation() -> None:
    assert DEFAULTS["require_cast_confirm"] is True
    assert DEFAULTS["bind_default_interface_only"] is False


def test_require_cast_confirm_reads_config(monkeypatch) -> None:
    cfg = _patch_config(monkeypatch)
    probe = RendererBridge.__new__(RendererBridge)
    # 未配置时默认开启
    assert probe._require_cast_confirm() is True
    cfg.values["require_cast_confirm"] = False
    assert probe._require_cast_confirm() is False


def test_first_cast_from_new_controller_requires_confirmation() -> None:
    gate_calls: list[tuple[str, str, str]] = []
    ui_calls: list[int] = []

    async def gate(controller: str, url: str, title: str) -> bool:
        gate_calls.append((controller, url, title))
        return True

    bridge = _gated_bridge(gate)
    bridge.on_cast_started = lambda: ui_calls.append(1)
    with _controller(CONTROLLER_A):
        asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))

    assert gate_calls == [
        (CONTROLLER_A, "https://public.example/video.mp4", "https://public.example/video.mp4")
    ]
    assert ui_calls == [1]
    assert bridge._player.played != []
    assert bridge._confirmed_controllers == {CONTROLLER_A}
    assert bridge._avt.state_variable("TransportStatus").value == "OK"


def test_gate_receives_didl_title() -> None:
    gate_calls: list[tuple[str, str, str]] = []

    async def gate(controller: str, url: str, title: str) -> bool:  # noqa: ARG001
        gate_calls.append((controller, url, title))
        return True

    bridge = _gated_bridge(gate)
    meta = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<item><dc:title>番剧 第01话</dc:title></item></DIDL-Lite>"
    )
    with _controller(CONTROLLER_A):
        asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", meta))
    assert gate_calls == [("192.168.1.20", "https://public.example/video.mp4", "番剧 第01话")]


def test_authorized_controller_casts_again_without_dialog() -> None:
    gate_calls: list[tuple[str, str, str]] = []

    async def gate(controller: str, url: str, title: str) -> bool:
        gate_calls.append((controller, url, title))
        return True

    bridge = _gated_bridge(gate)
    with _controller(CONTROLLER_A):
        asyncio.run(bridge.on_set_uri("https://public.example/first.mp4", ""))
        asyncio.run(bridge.on_set_uri("https://public.example/second.mp4", ""))

    assert len(gate_calls) == 1
    assert len(bridge._player.played) == 2


def test_same_media_host_from_other_controller_still_requires_confirmation() -> None:
    """授权串用回归：控制点 B 投控制点 A 已确认过的同一 CDN 域名，必须再弹窗。"""
    gate_calls: list[str] = []

    async def gate(controller: str, url: str, title: str) -> bool:  # noqa: ARG001
        gate_calls.append(controller)
        return True

    bridge = _gated_bridge(gate)
    with _controller(CONTROLLER_A):
        asyncio.run(bridge.on_set_uri("https://cdn.example.com/video.mp4", ""))
    with _controller(CONTROLLER_B):
        asyncio.run(bridge.on_set_uri("https://cdn.example.com/other.mp4", ""))

    # B 投的是与 A 完全相同的来源域名，也必须经过确认
    assert gate_calls == [CONTROLLER_A, CONTROLLER_B]
    assert bridge._confirmed_controllers == {CONTROLLER_A, CONTROLLER_B}


def test_rejected_cast_sets_error_and_keeps_controller_unauthorized() -> None:
    gate_calls: list[str] = []
    ui_calls: list[int] = []

    async def gate(controller: str, url: str, title: str) -> bool:  # noqa: ARG001
        gate_calls.append(controller)
        return False

    bridge = _gated_bridge(gate)
    bridge.on_cast_started = lambda: ui_calls.append(1)
    with _controller(CONTROLLER_A):
        with pytest.raises(UpnpActionError):
            asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))

    assert gate_calls == [CONTROLLER_A]
    assert ui_calls == []
    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"
    assert bridge._avt.state_variable("TransportState").value == "STOPPED"
    assert bridge._avt.state_variable("AVTransportURI").value == "https://previous.example/video.mp4"
    # 拒绝后该控制点不得获得任何播放控制权
    with _controller(CONTROLLER_A):
        with pytest.raises(UpnpActionError):
            bridge.on_play()


def test_gate_exception_rejects_cast() -> None:
    async def gate(controller: str, url: str, title: str) -> bool:  # noqa: ARG001
        raise RuntimeError("dialog crashed")

    bridge = _gated_bridge(gate)
    with _controller(CONTROLLER_A):
        with pytest.raises(UpnpActionError):
            asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))

    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"
    # 异常按拒绝处理：控制点不会被授权
    assert bridge._confirmed_controllers == set()


def test_missing_controller_ip_is_fail_closed() -> None:
    async def gate(controller: str, url: str, title: str) -> bool:  # noqa: ARG001
        raise AssertionError("未知控制点不应触发确认弹窗")

    bridge = _gated_bridge(gate)
    with _controller(None):
        with pytest.raises(UpnpActionError):
            asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))

    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"
    assert bridge._confirmed_controllers == set()


def test_missing_gate_injection_is_fail_closed() -> None:
    bridge = _gated_bridge(None)
    with _controller(CONTROLLER_A):
        with pytest.raises(UpnpActionError):
            asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))

    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"
    assert bridge._confirmed_controllers == set()


def test_blocked_url_never_triggers_gate() -> None:
    async def gate(controller: str, url: str, title: str) -> bool:  # noqa: ARG001
        raise AssertionError("被安全策略拦截的 URL 不应触发确认弹窗")

    bridge = _gated_bridge(gate)
    with _controller(CONTROLLER_A):
        asyncio.run(bridge.on_set_uri("http://127.0.0.1/private", ""))

    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"
    assert bridge._confirmed_controllers == set()


def test_confirmation_disabled_allows_everything(monkeypatch) -> None:
    _patch_config(monkeypatch, {"require_cast_confirm": False})
    gate_calls: list[str] = []

    async def gate(controller: str, url: str, title: str) -> bool:  # noqa: ARG001
        gate_calls.append(controller)
        return False

    bridge = _gated_bridge(gate, require_confirm=False)
    with _controller(CONTROLLER_A):
        asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))
        # 无鉴权模式下未授权控制点也能控制播放
        bridge.on_play()
    with _controller(None):
        bridge.on_set_volume(30)

    assert gate_calls == []
    assert len(bridge._player.played) == 1
    assert bridge._player.volumes == [30]


def test_state_changing_actions_reject_unauthorized_controller() -> None:
    """未授权控制点不能暂停 / 停止 / Seek / 调音量 / 静音。"""
    bridge = _gated_bridge(None)  # 未注入 gate：即使收到投屏也会 fail-closed
    bridge._confirmed_controllers = {CONTROLLER_A}

    for action in (
        lambda: bridge.on_play(),
        lambda: bridge.on_pause(),
        lambda: bridge.on_stop(),
        lambda: bridge.on_seek("REL_TIME", "00:00:10"),
        lambda: bridge.on_set_volume(100),
        lambda: bridge.on_set_mute(True),
    ):
        with _controller(CONTROLLER_B):
            with pytest.raises(UpnpActionError):
                action()

    assert bridge._player.paused == []
    assert bridge._player.stopped == 0
    assert bridge._player.seeks == []
    assert bridge._player.volumes == []
    assert bridge._player.mutes == []


def test_unknown_controller_actions_are_fail_closed() -> None:
    """SOAP 上下文缺失（拿不到控制点 IP）时状态变更 action 也拒绝。"""
    bridge = _gated_bridge(None)
    bridge._confirmed_controllers = {CONTROLLER_A}

    with _controller(None):
        with pytest.raises(UpnpActionError):
            bridge.on_play()
        with pytest.raises(UpnpActionError):
            bridge.on_set_volume(0)


def test_authorized_controller_controls_playback() -> None:
    bridge = _gated_bridge(None)
    bridge._confirmed_controllers = {CONTROLLER_A}

    with _controller(CONTROLLER_A):
        bridge.on_play()
        bridge.on_pause()
        bridge.on_seek("REL_TIME", "00:00:10")
        bridge.on_set_volume(40)
        bridge.on_set_mute(True)
        bridge.on_stop()

    assert bridge._player.paused == [False, True]
    assert bridge._player.seeks == [10.0]
    assert bridge._player.volumes == [40]
    assert bridge._player.mutes == [True]
    assert bridge._player.stopped == 1
    # 控制点 B 即使 A 已授权也不能控制
    with _controller(CONTROLLER_B):
        with pytest.raises(UpnpActionError):
            bridge.on_set_volume(0)


def test_contextvar_isolates_requests() -> None:
    """server 层按请求登记的上下文不能串到其他请求。"""
    assert current_controller_ip() is None
    with _controller(CONTROLLER_A):
        assert current_controller_ip() == CONTROLLER_A
        with _controller(CONTROLLER_B):
            assert current_controller_ip() == CONTROLLER_B
        assert current_controller_ip() == CONTROLLER_A
    assert current_controller_ip() is None
