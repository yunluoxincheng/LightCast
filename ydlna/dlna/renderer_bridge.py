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

from async_upnp_client.exceptions import UpnpActionError
from defusedxml import ElementTree as ET  # 安全 XML 解析

from ..async_tasks import BackgroundTasks
from ..logger import get_logger
from ..player._url_guard import UrlBlockedError, validate_upstream_url
from ..player.mpv_player import Player
from ._control_context import current_controller_ip

if TYPE_CHECKING:
    from .avtransport import AVTransportService
    from .rendering_control import RenderingControlService
    from .connection_manager import ConnectionManagerService

log = get_logger("dlna.bridge")

_TRANSPORT_STATES = frozenset({
    "STOPPED",
    "PLAYING",
    "TRANSITIONING",
    "PAUSED_PLAYBACK",
    "NO_MEDIA_PRESENT",
})
_TRANSPORT_STATUSES = frozenset({"OK", "ERROR_OCCURRED"})

# DLNA 时间格式 H:MM:SS 或 H:MM:SS.frac → 秒
_TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def dlna_time_to_seconds(text: str) -> Optional[float]:
    """把 DLNA 时间字符串转秒。失败返回 None。"""
    text = text.strip()
    if not text or text == "NOT_IMPLEMENTED":
        return None
    m = _TIME_RE.match(text)
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
        self._poll_tasks = BackgroundTasks()
        # SetAVTransportURI 涉及旧代理停止、新代理创建、Player 切换和协议状态提交，
        # 必须作为一个串行事务执行，避免较早请求晚完成后覆盖较新的投屏。
        self._set_uri_lock = asyncio.Lock()
        self._last_position: Optional[float] = None
        self._last_duration: Optional[float] = None
        # TransportStatus 与 TransportState 是两条独立的 UPnP 状态轴：
        # 错误只能写 ERROR_OCCURRED 到前者，后者必须保持合法的播放状态。
        # Bridge 在 DLNA service 停服期间继续存活，因此同时持有该状态供重绑恢复。
        self._transport_status = "OK"
        # 保存控制点提供的原始 URI/元数据。Player 播放的是 127.0.0.1 代理地址，
        # DLNA 服务重启后不能把该内部 URL 暴露给重新连接的控制点。
        self._current_uri = ""
        self._current_meta = ""
        # 投屏到达（SetAVTransportURI）回调：由 app 注入，用于立即切页 + 缓冲动画
        # （不等代理/解码，用户体感秒进播放器页）
        self.on_cast_started = None  # Callable[[], None] | None
        # 投屏确认门控：由 app 注入 async (controller_ip, url, title) -> bool，
        # 弹窗询问用户。返回 True 放行并授权该控制点，False 拒绝。
        # 未注入（装配缺失）时按 fail-closed 拒绝。
        self.cast_gate = None  # Callable[[str, str, str], Awaitable[bool]] | None
        # 本次运行内已授权的控制点（真实 SOAP peer IP，由 server 层补丁登记）。
        # 授权后该控制点的全部状态变更 action（SetURI/Play/Pause/Stop/Seek/
        # Volume/Mute）都放行；未授权控制点一律拒绝。
        self._confirmed_controllers: set[str] = set()
        # 当前活跃的媒体代理（换媒体时先停旧的）
        self._hls_proxy = None
        self._direct_proxy = None
        # 连接 player 状态变化，同步到新的 DLNA service。
        self._player_signals_connected = False
        self._connect_player_signals()

    def _connect_player_signals(self) -> None:
        """确保 player 同步信号只连接一次；DLNA 服务重启时可重新连接。"""
        if self._player_signals_connected:
            return
        self._player.signals.stateChanged.connect(self._on_player_state)
        self._player.signals.volumeChanged.connect(self._on_player_volume)
        self._player.signals.muteChanged.connect(self._on_player_mute)
        self._player_signals_connected = True

    def _disconnect_player_signals(self) -> None:
        if not getattr(self, "_player_signals_connected", False):
            return
        for signal, callback in (
            (self._player.signals.stateChanged, self._on_player_state),
            (self._player.signals.volumeChanged, self._on_player_volume),
            (self._player.signals.muteChanged, self._on_player_mute),
        ):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._player_signals_connected = False

    def set_services(self, avt, rc, cm) -> None:  # noqa: ANN001
        """注入三个 service 实例（由 DlnaServer 在启动后调用）。"""
        self._connect_player_signals()
        self._avt = avt
        self._rc = rc
        self._cm = cm
        avt.bridge = self
        rc.bridge = self
        # Player/Bridge 是停服期间仍存活的播放状态所有者。新 service 的默认值
        # 必须被当前状态覆盖，不能反向把默认音量 80 写回正在播放的 Player。
        self._sync_services_from_player()

    @staticmethod
    def _write_state_variable(service, name: str, value) -> None:  # noqa: ANN001
        try:
            service.state_variable(name).value = value
        except Exception as e:  # noqa: BLE001
            log.debug("同步状态变量 %s 失败: %s", name, e)

    def _player_transport_state(self, state: str | None = None) -> str:
        state = state or self._player.get_state()
        mapping = {
            "playing": "PLAYING",
            "paused": "PAUSED_PLAYBACK",
            "stopped": "STOPPED",
        }
        if state in mapping:
            return mapping[state]
        if state == "idle" and (self._current_uri or self._player.get_url()):
            return "STOPPED"
        return "NO_MEDIA_PRESENT"

    def _sync_services_from_player(self) -> None:
        """服务启动/重启后立即恢复持久播放状态，不等待下一次 player 信号。"""
        avt = self._avt
        rc = self._rc
        if avt is None or rc is None:
            return

        position = self._player.get_position()
        duration = self._player.get_duration()
        transport_state = self._player_transport_state()
        uri = self._current_uri
        meta = self._current_meta

        for name, value in (
            ("TransportState", transport_state),
            ("TransportStatus", self._transport_status),
            ("AVTransportURI", uri),
            ("AVTransportURIMetaData", meta),
            ("CurrentTrackURI", uri),
            ("CurrentTrackMetaData", meta),
            ("CurrentTrack", 1 if uri else 0),
            ("NumberOfTracks", 1 if uri else 0),
            ("RelativeTimePosition", seconds_to_dlna(position)),
            ("AbsoluteTimePosition", seconds_to_dlna(position)),
            ("CurrentMediaDuration", seconds_to_dlna(duration)),
            ("CurrentTrackDuration", seconds_to_dlna(duration)),
        ):
            self._write_state_variable(avt, name, value)
        self._write_state_variable(rc, "Volume", self._player.get_volume())
        # Mute 状态变量是 boolean 类型，必须写真实 bool（写 "1"/"0" 会被库拒绝）
        self._write_state_variable(rc, "Mute", self._player.is_muted())

        self._last_position = position
        self._last_duration = duration
        if transport_state == "PLAYING":
            self._start_polling()

    # ------------------------------------------------------------------ #
    # DLNA action → Player（由各 service 的 action handler 调用）
    # ------------------------------------------------------------------ #
    async def on_set_uri(self, url: str, meta: str) -> None:
        """串行执行媒体切换；后到请求最终覆盖先到请求。"""
        async with self._set_uri_lock:
            await self._on_set_uri_locked(url, meta)

    async def _on_set_uri_locked(self, url: str, meta: str) -> None:
        """设置媒体 URI。

        所有 http(s) 媒体都先过本地代理：
        - m3u8 → HLS 重写代理（分片改名 + 密钥/初始化段转发 + 内容兼容）
        - 直链 → DirectProxy（防盗链头 + 重试 + 内容模式兼容）
        mpv 只接收本地代理 URL；代理初始化失败时进入错误状态，不把原始上游 URL
        直接交给 mpv。否则 mpv 自己的 DNS / 重定向会绕过代理层 SSRF 防护。

        SSRF 防护（三道关）：
        1. 非 http(s) scheme（file:// / edl:// / data: 等）→ 直接拒绝，不传给 mpv。
        2. 入口 validate_upstream_url：URL 字面量指向本机/云元数据/（收紧模式下）私网
           → 抛 UrlBlockedError → 置 TransportStatus=ERROR_OCCURRED，绝不 fallback 到 mpv。
        3. 代理层 setup_*_proxy 内部 + SSRFSafeConnector 连接时再校验（堵 302/rebinding）。
        普通失败（403/格式/网络）同样不绕过代理层。
        """
        title = parse_title_from_didl(meta) or url
        log.info("桥接: 设置媒体 title=%r url=%s", title, url)

        # 第一道：scheme 白名单。所有拒绝都发生在写入任何 evented 状态之前，
        # 候选 URI 不会短暂暴露给 GENA 订阅者。
        if not url.lower().startswith(("http://", "https://")):
            log.warning("拒绝非 http(s) 投屏 URL（可能尝试读取本地文件）: %s", url)
            self._set_transport_status("ERROR_OCCURRED")
            return

        # 第二道：入口安全校验。被拦（UrlBlockedError）→ 直接 ERROR，绝不 fallback。
        # 在 cb() 之前做：安全拒绝时不该再弹缓冲动画误导用户。
        allow_intranet = self._allow_intranet()
        try:
            validate_upstream_url(url, allow_intranet=allow_intranet)
        except UrlBlockedError as e:
            log.warning("投屏 URL 被安全策略拦截: %s（%s）", url, e.reason)
            self._set_transport_status("ERROR_OCCURRED")
            return

        # 第三道：控制点授权 + 投屏确认。授权是第一道状态变更门槛——通过之前
        # 不发布候选 URI / TransportState（service 的 AVTransportURI 等都是
        # evented 变量，先写再确认会让订阅者看到未批准的媒体）。被拒绝的投屏
        # 不切页、不显示缓冲动画，并以 SOAP fault 拒绝本次 action，控制点
        # 不会误以为投屏已被接受。
        controller = current_controller_ip()
        if not await self._confirm_cast(controller, url, title):
            log.info(
                "投屏请求未获本机确认，已拒绝: controller=%s title=%r",
                controller, title,
            )
            self._set_transport_status("ERROR_OCCURRED")
            raise UpnpActionError(
                error_desc="控制点未获授权", message="controller not authorized"
            )

        # 授权通过后才发布候选请求状态（evented）；只有代理初始化且
        # Player.play() 成功返回后才提交到 Bridge。失败则恢复上一份已提交身份。
        self._begin_transport_attempt(url, meta)

        # 先通知 UI「投屏到达」：立即切到播放器页并显示缓冲动画，
        # 代理/解码在后台进行（用户体感秒进）
        cb = self.on_cast_started
        if cb is not None:
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                log.debug("on_cast_started 回调异常: %s", e)

        candidate_info = None
        try:
            candidate_info = await self._setup_proxy_candidate(
                url, allow_intranet=allow_intranet
            )
            if candidate_info is not None:
                proxy_attr, candidate = candidate_info
                proxied = candidate.playlist_url
                log.info("播放候选代理 URL: %s", proxied)
                # 只有 Player 接受 candidate 后才提交媒体身份和代理引用；
                # 此前旧代理始终保持运行，可用于完整回滚。
                self._player.play(proxied, title)
                self._current_uri = url
                self._current_meta = meta or ""
                await self._commit_proxy_candidate(proxy_attr, candidate)
                return
            log.warning("代理初始化失败；为避免绕过 SSRF 防护，不直接播放原始 URL")
            self._fail_transport_attempt()
        except UrlBlockedError as e:
            # 代理层 SSRF 拒绝（分片/密钥/重定向目标命中黑名单）：同样绝不 fallback。
            log.warning("代理阶段 URL 被安全策略拦截: %s（%s）", url, e.reason)
            if candidate_info is not None:
                await self._discard_proxy_candidate(candidate_info[1])
            self._fail_transport_attempt()
        except Exception as e:  # noqa: BLE001  代理/播放失败：回滚状态，避免 UI 不一致
            log.warning("设置媒体失败: %s", e)
            if candidate_info is not None:
                await self._discard_proxy_candidate(candidate_info[1])
            self._fail_transport_attempt()

    def _write_transport_identity(self, uri: str, meta: str) -> None:
        if self._avt is None:
            return
        for name, value in (
            ("AVTransportURI", uri),
            ("AVTransportURIMetaData", meta),
            ("CurrentTrackURI", uri),
            ("CurrentTrackMetaData", meta),
            ("CurrentTrack", 1 if uri else 0),
            ("NumberOfTracks", 1 if uri else 0),
        ):
            self._write_state_variable(self._avt, name, value)

    def _begin_transport_attempt(self, uri: str, meta: str) -> None:
        """发布候选请求状态，但暂不覆盖 Bridge 已提交的媒体身份。"""
        self._set_transport_status("OK")
        self._write_transport_identity(uri, meta or "")
        self._set_transport_state("STOPPED")

    def _fail_transport_attempt(self) -> None:
        """失败时恢复上一份已提交媒体身份，并独立报告错误状态。"""
        self._write_transport_identity(self._current_uri, self._current_meta)
        self._set_transport_state(self._player_transport_state())
        self._set_transport_status("ERROR_OCCURRED")

    def _allow_intranet(self) -> bool:
        """读取「允许内网投屏源」配置（与 hls_rewriter 一致，默认 True）。"""
        try:
            from ..config import Config

            return bool(Config.instance().get("allow_intranet_cast", True))
        except Exception:  # noqa: BLE001
            return True

    def _require_cast_confirm(self) -> bool:
        """读取「投屏需本机确认」配置（默认 True）。"""
        try:
            from ..config import Config

            return bool(Config.instance().get("require_cast_confirm", True))
        except Exception:  # noqa: BLE001
            return True

    async def _confirm_cast(
        self, controller: Optional[str], url: str, title: str
    ) -> bool:
        """控制点授权 + 投屏确认门控（H7）。

        - 配置关闭（require_cast_confirm=False）→ 用户显式选择无鉴权模式，
          全部放行；
        - 拿不到控制点 IP（server 层上下文缺失）→ fail-closed 拒绝；
        - 控制点已授权 → 放行；
        - 首次出现的新控制点 → await 注入的确认协程（app 注入的弹窗），
          同意则授权该控制点，拒绝（或协程异常）则拒绝。

        门控协程异常按拒绝处理：确认是安全增强，出问题时宁可拒绝也不能
        退回到「无鉴权直接播放」。
        """
        if not self._require_cast_confirm():
            return True
        if controller is None:
            log.error("无法确定控制点 IP（SOAP 上下文缺失），拒绝投屏")
            return False
        if controller in self._confirmed_controllers:
            return True
        gate = self.cast_gate
        if gate is None:
            log.error("投屏确认门控未注入（应用装配错误），拒绝投屏")
            return False
        try:
            allowed = bool(await gate(controller, url, title))
        except Exception as e:  # noqa: BLE001
            log.warning("投屏确认门控异常，按拒绝处理: %s", e)
            return False
        if allowed:
            self._confirmed_controllers.add(controller)
        return allowed

    def ensure_authorized_controller(self, action: str) -> None:
        """控制点授权检查（service 写 action 在任何状态变更前调用）。

        状态变更类 action 只允许已授权控制点；未授权直接 SOAP fault。
        覆盖 SetURI/Play/Pause/Stop/Seek/SetVolume/SetMute/SetNextURI——
        否则攻击者即使从未通过投屏确认，也能暂停/停止播放、任意 Seek、
        把音量调满或静音、篡改队列中的下一个媒体。require_cast_confirm
        关闭（无鉴权模式）时不检查。
        """
        if not self._require_cast_confirm():
            return
        controller = current_controller_ip()
        if controller is not None and controller in self._confirmed_controllers:
            return
        log.warning("拒绝未授权控制点的 %s 请求: controller=%s", action, controller)
        raise UpnpActionError(
            error_desc=f"控制点未获授权: {controller}", message="controller not authorized"
        )

    async def _setup_proxy_candidate(
        self, url: str, *, allow_intranet: bool
    ) -> Optional[tuple[str, object]]:
        """建立候选代理但不触碰当前代理引用（失败返回 None）。

        m3u8 走 HLS 重写代理，其它 http(s) 直链走 DirectProxy；
        候选代理只有在 Player.play() 成功返回后才会被提交。
        """
        from ..player.hls_rewriter import setup_direct_proxy, setup_hls_proxy
        if ".m3u8" in url.lower():
            proxy = await setup_hls_proxy(url, allow_intranet=allow_intranet)
            proxy_attr = "_hls_proxy"
        else:
            proxy = await setup_direct_proxy(url, allow_intranet=allow_intranet)
            proxy_attr = "_direct_proxy"
        if proxy is None:
            return None
        return proxy_attr, proxy

    async def _discard_proxy_candidate(self, candidate) -> None:  # noqa: ANN001
        """Player 未接受候选代理时只清理 candidate，绝不触碰旧代理。"""
        try:
            await candidate.stop()
        except Exception as e:  # noqa: BLE001
            log.warning("清理未提交候选代理失败: %s", e)

    async def _commit_proxy_candidate(self, proxy_attr: str, candidate) -> None:  # noqa: ANN001
        """提交 candidate 后退休全部旧代理；调用前 Player 已接受新 URL。"""
        old_proxies = tuple(
            (attr, getattr(self, attr, None))
            for attr in ("_hls_proxy", "_direct_proxy")
        )
        setattr(self, proxy_attr, candidate)

        for attr, old in old_proxies:
            if old is None or old is candidate:
                continue
            try:
                if old.running:
                    await old.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("停止已替换媒体代理失败（%s）: %s", attr, e)
            finally:
                # 同类型 attr 已指向 candidate 时不能被旧代理清理覆盖。
                if getattr(self, attr, None) is old:
                    setattr(self, attr, None)

    def on_play(self) -> None:
        self.ensure_authorized_controller("Play")
        log.info("桥接: 播放")
        self._player.set_paused(False)
        self._set_transport_state("PLAYING")
        self._start_polling()

    def on_pause(self) -> None:
        self.ensure_authorized_controller("Pause")
        log.info("桥接: 暂停")
        self._player.set_paused(True)
        self._set_transport_state("PAUSED_PLAYBACK")

    def on_stop(self) -> None:
        self.ensure_authorized_controller("Stop")
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
        self.ensure_authorized_controller("Seek")
        if unit in ("REL_TIME", "ABS_TIME"):
            seconds = dlna_time_to_seconds(target)
            if seconds is not None:
                log.info("桥接: seek 到 %.2fs", seconds)
                self._player.seek(seconds)
        else:
            log.warning("不支持的 seek 类型: %s", unit)

    def on_set_volume(self, volume: int) -> None:
        self.ensure_authorized_controller("SetVolume")
        self._player.set_volume(volume)

    def on_set_mute(self, muted: bool) -> None:
        self.ensure_authorized_controller("SetMute")
        self._player.set_mute(muted)

    # ------------------------------------------------------------------ #
    # Player 状态 → DLNA TransportState
    # ------------------------------------------------------------------ #
    def _on_player_state(self, state: str) -> None:
        """player 的 stateChanged 信号回调（已在主线程）。"""
        dlna_state = self._player_transport_state(state)
        self._set_transport_state(dlna_state)
        if state in ("playing",):
            self._start_polling()
        elif state in ("idle", "stopped"):
            self._stop_polling()

    def _on_player_volume(self, volume: int) -> None:
        if self._rc is not None:
            self._write_state_variable(self._rc, "Volume", volume)

    def _on_player_mute(self, muted: bool) -> None:
        if self._rc is not None:
            # Mute 状态变量是 boolean 类型，必须写真实 bool
            self._write_state_variable(self._rc, "Mute", muted)

    def _set_transport_state(self, dlna_state: str) -> None:
        if dlna_state not in _TRANSPORT_STATES:
            raise ValueError(f"非法 TransportState: {dlna_state}")
        if self._avt is None:
            return
        try:
            self._avt.state_variable("TransportState").value = dlna_state
        except Exception as e:  # noqa: BLE001
            log.debug("设置 TransportState 失败: %s", e)

    def _set_transport_status(self, status: str) -> None:
        """更新协议状态；错误状态不能写入 TransportState。"""
        if status not in _TRANSPORT_STATUSES:
            raise ValueError(f"非法 TransportStatus: {status}")
        self._transport_status = status
        if self._avt is None:
            return
        try:
            self._avt.state_variable("TransportStatus").value = status
        except Exception as e:  # noqa: BLE001
            log.debug("设置 TransportStatus 失败: %s", e)

    # ------------------------------------------------------------------ #
    # 进度回传（1Hz 定时器）
    # ------------------------------------------------------------------ #
    def _start_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return
            self._poll_task = self._poll_tasks.create(
                self._poll_loop(), name="renderer-position-poll"
            )

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
        """停止协议桥接；关闭服务时保留当前媒体代理与播放。"""
        self._stop_polling()
        self._disconnect_player_signals()
        # 旧 service 可能仍被库内部对象短暂持有；解除双向引用，避免播放器
        # 状态在服务停止/重启后继续写入已经失效的 state variable。
        for service in (
            getattr(self, "_avt", None),
            getattr(self, "_rc", None),
            getattr(self, "_cm", None),
        ):
            if service is not None and getattr(service, "bridge", None) is self:
                service.bridge = None
        self._avt = None
        self._rc = None
        self._cm = None
        self._last_position = None
        self._last_duration = None

    async def shutdown_all(self) -> None:
        """应用退出清理：等待轮询退出，并停止所有当前媒体代理。"""
        self.shutdown()
        # shutdown() 只负责同步发出一次 cancel；任务仍由注册表强引用持有，
        # 此处等待它真正退出，且不重复 cancel（避免打断其取消清理逻辑）。
        await self._poll_tasks.wait_all()

        for attr in ("_hls_proxy", "_direct_proxy"):
            proxy = getattr(self, attr, None)
            if proxy is None:
                continue
            try:
                await proxy.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("应用退出时停止媒体代理失败（%s）: %s", attr, e)
            finally:
                setattr(self, attr, None)
