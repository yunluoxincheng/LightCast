from __future__ import annotations

import asyncio

import pytest

from ydlna.dlna._control_context import set_controller_ip
from ydlna.dlna.renderer_bridge import RendererBridge
from ydlna.player._url_guard import UrlBlockedError

# 测试预设的已授权控制点（聚焦 SSRF/代理语义，门控不会被触发）
CONTROLLER = "192.168.1.20"


def _set_uri(bridge: RendererBridge, url: str, meta: str = "") -> None:
    """以已授权控制点身份执行 SetAVTransportURI（登记 SOAP 上下文）。"""
    async def scenario() -> None:
        set_controller_ip(CONTROLLER)
        await bridge.on_set_uri(url, meta)

    asyncio.run(scenario())


class _FakePlayer:
    def __init__(self) -> None:
        self.played: list[tuple[str, str]] = []
        self.state = "stopped"
        self.url = ""
        self.raise_on_play = False

    def play(self, url: str, title: str) -> None:
        if self.raise_on_play:
            raise RuntimeError("mpv rejected candidate")
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
    def __init__(self, playlist_url: str, stop_hook=None) -> None:  # noqa: ANN001
        self.playlist_url = playlist_url
        self.running = True
        self.stop_calls = 0
        self._stop_hook = stop_hook

    async def stop(self) -> None:
        if self._stop_hook is not None:
            self._stop_hook()
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


def _bridge_with_setup(setup):  # noqa: ANN001, ANN202
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
    # 投屏确认门控：测试聚焦 SSRF/代理语义，预设为已授权控制点（见
    # _set_uri / CONTROLLER），门控不会被触发，也不触碰真实 Config 单例。
    bridge._require_cast_confirm = lambda: True
    bridge.cast_gate = None
    bridge._confirmed_controllers = {CONTROLLER}

    async def setup_candidate(url: str, *, allow_intranet: bool):  # noqa: ANN202
        result = await setup(url, allow_intranet=allow_intranet)
        if result is None:
            return None
        proxy = result if isinstance(result, _FakeProxy) else _FakeProxy(result)
        attr = "_hls_proxy" if ".m3u8" in url.lower() else "_direct_proxy"
        return attr, proxy

    bridge._setup_proxy_candidate = setup_candidate
    return bridge


def test_literal_block_is_never_sent_to_mpv() -> None:
    async def setup(_url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        raise AssertionError("blocked URL must not reach proxy setup")

    bridge = _bridge_with_setup(setup)
    _set_uri(bridge, "http://127.0.0.1/private")
    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportState").value == "STOPPED"
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"
    assert bridge._avt.state_variable("AVTransportURI").value == bridge._current_uri
    with pytest.raises(ValueError, match="非法 TransportState"):
        bridge._set_transport_state("ERROR_OCCURRED")


def test_proxy_failure_does_not_fallback_to_raw_upstream_url() -> None:
    async def setup(_url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        return None

    bridge = _bridge_with_setup(setup)
    _set_uri(bridge, "https://public.example/video.mp4")
    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportState").value == "STOPPED"
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"


def test_connector_policy_failure_does_not_fallback_to_mpv() -> None:
    async def setup(url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        raise UrlBlockedError(url, "DNS 解析结果指向回环地址")

    bridge = _bridge_with_setup(setup)
    _set_uri(bridge, "https://rebinding.example/video.mp4")
    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportState").value == "STOPPED"
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"


def test_unexpected_proxy_failure_sets_transport_status_only() -> None:
    async def setup(_url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        raise RuntimeError("proxy startup failed")

    bridge = _bridge_with_setup(setup)
    _set_uri(bridge, "https://public.example/video.mp4")
    assert bridge._player.played == []
    assert bridge._avt.state_variable("TransportState").value == "STOPPED"
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"


def test_only_local_proxy_url_is_sent_to_mpv() -> None:
    candidate = _FakeProxy("http://127.0.0.1:54321/stream")

    async def setup(_url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        return candidate

    bridge = _bridge_with_setup(setup)
    old_proxy = _FakeProxy(
        "http://127.0.0.1:50001/stream",
        stop_hook=lambda: bridge._player.played
        or pytest.fail("旧代理在 Player 接受 candidate 前被停止"),
    )
    bridge._direct_proxy = old_proxy
    bridge._player.state = "playing"
    bridge._player.url = old_proxy.playlist_url
    bridge._set_transport_status("ERROR_OCCURRED")
    _set_uri(bridge, "https://public.example/video.mp4")
    assert bridge._player.played == [
        ("http://127.0.0.1:54321/stream", "https://public.example/video.mp4")
    ]
    assert bridge._avt.state_variable("TransportState").value == "STOPPED"
    assert bridge._avt.state_variable("TransportStatus").value == "OK"
    assert bridge._current_uri == "https://public.example/video.mp4"
    assert bridge._avt.state_variable("AVTransportURI").value == bridge._current_uri
    assert bridge._direct_proxy is candidate
    assert candidate.stop_calls == 0
    assert old_proxy.stop_calls == 1


def test_proxy_setup_failure_keeps_previous_proxy_running() -> None:
    async def setup(_url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        return None

    bridge = _bridge_with_setup(setup)
    old_proxy = _FakeProxy("http://127.0.0.1:50001/stream")
    bridge._direct_proxy = old_proxy
    bridge._player.state = "playing"
    bridge._player.url = old_proxy.playlist_url

    _set_uri(bridge, "https://public.example/new.mp4")

    assert old_proxy.running is True
    assert old_proxy.stop_calls == 0
    assert bridge._direct_proxy is old_proxy
    assert bridge._player.url == old_proxy.playlist_url
    assert bridge._current_uri == "https://previous.example/video.mp4"
    assert bridge._avt.state_variable("AVTransportURI").value == bridge._current_uri
    assert bridge._avt.state_variable("TransportState").value == "PLAYING"
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"


def test_player_rejection_discards_candidate_and_keeps_previous_proxy() -> None:
    candidate = _FakeProxy("http://127.0.0.1:50002/stream")

    async def setup(_url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        return candidate

    bridge = _bridge_with_setup(setup)
    old_proxy = _FakeProxy("http://127.0.0.1:50001/stream")
    bridge._direct_proxy = old_proxy
    bridge._player.state = "playing"
    bridge._player.url = old_proxy.playlist_url
    bridge._player.raise_on_play = True

    _set_uri(bridge, "https://public.example/new.mp4")

    assert candidate.running is False
    assert candidate.stop_calls == 1
    assert old_proxy.running is True
    assert old_proxy.stop_calls == 0
    assert bridge._direct_proxy is old_proxy
    assert bridge._player.url == old_proxy.playlist_url
    assert bridge._current_uri == "https://previous.example/video.mp4"
    assert bridge._avt.state_variable("AVTransportURI").value == bridge._current_uri
    assert bridge._avt.state_variable("TransportState").value == "PLAYING"
    assert bridge._avt.state_variable("TransportStatus").value == "ERROR_OCCURRED"


def test_concurrent_set_uri_requests_are_serialized_and_latest_wins() -> None:
    async def scenario() -> None:
        set_controller_ip(CONTROLLER)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        setup_order: list[str] = []
        active = 0
        max_active = 0

        async def setup(url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            setup_order.append(url)
            try:
                if url.endswith("/first.mp4"):
                    first_started.set()
                    await release_first.wait()
                    return None
                return "http://127.0.0.1:54321/second"
            finally:
                active -= 1

        bridge = _bridge_with_setup(setup)
        first = asyncio.create_task(
            bridge.on_set_uri("https://public.example/first.mp4", "first")
        )
        await first_started.wait()
        second = asyncio.create_task(
            bridge.on_set_uri("https://public.example/second.mp4", "second")
        )
        await asyncio.sleep(0)

        # 第二个请求必须等待第一个完整事务退出，不能并发操作共享代理/状态。
        assert setup_order == ["https://public.example/first.mp4"]
        release_first.set()
        await asyncio.gather(first, second)

        assert max_active == 1
        assert setup_order == [
            "https://public.example/first.mp4",
            "https://public.example/second.mp4",
        ]
        assert bridge._player.played == [
            (
                "http://127.0.0.1:54321/second",
                "https://public.example/second.mp4",
            )
        ]
        assert bridge._current_uri == "https://public.example/second.mp4"
        assert bridge._avt.state_variable("AVTransportURI").value == (
            "https://public.example/second.mp4"
        )
        assert bridge._avt.state_variable("TransportStatus").value == "OK"

    asyncio.run(scenario())
