"""RendererBridge —— 连接 DLNA 协议层与播放器层。

职责
----
1. 把 DLNA action（SetAVTransportURI/Play/Pause/Stop/Seek/SetVolume/SetMute）
   翻译成对 Player 的调用。
2. 周期性（1Hz）把播放器进度（time_pos/duration/state）写回 AVTransport 状态变量，
   触发 GENA 事件推送给控制点，使手机端进度条实时刷新。
3. 解析 DIDL-Lite 元数据，提取媒体标题。
4. 维护 DLNA TransportState 与 mpv 播放状态的映射。
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Optional

from defusedxml import ElementTree as ET  # 安全 XML 解析

from ..logger import get_logger
from ..player.mpv_player import Player

if TYPE_CHECKING:
    from .avtransport import AVTransportService
    from .rendering_control import RenderingControlService
    from .connection_manager import ConnectionManagerService

log = get_logger("dlna.bridge")

# DLNA 时间格式 H:MM:SS 或 H:MM:SS.frac → 秒
_TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def dlna_time_to_seconds(text: str) -> Optional[float]:
    """把 DLNA 时间字符串转秒。失败返回 None。"""
    if not text or text in ("NOT_IMPLEMENTED", "00:00:00", ""):
        return None
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi, s = m.group(1), int(m.group(2)), float(m.group(3))
    total = mi * 60 + s
    if h:
        total += int(h) * 3600
    return total


def seconds_to_dlna(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "NOT_IMPLEMENTED"
    total = float(seconds)
    h = int(total // 3600)
    mi = int((total % 3600) // 60)
    s = total % 60
    return f"{h}:{mi:02d}:{int(s):02d}"


def parse_title_from_didl(meta_xml: str) -> str:
    """从 DIDL-Lite 元数据提取 dc:title。

    meta_xml 形如::
        <DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
                   xmlns:dc="http://purl.org/dc/elements/1.1/">
          <item ...><dc:title>视频标题</dc:title>...</item>
        </DIDL-Lite>
    """
    if not meta_xml:
        return ""
    try:
        root = ET.fromstring(meta_xml)
    except ET.ParseError:
        return ""
    # dc 命名空间
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    title_el = root.find(".//dc:title", ns)
    if title_el is not None and title_el.text:
        return title_el.text.strip()
    # 兜底：无命名空间的 title
    title_el = root.find(".//{http://purl.org/dc/elements/1.1/}title")
    if title_el is not None and title_el.text:
        return title_el.text.strip()
    return ""


def parse_class_from_didl(meta_xml: str) -> str:
    """从 DIDL-Lite 元数据提取 upnp:class（媒体类型）。

    返回如 "object.item.imageItem" / "object.item.videoItem" / ""。
    """
    if not meta_xml:
        return ""
    try:
        root = ET.fromstring(meta_xml)
    except ET.ParseError:
        return ""
    ns = {"upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/"}
    el = root.find(".//upnp:class", ns)
    if el is not None and el.text:
        return el.text.strip()
    return ""


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".avif")

# 常见图片 URL 特征（无扩展名时兜底）
_IMAGE_CT = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp")


def url_is_image(url: str) -> bool:
    """判断 URL 是否指向图片。"""
    path = url.split("?", 1)[0].lower()
    return path.endswith(_IMAGE_EXTS)


class RendererBridge:
    """DLNA 协议层 ↔ 播放器层的桥接器。

    使用前需要调用 ``set_services`` 注入三个 service 实例。
    """

    def __init__(self, player: Player) -> None:
        self._player = player
        self._avt: Optional["AVTransportService"] = None
        self._rc: Optional["RenderingControlService"] = None
        self._cm: Optional["ConnectionManagerService"] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._last_position: Optional[float] = None
        self._last_duration: Optional[float] = None
        # 连接 player 状态变化，同步到 DLNA TransportState
        self._player.signals.stateChanged.connect(self._on_player_state)

    def set_services(self, avt, rc, cm) -> None:  # noqa: ANN001
        """注入三个 service 实例（由 DlnaServer 在启动后调用）。"""
        self._avt = avt
        self._rc = rc
        self._cm = cm
        avt.bridge = self
        rc.bridge = self
        # 初始音量同步到 player
        try:
            vol = int(rc.state_variable("Volume").value)
            self._player.set_volume(vol)
        except Exception as e:  # noqa: BLE001
            log.debug("初始音量同步失败: %s", e)

    # ------------------------------------------------------------------ #
    # DLNA action → Player（由各 service 的 action handler 调用）
    # ------------------------------------------------------------------ #
    async def on_set_uri(self, url: str, meta: str) -> None:
        """设置媒体 URI。

        对 m3u8 流做特殊处理：某些源的 m3u8 分片用 .jpg 扩展名伪装，
        ffmpeg 会误判为图片导致解码失败。先下载并重写 m3u8（分片指向
        本地代理 + .mp4 扩展名），再交给 mpv。
        """
        title = parse_title_from_didl(meta) or url
        log.info("桥接: 设置媒体 title=%r url=%s", title, url)

        if ".m3u8" in url.lower():
            from ..player.hls_rewriter import setup_hls_proxy
            # 复用同一个代理（多次投屏时先停旧的）
            proxy = getattr(self, "_hls_proxy", None)
            if proxy is not None and proxy.running:
                await proxy.stop()

            proxy = await setup_hls_proxy(url)
            if proxy is not None:
                self._hls_proxy = proxy
                log.info("播放重写后的 m3u8: %s", proxy.playlist_url)
                self._player.play(proxy.playlist_url, title)
                return
            log.warning("m3u8 重写失败，回退直接播放原 URL")
            self._player.play(url, title)
        else:
            self._player.play(url, title)

    def on_play(self) -> None:
        log.info("桥接: 播放")
        self._player.set_paused(False)
        self._set_transport_state("PLAYING")
        self._start_polling()

    def on_pause(self) -> None:
        log.info("桥接: 暂停")
        self._player.set_paused(True)
        self._set_transport_state("PAUSED_PLAYBACK")

    def on_stop(self) -> None:
        log.info("桥接: 停止")
        self._player.stop()
        self._set_transport_state("STOPPED")
        self._stop_polling()
        # 清空进度
        self._last_position = None
        if self._avt is not None:
            self._avt.state_variable("RelativeTimePosition").value = "NOT_IMPLEMENTED"

    def on_seek(self, unit: str, target: str) -> None:
        """unit ∈ {ABS_TIME, REL_TIME, ABS_COUNT, REL_COUNT, TRACK_NR}。"""
        if unit in ("REL_TIME", "ABS_TIME"):
            seconds = dlna_time_to_seconds(target)
            if seconds is not None:
                log.info("桥接: seek 到 %.2fs", seconds)
                self._player.seek(seconds)
        else:
            log.warning("不支持的 seek 类型: %s", unit)

    def on_set_volume(self, volume: int) -> None:
        self._player.set_volume(volume)

    def on_set_mute(self, muted: bool) -> None:
        self._player.set_mute(muted)

    # ------------------------------------------------------------------ #
    # Player 状态 → DLNA TransportState
    # ------------------------------------------------------------------ #
    def _on_player_state(self, state: str) -> None:
        """player 的 stateChanged 信号回调（已在主线程）。"""
        mapping = {
            "playing": "PLAYING",
            "paused": "PAUSED_PLAYBACK",
            "stopped": "STOPPED",
            "idle": "NO_MEDIA_PRESENT",
        }
        dlna_state = mapping.get(state)
        if dlna_state is None:
            return
        # idle 不一定是 NO_MEDIA（可能是播完回到 idle），只有有 URI 时才映射
        if state == "idle":
            url = self._player.get_url()
            if url:
                dlna_state = "STOPPED"
            else:
                dlna_state = "NO_MEDIA_PRESENT"
        self._set_transport_state(dlna_state)
        if state in ("playing",):
            self._start_polling()
        elif state in ("idle", "stopped"):
            self._stop_polling()

    def _set_transport_state(self, dlna_state: str) -> None:
        if self._avt is None:
            return
        try:
            self._avt.state_variable("TransportState").value = dlna_state
        except Exception as e:  # noqa: BLE001
            log.debug("设置 TransportState 失败: %s", e)

    # ------------------------------------------------------------------ #
    # 进度回传（1Hz 定时器）
    # ------------------------------------------------------------------ #
    def _start_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                return
            self._poll_task = loop.create_task(self._poll_loop())

    def _stop_polling(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self) -> None:
        """每秒把 player 进度写回 AVTransport 状态变量，触发 GENA 事件。"""
        log.debug("开始进度回传轮询")
        try:
            while True:
                await asyncio.sleep(1.0)
                self._update_position()
        except asyncio.CancelledError:
            log.debug("进度回传轮询已取消")
        except Exception as e:  # noqa: BLE001
            log.exception("进度轮询异常: %s", e)

    def _update_position(self) -> None:
        if self._avt is None:
            return
        pos = self._player.get_position()
        dur = self._player.get_duration()
        # 避免无变化时重复触发事件
        if pos != self._last_position:
            self._last_position = pos
            try:
                self._avt.state_variable("RelativeTimePosition").value = seconds_to_dlna(pos)
            except Exception as e:  # noqa: BLE001
                log.debug("更新 RelTime 失败: %s", e)
        if dur != self._last_duration:
            self._last_duration = dur
            try:
                self._avt.state_variable("CurrentMediaDuration").value = seconds_to_dlna(dur)
            except Exception as e:  # noqa: BLE001
                log.debug("更新 duration 失败: %s", e)

    # ------------------------------------------------------------------ #
    # 清理
    # ------------------------------------------------------------------ #
    def shutdown(self) -> None:
        self._stop_polling()
