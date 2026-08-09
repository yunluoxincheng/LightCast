from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from ydlna.dlna.renderer_bridge import RendererBridge
from ydlna.player import mpv_player

_QT_APP: QCoreApplication | None = None


def _qt_app() -> QCoreApplication:
    global _QT_APP  # noqa: PLW0603
    if _QT_APP is None:
        _QT_APP = QCoreApplication.instance() or QCoreApplication([])
    return _QT_APP


def _process_events_until(predicate, timeout: float = 1.0) -> None:  # noqa: ANN001
    app = _qt_app()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


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
    _qt_app()
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
    _process_events_until(lambda: bool(media))

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
    _qt_app().processEvents()
    assert player.get_position() == 12.5
    assert (positions, media, states) == before_shutdown


def test_player_signals_dispatch_plain_python_receiver_on_qt_thread() -> None:
    """普通 function receiver 也必须在 PlayerSignals 所属 Qt 线程执行。"""
    app = _qt_app()
    signals = mpv_player.PlayerSignals()
    generation = signals.activate()
    main_thread_id = threading.get_ident()
    receiver_threads: list[int] = []
    receiver_states: list[str] = []
    worker_threads: list[int] = []

    def receiver(state: str) -> None:
        receiver_threads.append(threading.get_ident())
        receiver_states.append(state)

    signals.stateChanged.connect(receiver)

    def emit_from_worker() -> None:
        worker_threads.append(threading.get_ident())
        signals.post("state", "playing", generation)

    worker = threading.Thread(target=emit_from_worker)
    worker.start()
    worker.join()

    assert receiver_threads == []
    _process_events_until(lambda: bool(receiver_threads))
    assert worker_threads[0] != main_thread_id
    assert receiver_threads == [main_thread_id]
    assert receiver_states == ["playing"]
    assert signals.thread() == app.thread()

    # shutdown/重新 attach 间的旧世代排队事件不能在新世代复活。
    signals.post("state", "stale", generation)
    signals.deactivate()
    next_generation = signals.activate()
    signals.post("state", "paused", next_generation)
    _process_events_until(lambda: len(receiver_states) == 2)
    assert receiver_states == ["playing", "paused"]


class _Signal:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, slot) -> None:  # noqa: ANN001
        if slot not in self.slots:
            self.slots.append(slot)

    def disconnect(self, slot) -> None:  # noqa: ANN001
        self.slots.remove(slot)

    def emit(self, value) -> None:  # noqa: ANN001
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
            "TransportStatus": _StateVariable("OK"),
            "AVTransportURI": _StateVariable(""),
            "AVTransportURIMetaData": _StateVariable(""),
            "CurrentTrackURI": _StateVariable(""),
            "CurrentTrackMetaData": _StateVariable(""),
            "CurrentTrack": _StateVariable(0),
            "NumberOfTracks": _StateVariable(0),
            "RelativeTimePosition": _StateVariable("NOT_IMPLEMENTED"),
            "AbsoluteTimePosition": _StateVariable("NOT_IMPLEMENTED"),
            "CurrentMediaDuration": _StateVariable("NOT_IMPLEMENTED"),
            "CurrentTrackDuration": _StateVariable("NOT_IMPLEMENTED"),
            "Volume": _StateVariable(volume),
            "Mute": _StateVariable("0"),
        }

    def state_variable(self, name: str) -> _StateVariable:
        return self.variables[name]


def test_renderer_shutdown_disconnects_old_services_and_reconnects_on_restart() -> None:
    asyncio.run(_renderer_restart_scenario())


async def _renderer_restart_scenario() -> None:
    state_signal = _Signal()
    volume_signal = _Signal()
    mute_signal = _Signal()

    class Player:
        signals = SimpleNamespace(
            stateChanged=state_signal,
            volumeChanged=volume_signal,
            muteChanged=mute_signal,
        )

        def __init__(self) -> None:
            self.state = "playing"
            self.position = 12.0
            self.duration = 60.0
            self.volume = 37
            self.muted = True
            self.set_volume_calls: list[int] = []

        def set_volume(self, value: int) -> None:
            self.set_volume_calls.append(value)
            self.volume = value

        def get_url(self) -> str:
            return "http://127.0.0.1:43123/media"

        def get_state(self) -> str:
            return self.state

        def get_position(self) -> float:
            return self.position

        def get_duration(self) -> float:
            return self.duration

        def get_volume(self) -> int:
            return self.volume

        def is_muted(self) -> bool:
            return self.muted

    player = Player()
    bridge = RendererBridge(player)  # type: ignore[arg-type]
    bridge._current_uri = "http://192.168.1.20/video.mp4"  # noqa: SLF001
    bridge._current_meta = "<DIDL-Lite>original</DIDL-Lite>"  # noqa: SLF001
    old_avt, old_rc, old_cm = _Service(), _Service(volume=55), _Service()
    bridge.set_services(old_avt, old_rc, old_cm)

    assert old_avt.state_variable("TransportState").value == "PLAYING"
    assert old_rc.state_variable("Volume").value == 37
    assert old_rc.state_variable("Mute").value == "1"
    assert player.set_volume_calls == []
    assert bridge._poll_task is not None  # noqa: SLF001

    bridge.shutdown()

    assert state_signal.slots == []
    assert bridge._avt is None  # noqa: SLF001
    assert bridge._rc is None  # noqa: SLF001
    assert bridge._cm is None  # noqa: SLF001
    assert old_avt.bridge is None
    assert old_rc.bridge is None
    state_signal.emit("paused")
    assert old_avt.state_variable("TransportState").value == "PLAYING"
    assert state_signal.slots == []
    assert volume_signal.slots == []
    assert mute_signal.slots == []

    new_avt, new_rc, new_cm = _Service(), _Service(volume=70), _Service()
    bridge.set_services(new_avt, new_rc, new_cm)
    assert len(state_signal.slots) == 1
    assert len(volume_signal.slots) == 1
    assert len(mute_signal.slots) == 1
    assert bridge._poll_task is not None  # noqa: SLF001

    # 不制造任何新的 player signal：重绑动作本身必须立即恢复完整播放状态。
    assert new_avt.state_variable("TransportState").value == "PLAYING"
    assert new_avt.state_variable("RelativeTimePosition").value == "0:00:12"
    assert new_avt.state_variable("CurrentMediaDuration").value == "0:01:00"
    assert new_avt.state_variable("AVTransportURI").value == (
        "http://192.168.1.20/video.mp4"
    )
    assert new_avt.state_variable("CurrentTrackURI").value == (
        "http://192.168.1.20/video.mp4"
    )
    assert new_avt.state_variable("AVTransportURIMetaData").value == (
        "<DIDL-Lite>original</DIDL-Lite>"
    )
    assert new_rc.state_variable("Volume").value == 37
    assert new_rc.state_variable("Mute").value == "1"
    assert player.volume == 37
    assert player.set_volume_calls == []

    player.state = "paused"
    state_signal.emit("paused")
    volume_signal.emit(41)
    mute_signal.emit(False)
    assert new_avt.state_variable("TransportState").value == "PAUSED_PLAYBACK"
    assert new_rc.state_variable("Volume").value == 41
    assert new_rc.state_variable("Mute").value == "0"
    await bridge.shutdown_all()
