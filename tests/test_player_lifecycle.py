from __future__ import annotations

from types import SimpleNamespace

from ydlna.dlna.renderer_bridge import RendererBridge
from ydlna.player import mpv_player


class _FakeMpv:
    def __init__(self) -> None:
        self.observers = {}
        self.callbacks = {}
        self.terminated = False
        self.volume = 0
        self.mute = False
        self.speed = 1.0
        self.audio_device = ""

    def property_observer(self, name: str):  # noqa: ANN201
        def decorator(callback):  # noqa: ANN001, ANN202
            self.observers[name] = callback
            return callback

        return decorator

    def event_callback(self, name: str):  # noqa: ANN201
        def decorator(callback):  # noqa: ANN001, ANN202
            self.callbacks[name] = callback
            return callback

        return decorator

    def play(self, _url: str) -> None:
        pass

    def terminate(self) -> None:
        # python-mpv terminate 会等待事件线程；模拟终止过程中到达的最后一批回调。
        self.observers["time-pos"]("time-pos", 999.0)
        self.observers["duration"]("duration", 999.0)
        self.callbacks["file-loaded"](None)
        self.terminated = True


def test_player_shutdown_blocks_late_mpv_callbacks(monkeypatch) -> None:
    fake_mpv = _FakeMpv()
    monkeypatch.setattr(mpv_player, "_MPV_AVAILABLE", True)
    monkeypatch.setattr(
        mpv_player,
        "mpv",
        SimpleNamespace(MPV=lambda **_kwargs: fake_mpv),
    )

    player = mpv_player.Player()
    positions: list[float] = []
    media: list[tuple[str, str]] = []
    states: list[str] = []
    player.signals.positionChanged.connect(positions.append)
    player.signals.mediaChanged.connect(lambda title, url: media.append((title, url)))
    player.signals.stateChanged.connect(states.append)

    player.attach(123)
    player.play("http://127.0.0.1/media", "Test media")
    fake_mpv.observers["time-pos"]("time-pos", 12.5)
    fake_mpv.observers["duration"]("duration", 60.0)
    fake_mpv.callbacks["file-loaded"](None)

    assert player.get_position() == 12.5
    assert player.get_duration() == 60.0
    assert player.get_state() == "playing"
    before_shutdown = (list(positions), list(media), list(states))

    player.shutdown()

    assert fake_mpv.terminated is True
    assert player.available is False
    assert player._mpv is None  # noqa: SLF001
    assert player._shutting_down is True  # noqa: SLF001
    assert player.get_position() == 12.5
    assert player.get_duration() == 60.0
    assert (positions, media, states) == before_shutdown

    # 即使有回调在 terminate 返回后再次到达，也不能更新缓存或发 Qt 信号。
    fake_mpv.observers["time-pos"]("time-pos", 1000.0)
    fake_mpv.callbacks["file-loaded"](None)
    assert player.get_position() == 12.5
    assert (positions, media, states) == before_shutdown


class _Signal:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, slot) -> None:  # noqa: ANN001
        if slot not in self.slots:
            self.slots.append(slot)

    def disconnect(self, slot) -> None:  # noqa: ANN001
        self.slots.remove(slot)

    def emit(self, value: str) -> None:
        for slot in tuple(self.slots):
            slot(value)


class _StateVariable:
    def __init__(self, value) -> None:  # noqa: ANN001
        self.value = value


class _Service:
    def __init__(self, *, volume: int = 80) -> None:
        self.bridge = None
        self.variables = {
            "TransportState": _StateVariable("NO_MEDIA_PRESENT"),
            "RelativeTimePosition": _StateVariable("NOT_IMPLEMENTED"),
            "CurrentMediaDuration": _StateVariable("NOT_IMPLEMENTED"),
            "Volume": _StateVariable(volume),
        }

    def state_variable(self, name: str) -> _StateVariable:
        return self.variables[name]


def test_renderer_shutdown_disconnects_old_services_and_reconnects_on_restart() -> None:
    state_signal = _Signal()

    class Player:
        signals = SimpleNamespace(stateChanged=state_signal)

        def __init__(self) -> None:
            self.volume = 0

        def set_volume(self, value: int) -> None:
            self.volume = value

        def get_url(self) -> str:
            return ""

    player = Player()
    bridge = RendererBridge(player)  # type: ignore[arg-type]
    old_avt, old_rc, old_cm = _Service(), _Service(volume=55), _Service()
    bridge.set_services(old_avt, old_rc, old_cm)

    state_signal.emit("playing")
    assert old_avt.state_variable("TransportState").value == "PLAYING"
    assert player.volume == 55

    bridge.shutdown()

    assert state_signal.slots == []
    assert bridge._avt is None  # noqa: SLF001
    assert bridge._rc is None  # noqa: SLF001
    assert bridge._cm is None  # noqa: SLF001
    assert old_avt.bridge is None
    assert old_rc.bridge is None
    state_signal.emit("paused")
    assert old_avt.state_variable("TransportState").value == "PLAYING"

    new_avt, new_rc, new_cm = _Service(), _Service(volume=70), _Service()
    bridge.set_services(new_avt, new_rc, new_cm)
    assert len(state_signal.slots) == 1
    state_signal.emit("paused")

    assert new_avt.state_variable("TransportState").value == "PAUSED_PLAYBACK"
    assert player.volume == 70
