from __future__ import annotations

import asyncio

from ydlna.dlna.renderer_bridge import RendererBridge
from ydlna.player._url_guard import UrlBlockedError


class _FakePlayer:
    def __init__(self) -> None:
        self.played: list[tuple[str, str]] = []

    def play(self, url: str, title: str) -> None:
        self.played.append((url, title))


def _bridge_with_setup(setup):  # noqa: ANN001, ANN202
    bridge = RendererBridge.__new__(RendererBridge)
    bridge._player = _FakePlayer()
    bridge.on_cast_started = None
    bridge._allow_intranet = lambda: True
    bridge._setup_proxy = setup
    states: list[str] = []
    bridge._set_transport_state = states.append
    return bridge, states


def test_literal_block_is_never_sent_to_mpv() -> None:
    async def setup(_url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        raise AssertionError("blocked URL must not reach proxy setup")

    bridge, states = _bridge_with_setup(setup)
    asyncio.run(bridge.on_set_uri("http://127.0.0.1/private", ""))
    assert bridge._player.played == []
    assert states == ["ERROR_OCCURRED"]


def test_proxy_failure_does_not_fallback_to_raw_upstream_url() -> None:
    async def setup(_url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        return None

    bridge, states = _bridge_with_setup(setup)
    asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))
    assert bridge._player.played == []
    assert states == ["ERROR_OCCURRED"]


def test_connector_policy_failure_does_not_fallback_to_mpv() -> None:
    async def setup(url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        raise UrlBlockedError(url, "DNS 解析结果指向回环地址")

    bridge, states = _bridge_with_setup(setup)
    asyncio.run(bridge.on_set_uri("https://rebinding.example/video.mp4", ""))
    assert bridge._player.played == []
    assert states == ["ERROR_OCCURRED"]


def test_only_local_proxy_url_is_sent_to_mpv() -> None:
    async def setup(_url: str, *, allow_intranet: bool):  # noqa: ARG001, ANN202
        return "http://127.0.0.1:54321/stream"

    bridge, states = _bridge_with_setup(setup)
    asyncio.run(bridge.on_set_uri("https://public.example/video.mp4", ""))
    assert bridge._player.played == [
        ("http://127.0.0.1:54321/stream", "https://public.example/video.mp4")
    ]
    assert states == []
