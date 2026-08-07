"""python-mpv 封装：把 libmpv 嵌入 Qt 窗口，并提供 Qt 信号桥接。

设计要点
--------
1. mpv 实例通过 ``wid`` 嵌入由 ``MpvWidget`` 提供的原生窗口句柄。
2. python-mpv 的 property_observer / event_callback 在 mpv 的**事件线程**触发，
   线程不安全，所有对外通知一律通过 ``Signal`` marshal 回 Qt 主线程。
3. 对外只暴露 Qt 风格的异步信号，UI 层不应直接持有 mpv 对象。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..logger import get_logger

log = get_logger("player")

# mpv 是可选导入：libmpv 缺失时给出清晰错误，由上层提示用户。
# 关键：加载 libmpv 时不能持久改变 DLL 搜索路径，否则会破坏 PySide6。
# 做法：临时把 bin/ 放到 PATH 最前 → import mpv → 立即恢复 PATH。
import os as _os
from ..constants import BIN_DIR as _BIN_DIR

_orig_path = _os.environ.get("PATH", "")
_bin_str = str(_BIN_DIR)
_path_modified = _bin_str not in _orig_path.split(_os.pathsep)
if _path_modified:
    _os.environ["PATH"] = _bin_str + _os.pathsep + _orig_path

try:
    import mpv  # type: ignore
    _MPV_AVAILABLE = True
    _MPV_IMPORT_ERROR: Optional[str] = None
except (ImportError, OSError) as _e:  # python-mpv 装了但找不到 dll 也会 OSError
    mpv = None  # type: ignore
    _MPV_AVAILABLE = False
    _MPV_IMPORT_ERROR = str(_e)
finally:
    # 恢复 PATH，避免影响后续 PySide6 的 dll 加载
    if _path_modified:
        _os.environ["PATH"] = _orig_path


def is_available() -> bool:
    """是否成功加载了 libmpv。"""
    return _MPV_AVAILABLE


def import_error() -> Optional[str]:
    return _MPV_IMPORT_ERROR


class PlayerSignals(QObject):
    """从 mpv 事件线程 marshal 到 Qt 主线程的信号集合。"""

    # 当前播放位置（秒）。value=None 表示无媒体
    positionChanged = Signal(object)  # float | None
    # 媒体总时长（秒）。value=None 表示未知
    durationChanged = Signal(object)  # float | None
    # 播放状态："playing" | "paused" | "stopped" | "idle"
    stateChanged = Signal(str)
    # 新媒体装载：(title, url)。title 可能为空字符串
    mediaChanged = Signal(str, str)
    # 播放结束（自然结束或被 stop）
    ended = Signal()
    # 音量变化 0..100
    volumeChanged = Signal(int)
    # 静音状态变化
    muteChanged = Signal(bool)
    # mpv 内部错误消息
    errorOccurred = Signal(str)
    # 播放/加载失败：(标题, 技术细节)。用于 UI 友好提示
    playbackFailed = Signal(str, str)


class Player:
    """libmpv 播放器封装（不创建窗口，需要外部提供 wid）。

    生命周期
    --------
    1. ``player = Player()`` — 创建 mpv 实例（此时还未关联窗口）
    2. ``player.attach(wid)`` — 关联到 QWidget 的窗口句柄，此后可播放
    3. ``player.play(url, title)`` / ``player.pause`` / ``player.stop`` ...
    4. ``player.shutdown()`` — 销毁 mpv 实例，释放资源
    """

    def __init__(self) -> None:
        if not _MPV_AVAILABLE:
            raise RuntimeError(
                f"libmpv 不可用: {_MPV_IMPORT_ERROR}. "
                "请将 mpv-2.dll 放入 bin/ 目录（详见 README）。"
            )
        self.signals = PlayerSignals()
        self._mpv = None  # 延迟到 attach 时创建
        self._attached = False
        self._title: str = ""
        self._url: str = ""
        # 缓存最新位置/时长，供 DLNA 进度查询用（避免跨线程读 mpv 属性）
        self._position: Optional[float] = None
        self._duration: Optional[float] = None
        self._state: str = "idle"
        self._volume: int = 80
        self._muted: bool = False
        self._speed: float = 1.0
        self._audio_device: str = ""
        # 本次播放是否已成功装载（用于区分「加载失败」与「正常结束」）
        self._load_ok: bool = False
        # 最近一次播放的关键错误细节（播放失败时随 playbackFailed 上报）
        self._last_error: str = ""

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def attach(self, wid: int) -> None:
        """把 mpv 嵌入指定窗口句柄（HWND）。只能在主线程调用。"""
        if self._attached:
            return
        log.info("创建 mpv 实例，wid=%s", wid)
        self._mpv = mpv.MPV(
            wid=str(wid),
            vo="gpu",
            hwdec="auto",
            keep_open="always",          # 播完保持画面，便于 DLNA 反复查看进度
            input_default_bindings=True,
            input_vo_keyboard=False,     # 键盘由 Qt 处理，避免与 mpv 冲突
            input_cursor=False,
            osc=False,                   # 不显示 mpv 自带 OSD 控制条
            osd_level=0,
            cursor_autohide=False,
            terminal=False,
            ytdl=False,                  # 禁用 youtube-dl 解析（本地 m3u8 不需要，且会报错干扰）
            # 把 mpv 内部日志接到我们的日志体系（关键诊断基础设施：
            # 没有它，媒体解码失败时看不到任何错误，只能盲猜）
            log_handler=self._mpv_log_handler,
            loglevel="info",
        )
        self._register_callbacks()
        # 应用初始音量/倍速/音频设备
        try:
            self._mpv.volume = self._volume
            self._mpv.mute = self._muted
            self._mpv.speed = self._speed
            if self._audio_device:
                self._mpv.audio_device = self._audio_device
        except Exception as e:  # 属性写入偶尔会因时机失败，忽略
            log.debug("设置初始属性失败: %s", e)
        self._attached = True
        self._set_state("idle")

    def shutdown(self) -> None:
        """销毁 mpv 实例。"""
        if self._mpv is None:
            return
        try:
            self._mpv.terminate()
        except Exception as e:
            log.warning("mpv terminate 失败: %s", e)
        self._mpv = None
        self._attached = False

    @property
    def available(self) -> bool:
        return self._attached and self._mpv is not None

    # ------------------------------------------------------------------ #
    # mpv 日志接入
    # ------------------------------------------------------------------ #
    def _mpv_log_handler(self, loglevel: str, component: str, message: str) -> None:
        """把 mpv 内部日志映射到我们的 logger。

        在 mpv 事件线程触发，只做日志，不碰 Qt。
        """
        try:
            if loglevel == "error" or loglevel == "fatal":
                log.error("[mpv/%s] %s", component, message.strip())
                # 记录关键组件的错误细节（用于失败提示）；
                # 排除解码器噪声（aac/video 的包损坏告警对定位没帮助）
                if component in ("lavf", "ffmpeg/demuxer", "cplayer", "vd", "ad"):
                    self._last_error = message.strip()
            elif loglevel == "warn":
                log.warning("[mpv/%s] %s", component, message.strip())
            else:
                log.debug("[mpv/%s] %s", component, message.strip())
        except Exception:  # noqa: BLE001  日志失败绝不能影响播放
            pass

    # ------------------------------------------------------------------ #
    # 回调注册（mpv 事件线程 → Qt 信号）
    #
    # python-mpv 的回调运行在它自己的事件线程。Qt 跨线程信号在 AutoConnection
    # 下走 QueuedConnection，需接收方线程（主线程）有事件循环——qasync 提供。
    # 实测从普通 Python 线程直接 emit 可正常投递到主线程，故此处直接 emit。
    # ------------------------------------------------------------------ #
    def _register_callbacks(self) -> None:
        m = self._mpv

        @m.property_observer("time-pos")
        def _on_time_pos(_name, value):  # noqa: ANN001
            log.debug("obs time-pos=%s", value)
            self._position = value
            self.signals.positionChanged.emit(value)

        @m.property_observer("duration")
        def _on_duration(_name, value):  # noqa: ANN001
            log.debug("obs duration=%s", value)
            self._duration = value
            self.signals.durationChanged.emit(value)

        @m.property_observer("pause")
        def _on_pause(_name, value):  # noqa: ANN001
            if value:
                self._set_state("paused")
            elif self._state != "idle":
                self._set_state("playing")

        @m.property_observer("volume")
        def _on_volume(_name, value):  # noqa: ANN001
            if value is not None:
                self._volume = int(value)
                self.signals.volumeChanged.emit(self._volume)

        @m.property_observer("mute")
        def _on_mute(_name, value):  # noqa: ANN001
            self._muted = bool(value)
            self.signals.muteChanged.emit(self._muted)

        @m.property_observer("idle-active")
        def _on_idle(_name, value):  # noqa: ANN001
            if value:
                self._set_state("idle")

        @m.event_callback("file-loaded")
        def _on_file_loaded(_event):  # noqa: ANN001
            log.info("媒体已装载: %s", self._url)
            self._load_ok = True
            self._set_state("playing")
            self.signals.mediaChanged.emit(self._title or self._url, self._url)

        @m.event_callback("end-file")
        def _on_end_file(event):  # noqa: ANN001
            reason = 0
            try:
                reason = event.event.reason
            except AttributeError:
                try:
                    reason = event["event"]["reason"]
                except (TypeError, KeyError):
                    pass
            log.info("媒体结束 (reason=%s)", reason)
            self._position = None
            self._set_state("idle")
            # mpv end-file reason: 0=EOF 2=STOP 3=QUIT 4=ERROR 5=REDIRECT
            # 播放失败（ERROR，或从未装载成功就结束）→ 上报友好提示
            if reason == 4 or (not self._load_ok and reason not in (2, 3)):
                detail = self._last_error
                self._last_error = ""
                self.signals.playbackFailed.emit(self._title, detail)
            self.signals.ended.emit()

        @m.event_callback("start-file")
        def _on_start_file(_event):  # noqa: ANN001
            log.debug("开始播放文件")

    # ------------------------------------------------------------------ #
    # 播放控制（主线程调用）
    # ------------------------------------------------------------------ #
    def play(self, url: str, title: str = "") -> None:
        """播放指定 URL（本地路径或 http(s)://）。"""
        if not self.available:
            log.error("mpv 未就绪，无法播放")
            return
        self._url = url
        self._title = title
        self._load_ok = False
        self._last_error = ""
        log.info("播放: title=%r url=%s", title, url)
        self._mpv.play(url)

    def play_local_file(self, path: str, title: str = "") -> None:
        """播放本地文件（重写后的 m3u8 临时文件）。"""
        if not self.available:
            log.error("mpv 未就绪，无法播放")
            return
        self._url = path
        self._title = title
        log.info("播放本地 m3u8: title=%r path=%s", title, path)
        self._mpv.play(path)

    def play_pause(self) -> None:
        if not self.available:
            return
        self._mpv.pause = not self._mpv.pause

    def set_paused(self, paused: bool) -> None:
        if not self.available:
            return
        self._mpv.pause = paused

    def stop(self) -> None:
        if not self.available:
            return
        log.info("停止播放")
        self._mpv.command("stop")

    def seek(self, seconds: float) -> None:
        """绝对定位到 seconds 秒。"""
        if not self.available:
            return
        try:
            self._mpv.command("seek", float(seconds), "absolute", "exact")
        except Exception as e:
            log.warning("seek 失败: %s", e)

    def seek_relative(self, delta: float) -> None:
        """相对快进/后退 delta 秒（用于「快进 ±10s」按钮）。"""
        if not self.available:
            return
        try:
            self._mpv.command("seek", float(delta), "relative", "exact")
        except Exception as e:
            log.warning("relative seek 失败: %s", e)

    def set_speed(self, speed: float) -> None:
        """设置播放倍速（0.25 ~ 4.0）。"""
        speed = max(0.25, min(4.0, float(speed)))
        if not self.available:
            self._speed = speed
            return
        try:
            self._mpv.speed = speed
        except Exception as e:
            log.debug("设置倍速失败: %s", e)

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, int(volume)))
        if not self.available:
            self._volume = volume
            return
        try:
            self._mpv.volume = volume
        except Exception as e:
            log.debug("设置音量失败: %s", e)

    def set_mute(self, muted: bool) -> None:
        if not self.available:
            self._muted = muted
            return
        try:
            self._mpv.mute = muted
        except Exception as e:
            log.debug("设置静音失败: %s", e)

    # ------------------------------------------------------------------ #
    # 音频输出设备
    # ------------------------------------------------------------------ #
    def get_audio_devices(self) -> list[tuple[str, str]]:
        """可用音频输出设备列表 [(name, description)]。"""
        if not self.available:
            return []
        try:
            devices = self._mpv.audio_device_list or []
            return [(d.get("name", ""), d.get("description", "")) for d in devices]
        except Exception as e:  # noqa: BLE001
            log.debug("读取音频设备列表失败: %s", e)
            return []

    def get_audio_device(self) -> str:
        """当前音频输出设备名（"" = 默认）。"""
        if not self.available:
            return self._audio_device
        try:
            return str(self._mpv.audio_device or "")
        except Exception:  # noqa: BLE001
            return self._audio_device

    def set_audio_device(self, name: str) -> None:
        """设置音频输出设备（name="" 表示默认/自动）。"""
        name = name or ""
        self._audio_device = name
        if not self.available:
            return
        try:
            self._mpv.audio_device = name
            log.info("音频输出设备: %r", name or "默认")
        except Exception as e:
            log.warning("设置音频设备失败: %s", e)

    # ------------------------------------------------------------------ #
    # 状态查询（供 DLNA 层用，缓存值，线程安全）
    # ------------------------------------------------------------------ #
    def get_position(self) -> Optional[float]:
        return self._position

    def get_duration(self) -> Optional[float]:
        return self._duration

    def get_state(self) -> str:
        return self._state

    def get_title(self) -> str:
        return self._title

    def get_url(self) -> str:
        return self._url

    def get_volume(self) -> int:
        return self._volume

    def get_speed(self) -> float:
        return self._speed

    def is_muted(self) -> bool:
        return self._muted

    # ------------------------------------------------------------------ #
    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.signals.stateChanged.emit(state)
