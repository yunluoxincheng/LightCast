"""service 层授权测试：未授权请求不得产生任何可观察的状态副作用。

审查要求的用例——直接调用真实 service action（而非 bridge 方法）：
- 未授权控制点 SetVolume / SetMute → UpnpActionError，evented 状态与
  Player 均保持原值（回归：先写 service 状态再查授权的顺序缺陷）；
- 未授权控制点 SetAVTransportURI → 确认弹窗挂起期间 AVTransportURI 仍为
  上一媒体，拒绝后仍为上一媒体，且 action 以 SOAP fault（UpnpActionError）
  告终，控制点不会误以为投屏已被接受。
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from async_upnp_client.exceptions import UpnpActionError

from ydlna.dlna._control_context import (
    current_controller_ip,
    reset_controller_ip,
    set_controller_ip,
)
from ydlna.dlna.avtransport import AVTransportService
from ydlna.dlna.renderer_bridge import RendererBridge
from ydlna.dlna.rendering_control import RenderingControlService

CONTROLLER_A = "192.168.1.20"
CONTROLLER_B = "192.168.1.77"
PREVIOUS_URI = "https://previous.example/video.mp4"


@contextmanager
def _controller(ip: str | None):
    token = set_controller_ip(ip)
    try:
        yield
    finally:
        reset_controller_ip(token)


class _FakePlayer:
    def __init__(self) -> None:
        self.volumes: list[int] = []
        self.mutes: list[bool] = []
        self.played: list[tuple[str, str]] = []

    def set_volume(self, volume: int) -> None:
        self.volumes.append(volume)

    def set_mute(self, muted: bool) -> None:
        self.mutes.append(muted)

    def play(self, url: str, title: str) -> None:
        self.played.append((url, title))


class _FakeCandidate:
    playlist_url = "http://127.0.0.1:54321/stream"
    running = True

    async def stop(self) -> None:
        self.running = False


def _authorized_bridge(avt, *, gate=None) -> RendererBridge:  # noqa: ANN001
    """最小 bridge：授权路径 + 成功路径（代理已 stub）需要的属性。

    _avt 指向真实 service，evented 状态断言才有效。
    """
    bridge = RendererBridge.__new__(RendererBridge)
    bridge._player = _FakePlayer()
    bridge._avt = avt
    bridge._set_uri_lock = asyncio.Lock()
    bridge._transport_status = "OK"
    bridge._current_uri = PREVIOUS_URI
    bridge._current_meta = "<DIDL-Lite>previous</DIDL-Lite>"
    bridge.on_cast_started = None
    bridge._allow_intranet = lambda: True
    bridge._require_cast_confirm = lambda: True
    bridge.cast_gate = gate
    bridge._confirmed_controllers = {CONTROLLER_A}

    async def setup_candidate(url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        return "_direct_proxy", _FakeCandidate()

    bridge._setup_proxy_candidate = setup_candidate

    async def commit_candidate(attr: str, candidate) -> None:  # noqa: ANN001, ARG001, ANN202
        pass

    bridge._commit_proxy_candidate = commit_candidate
    return bridge


def test_unauthorized_set_volume_leaves_state_and_player_untouched() -> None:
    async def scenario() -> None:
        rc = RenderingControlService(object())
        bridge = _authorized_bridge(rc)
        rc.bridge = bridge

        with _controller(CONTROLLER_B):
            with pytest.raises(UpnpActionError):
                await rc.set_volume(0, "Master", 100)

        # evented 状态保持默认值，Player 也未被触达
        assert rc.state_variable("Volume").value == 80
        assert bridge._player.volumes == []

    asyncio.run(scenario())


def test_unauthorized_set_mute_leaves_state_and_player_untouched() -> None:
    async def scenario() -> None:
        rc = RenderingControlService(object())
        bridge = _authorized_bridge(rc)
        rc.bridge = bridge

        with _controller(CONTROLLER_B):
            with pytest.raises(UpnpActionError):
                await rc.set_mute(0, "Master", True)

        assert rc.state_variable("Mute").value is False
        assert bridge._player.mutes == []

    asyncio.run(scenario())


def test_authorized_controller_can_set_volume_and_mute() -> None:
    async def scenario() -> None:
        rc = RenderingControlService(object())
        bridge = _authorized_bridge(rc)
        rc.bridge = bridge

        with _controller(CONTROLLER_A):
            await rc.set_volume(0, "Master", 40)
            await rc.set_mute(0, "Master", True)

        assert rc.state_variable("Volume").value == 40
        assert rc.state_variable("Mute").value is True
        assert bridge._player.volumes == [40]
        assert bridge._player.mutes == [True]

    asyncio.run(scenario())


def test_unauthorized_set_uri_publishes_nothing_and_faults() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def gate(controller: str, url: str, title: str) -> bool:  # noqa: ARG001
        started.set()
        await release.wait()
        return False

    async def scenario() -> None:
        # 未授权控制点 B 发起投屏（控制点上下文由 server 层登记）
        set_controller_ip(CONTROLLER_B)
        avt = AVTransportService(object())
        bridge = _authorized_bridge(avt, gate=gate)
        avt.bridge = bridge
        # 上一份已提交媒体
        avt.state_variable("AVTransportURI").value = PREVIOUS_URI
        avt.state_variable("TransportState").value = "PLAYING"
        state_before = avt.state_variable("TransportState").value

        task = asyncio.create_task(
            avt.set_av_transport_uri(0, "https://attacker.example/v.mp4", "")
        )
        # 确认弹窗挂起期间：候选 URI 不得写入 evented 状态
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert avt.state_variable("AVTransportURI").value == PREVIOUS_URI
        assert avt.state_variable("TransportState").value == state_before

        release.set()
        with pytest.raises(UpnpActionError):
            await task

        # 拒绝后仍保持上一媒体，且以错误状态标记本次 action 失败
        assert avt.state_variable("AVTransportURI").value == PREVIOUS_URI
        assert avt.state_variable("TransportState").value == state_before
        assert avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"
        # 拒绝的控制点不会因此获得授权
        assert bridge._confirmed_controllers == {CONTROLLER_A}

    asyncio.run(scenario())


def test_authorized_set_uri_publishes_candidate_after_gate() -> None:
    """授权控制点（已确认）的 SetURI 正常走完整事务并提交媒体身份。"""
    async def scenario() -> None:
        avt = AVTransportService(object())
        bridge = _authorized_bridge(avt)
        avt.bridge = bridge
        avt.state_variable("AVTransportURI").value = PREVIOUS_URI

        with _controller(CONTROLLER_A):
            await avt.set_av_transport_uri(0, "https://public.example/new.mp4", "")

        assert bridge._current_uri == "https://public.example/new.mp4"
        assert avt.state_variable("AVTransportURI").value == (
            "https://public.example/new.mp4"
        )
        assert avt.state_variable("TransportStatus").value == "OK"

    asyncio.run(scenario())


def test_controller_context_isolated_per_request() -> None:
    assert current_controller_ip() is None
    with _controller(CONTROLLER_A):
        assert current_controller_ip() == CONTROLLER_A
    assert current_controller_ip() is None
