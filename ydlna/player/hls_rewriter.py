"""HLS / 直链流本地代理 —— 解决 ffmpeg 分片识别与防盗链问题。

背景
----
某些 DLNA 投屏源（如部分看番软件）的 m3u8 分片 URL 用 ``.jpg`` 扩展名伪装，
但实际内容是 mp4 容器包裹的 h264 视频。ffmpeg 的 HLS demuxer 探测分片时，
``.jpg`` 扩展名让 image2 demuxer 得分碾压 mp4 → 把视频误判成图片 → 解码失败
（症状：媒体已装载但黑屏、"Invalid data found when processing input"）。

已验证：分片扩展名改成 ``.mp4`` 后 mpv 能正确识别 h264 1080p 并正常播放。
新版 ffmpeg（2025-01 起）对分片扩展名做了白名单检查（extension_picky），
非白名单扩展名（``.jpg``/``.m4s``/``.cmfv``/无扩展名）都会被直接拒绝，
因此代理把分片统一改名为 ``.mp4``/``.ts``/``.avi`` 是一并兜住的。

加密流（``#EXT-X-KEY``，AES-128）的密钥 URI 和 fMP4 的初始化段
（``#EXT-X-MAP``）若保持相对地址，会解析到本地代理的 404 —— 也必须
一并重写到本地端点并转发。

防盗链（403 是最常见的"换番报错"来源）
--------------------------------------
番剧站 CDN 普遍校验 Referer / User-Agent，mpv 裸请求会被 403 拒绝。
本代理对所有上游请求（m3u8/分片/密钥/初始化段/探测/预热）自动附加
``Referer: 源站 origin`` + 浏览器 UA，并用同一个会话（cookie jar 跟随
302 跳转自动累积）；网络错误 / 5xx 自动重试一次；上游 200 但返回 HTML
（登录墙/防盗链错误页）时识别出来并返回 502，让用户看到友好提示而不是
mpv 的"Invalid data"。

图像流（漫画/图文番，分片是真实 PNG/JPEG 图片）
------------------------------------------------
ffmpeg 的 HLS demuxer 无法直接播放图片分片（``Video: png`` +
"Could not find codec parameters"，mpv issue #14781 未修复）。原因：
图片管道解复用器（png_pipe/mjpeg_pipe）读取时依赖 ``avio_size``，
而 HLS 给分片挂的是不可 seek 的自定义 IO；即使换成 ffconcat 播放列表，
图片读取也会从错误偏移开始（读到的包是 PNG IEND 之后的尾随数据）。

方案：代理把每张图片惰性转成 JPEG（PIL）并包装成单帧 MJPEG/AVI，
分片 URL 改写为 ``/seg/{n}.avi`` —— HLS demuxer 走标准路径探测 AVI、
增量读取（AVI 解复用器对 ``avio_size`` 失败有回退），EXTINF 时长、
进度、seek 全部保留。

混合分片（PNG 封面 + TS 视频，番剧站省流量的常见套路）
-----------------------------------------------------
某些站把"小 PNG 封面 + 完整 MPEG-TS 视频"拼成一个文件（PNG 头 ~212 字节
伪装成图片上传图床）。播放时必须剥掉 PNG 前缀、从 TS 起点（连续 0x47
同步字节）提供。

主播放列表 / 直播流
-------------------
- 主播放列表（只有 ``#EXT-X-STREAM-INF`` + 变体）：递归跟进第一个变体。
- 直播流（无 ``#EXT-X-ENDLIST``）：当前不支持。不能把未经安全连接器约束的
  原始上游 URL 直接交给 mpv，否则重定向 / DNS rebinding 会绕过 SSRF 防护。

方案（直接接收播放，无文件、无额外步骤）
--------------------------------------
1. 收到 m3u8 URL → 下载 m3u8 文本（防盗链头 + 重试，302 后按最终地址解析）
2. 启动本地代理（aiohttp，内存态），提供端点：
   - ``/playlist.m3u8``：返回重写后的 m3u8
   - ``/seg/{n}.mp4``：转发真实分片（保留 Range/206 语义 + 防盗链 header）
   - ``/seg/{n}.avi``：图像流分片（PNG→JPEG→AVI，惰性转换 + 内存缓存）
   - ``/seg/{n}.ts``：混合分片（PNG 封面+TS，剥掉封面按 TS 提供）
   - ``/key/{n}.key``：转发 AES-128 密钥（内存缓存，校验 16 字节）
   - ``/map/{n}.mp4``：转发 fMP4 初始化段
   所有 URI（分片/密钥/初始化段）都按 m3u8 的最终基地址解析成绝对 URL 后转发
3. mpv 只播放一个 URL：``http://127.0.0.1:{port}/playlist.m3u8`` —— 走标准 HLS 路径

非 m3u8 直链（DirectProxy）
---------------------------
直链也走本地代理：同样带防盗链头 + 重试；内容按探测结果分三种模式：
视频转发（保留 Range）/ 纯图片转 AVI / PNG 头+TS 剥离（缓存 + Range 支持）。
"""
from __future__ import annotations

import asyncio
import io
import re
import struct
import warnings
from collections.abc import Callable, Coroutine, Hashable
from typing import Any, Optional, TypeVar
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import web

from ..async_tasks import BackgroundTasks
from ..config import Config
from ..logger import get_logger
from ._url_guard import (
    UrlBlockedError,
    make_session,
    safe_get,
    validate_upstream_url,
)

log = get_logger("player.hls")

_T = TypeVar("_T")

# #EXT-X-KEY / #EXT-X-MAP 里的 URI="..." 属性
_ATTR_URI_RE = re.compile(r'URI="([^"]*)"')

# 小响应的硬读取上限（防恶意上游无限返回撑爆内存）
_MAX_M3U8 = 1 * 1024 * 1024          # m3u8 播放列表：1MB 足够（极长番剧也就几十 KB）
_MAX_KEY = 16                         # AES-128 密钥固定 16 字节；读取时多读 1 字节以识别"上游多给内容"
_MAX_PROBE = 64 * 1024               # 分片类型探测：首 64KB

# 图像流 JPEG 缓存上限（每张 ~100KB-1MB，16 张足够顺序播放滚动）
_JPEG_CACHE_LIMIT = 16

# 常见图片魔数（探测"图文流"用）
_IMAGE_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF8", "gif"),
    (b"BM", "bmp"),
    (b"RIFF", "webp"),
)

# hybrid/图像模式的整段缓冲上限（防大分片 OOM）
_MAX_BUFFER = 96 * 1024 * 1024

# 防盗链：所有上游请求默认带浏览器 UA；Referer 用源站 origin
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 请求超时：内部小请求 15s，整段缓冲 60s；流式转发允许长时间播放，
# 但连接/连接池等待最多 10s，任意两次上游读之间最多等待 30s。
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_TIMEOUT_BIG = aiohttp.ClientTimeout(total=60)
_TIMEOUT_FORWARD = aiohttp.ClientTimeout(total=None, connect=10, sock_read=30)


def _origin(url: str) -> str:
    """URL 的源站（scheme://netloc），用作防盗链 Referer。"""
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _detect_image(data: bytes) -> Optional[str]:
    """按魔数判断数据是否为图片，返回类型名或 None。"""
    for magic, kind in _IMAGE_MAGICS:
        if data.startswith(magic):
            if kind == "webp" and data[8:12] != b"WEBP":
                continue
            return kind
    return None


def _find_ts_offset(data: bytes, syncs: int = 4) -> Optional[int]:
    """找 MPEG-TS 数据起点：连续 syncs 个 0x47 同步字节（188 字节间隔）。

    某些视频站的分片是"小 PNG 封面 + 附加完整 TS 视频"的混合文件，
    播放时必须剥掉 PNG 前缀、从 TS 起点提供。要求 4 个连续同步字节，
    把纯图片内容里随机撞上 0x47 的概率压到 ~1/2^32，几乎不可能误判。
    """
    span = 188 * (syncs - 1)
    for i in range(len(data) - span):
        if all(data[i + 188 * k] == 0x47 for k in range(syncs)):
            return i
    return None


def _looks_html(data: bytes) -> bool:
    """判断内容是否为 HTML 页面（防盗链/登录墙经常 200 返回 HTML 错误页）。"""
    head = data[:512].lstrip().lower()
    return head.startswith((b"<html", b"<!doctype", b"<?xml"))


def _make_avi(jpeg: bytes, width: int, height: int) -> bytes:
    """把单帧 MJPEG 包装成最小 AVI（流式读取，不依赖 avio_size）。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        pad = b"\x00" if len(payload) % 2 else b""
        return tag + struct.pack("<I", len(payload)) + payload + pad

    def list_chunk(kind: bytes, payload: bytes) -> bytes:
        return b"LIST" + struct.pack("<I", 4 + len(payload)) + kind + payload

    avih = struct.pack(
        "<IIIIIIIIII4I",
        40000,          # dwMicroSecPerFrame (25fps)
        0,              # dwMaxBytesPerSec
        0,              # dwPaddingGranularity
        0x10,           # dwFlags = AVIF_HASINDEX
        1,              # dwTotalFrames
        0,              # dwInitialFrames
        1,              # dwStreams
        len(jpeg),      # dwSuggestedBufferSize
        width,
        height,
        0, 0, 0, 0,     # dwReserved[4]
    )
    strh = struct.pack(
        "<4s4sIHHIIIIIIII4I4I",
        b"vids", b"MJPG",
        0,              # dwFlags
        0, 0,           # wPriority, wLanguage
        0,              # dwInitialFrames
        1, 25,          # dwScale, dwRate
        0,              # dwStart
        1,              # dwLength
        len(jpeg),      # dwSuggestedBufferSize
        0xFFFFFFFF,     # dwQuality
        0,              # dwSampleSize
        0, 0, 0, 0,     # rcFrame
        0, 0, 0, 0,     # dwReserved[4]
    )
    # biHeight 用负值（自顶向下）：MJPEG 解码输出为顶向下，避免画面翻转
    strf = struct.pack(
        "<IiiHH4sIIIII",
        40,             # biSize
        width, -height, # biWidth, biHeight
        1,              # biPlanes
        24,             # biBitCount
        b"MJPG",        # biCompression
        len(jpeg),      # biSizeImage
        0, 0, 0, 0,     # dpi / clr
    )

    strl = list_chunk(b"strl", chunk(b"strh", strh) + chunk(b"strf", strf))
    hdrl = list_chunk(b"hdrl", chunk(b"avih", avih) + strl)

    dc = chunk(b"00dc", jpeg)
    movi = list_chunk(b"movi", dc)
    # idx1 里的数据偏移 = RIFF 头(12) + hdrl + LIST头(12) + 00dc头(8)
    idx1 = b"idx1" + struct.pack("<I", 16) + b"00dc" + struct.pack(
        "<IIII", 0x10, 12 + len(hdrl) + 12 + 8, len(jpeg), 0
    )
    riff = b"RIFF" + struct.pack("<I", 4 + len(hdrl) + len(movi) + len(idx1)) + b"AVI "
    return riff + hdrl + movi + idx1


async def _read_capped(resp: aiohttp.ClientResponse, what: str,
                       cap: int = _MAX_BUFFER) -> Optional[bytes]:
    """整段读上游响应，超过上限返回 None（hybrid/图片模式防 OOM）。"""
    buf = bytearray()
    try:
        async for chunk in resp.content.iter_chunked(256 * 1024):
            buf += chunk
            if len(buf) > cap:
                log.warning("%s超过缓冲上限 %d MB，放弃", what, cap // (1024 * 1024))
                return None
    except asyncio.CancelledError:
        raise
    except (aiohttp.ClientError, OSError) as e:
        log.warning("%s读取中断: %s", what, e)
        return None
    return bytes(buf)


def _allow_intranet() -> bool:
    """读取「允许内网投屏源」配置（SSRF 防护开关，默认 True）。

    DLNA 投屏本就是同局域网场景，默认放行私网；loopback/link-local/云元数据
    由 _url_guard 始终拦截（与本配置无关）。
    """
    try:
        return bool(Config.instance().get("allow_intranet_cast", True))
    except Exception:  # noqa: BLE001  配置不可用时放行私网（匹配默认）
        return True


def _set_pil_pixel_limit() -> None:
    """限制 PIL 解码像素上限（防解压炸弹：小 PNG 解码成 GB 级位图）。

    MAX_IMAGE_PIXELS 是**像素数**（非字节数）。4096×4096≈16.7M 像素，
    番剧 / 漫画分页绰绰有余。PIL 对超过该值先发 DecompressionBombWarning，
    超过 2 倍才抛 Error；_convert_segment / _buffer_once 同时捕获两者。
    幂等，多次调用安全。
    """
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = 4096 * 4096
    except ImportError:
        pass


_set_pil_pixel_limit()


def _image_to_jpeg(data: bytes) -> tuple[bytes, tuple[int, int]]:
    """安全解码图片并转 JPEG；像素超限 warning 也按异常拒绝。"""
    from PIL import Image

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            size = source.size
            converted = source.convert("RGB") if source.mode != "RGB" else source
            try:
                buf = io.BytesIO()
                converted.save(buf, "JPEG", quality=88)
                return buf.getvalue(), size
            finally:
                if converted is not source:
                    converted.close()


async def _read_aes128_key(resp: aiohttp.ClientResponse) -> Optional[bytes]:
    """读取 AES-128 key；15/17+ 字节均拒绝，且最多读取 17 字节。"""
    try:
        # StreamReader.read(n) 允许在尚未 EOF 时返回少于 n 字节，单次 read(17)
        # 仍可能把分块到达的超长响应误判成 16 字节；readexactly 可避免该竞态。
        data = await resp.content.readexactly(_MAX_KEY + 1)
    except asyncio.IncompleteReadError as exc:
        data = exc.partial
    return data if len(data) == _MAX_KEY else None


class _BaseProxy:
    """本地代理公共部分：会话（cookie jar 共享）、端口、防盗链头、重试、Range 转发。"""

    _endpoint = ""

    def __init__(self) -> None:
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._port: int = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._referer: str = ""
        self._url: str = ""
        # 每个代理固定使用创建时的策略快照，避免设置切换导致 URL 校验与
        # 连接器策略不一致。
        self._allow_intranet: bool = _allow_intranet()
        self._background_tasks = BackgroundTasks()
        # 同一缓存资源只允许一个生产任务；请求方共享结果，代理负责其生命周期。
        self._inflight: dict[Hashable, asyncio.Task[Any]] = {}

    @property
    def port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        return self._site is not None

    @property
    def playlist_url(self) -> str:
        return f"http://127.0.0.1:{self._port}{self._endpoint}"

    async def start(self, url: str) -> None:
        """启动本地代理（复用 setup_* 阶段注入的会话，cookie jar 连续）。"""
        if self._site is not None:
            await self.stop()
        self._url = url
        if self._session is None:
            self._session = make_session(allow_intranet=self._allow_intranet)
        app = web.Application()
        self._register_routes(app)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)  # 端口 0 = 自动分配
        await self._site.start()
        sockets = self._site._server.sockets  # noqa: SLF001
        if sockets:
            self._port = sockets[0].getsockname()[1]
        log.info("本地代理已启动: %s", self.playlist_url)

    def _register_routes(self, app: web.Application) -> None:  # noqa: ANN001
        raise NotImplementedError

    async def stop(self) -> None:
        # 预热任务会使用当前 session；必须先取消等待，再关闭 session。
        await self._background_tasks.cancel_all()
        self._inflight.clear()
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("停代理 site 异常: %s", e)
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as e:  # noqa: BLE001
                log.debug("停代理 runner 异常: %s", e)
            self._runner = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception as e:  # noqa: BLE001
                log.debug("关 session 异常: %s", e)
            self._session = None
        self._port = 0
        log.info("本地代理已停止")

    def _singleflight_task(
        self,
        key: Hashable,
        producer: Callable[[], Coroutine[Any, Any, _T]],
        *,
        name: str,
    ) -> asyncio.Task[_T]:
        """取得或创建某个缓存资源唯一的生产任务。"""
        existing = self._inflight.get(key)
        if existing is not None:
            return existing  # type: ignore[return-value]

        task = self._background_tasks.create(producer(), name=name)
        self._inflight[key] = task

        def remove_if_current(done: asyncio.Task[Any]) -> None:
            if self._inflight.get(key) is done:
                self._inflight.pop(key, None)

        task.add_done_callback(remove_if_current)
        return task

    async def _singleflight(
        self,
        key: Hashable,
        producer: Callable[[], Coroutine[Any, Any, _T]],
        *,
        name: str,
    ) -> _T:
        """共享同一生产任务；单个等待方取消不会连带取消共享下载。"""
        task = self._singleflight_task(key, producer, name=name)
        return await asyncio.shield(task)

    def _default_headers(self) -> dict[str, str]:
        headers = {"User-Agent": _BROWSER_UA}
        if self._referer:
            headers["Referer"] = self._referer
        return headers

    async def _get(self, url: str, *, headers: Optional[dict[str, str]] = None,
                   retries: int = 1,
                   timeout: Optional[aiohttp.ClientTimeout] = None,
                   ) -> Optional[aiohttp.ClientResponse]:
        """内部请求（m3u8/探测/预热/密钥/整段缓冲）：防盗链头 + 重试。

        走 safe_get（关自动重定向 + 每跳 URL 校验），SSRF 由会话的
        SSRFSafeConnector 在连接层兜底，堵 302 绕过与 DNS rebinding。
        """
        assert self._session is not None
        hdrs = self._default_headers()
        if headers:
            hdrs.update(headers)
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                resp = await safe_get(
                    self._session, url, headers=hdrs, timeout=timeout,
                    allow_intranet=self._allow_intranet,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                last_exc = e
                if attempt < retries:
                    await asyncio.sleep(0.4 * (attempt + 1))
                continue
            if resp.status >= 500 and attempt < retries:
                log.warning("上游 HTTP %s（%s），重试", resp.status, url)
                resp.close()
                await asyncio.sleep(0.4 * (attempt + 1))
                continue
            return resp
        log.warning("请求上游 %s 失败: %s", url, last_exc)
        return None

    async def _forward_url(self, request: web.Request, real_url: str,
                           what: str) -> web.StreamResponse:
        """转发单个上游 URL（保留 Range/206 语义 + 防盗链头 + 重试）。

        mpv 依赖 Range 分段拉取（206），客户端头原样转发；
        上游 5xx / 网络错误重试一次；200 但返回 HTML 时判为登录墙/防盗链页。
        """
        assert self._session is not None
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "connection", "accept-encoding")}
        headers.update(self._default_headers())
        resp: Optional[aiohttp.ClientResponse] = None
        for attempt in range(2):
            try:
                # safe_get 关自动重定向 + 每跳 URL 校验，SSRF 由连接器兜底
                resp = await safe_get(
                    self._session, real_url, headers=headers,
                    timeout=_TIMEOUT_FORWARD,
                    allow_intranet=self._allow_intranet,
                )
            except UrlBlockedError as e:
                log.warning("代理%s URL 被安全策略拦截: %s", what, e)
                return web.Response(status=403, text="upstream blocked")
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                log.warning("代理%s %s 网络错误: %s", what, real_url, e)
                resp = None
            if resp is not None and resp.status < 500:
                break
            if resp is not None:
                resp.close()
                if attempt == 0:
                    log.warning("%s上游返回 HTTP %s，重试: %s",
                                what, resp.status, real_url)
                    await asyncio.sleep(0.4)
        if resp is None:
            return web.Response(status=502, text="proxy error")
        if resp.status not in (200, 206):
            log.warning("%s上游返回 HTTP %s: %s", what, resp.status, real_url)
            status = resp.status
            resp.close()
            return web.Response(status=status, text="upstream error")
        # 先取前 4KB 判断是否为 HTML 错误页（防盗链/登录墙常 200 返回 HTML）
        try:
            first = await resp.content.read(4096)
        except asyncio.CancelledError:
            resp.close()
            raise
        except asyncio.TimeoutError:
            log.warning("%s上游读取超时: %s", what, real_url)
            resp.close()
            return web.Response(status=504, text="upstream timeout")
        except (aiohttp.ClientError, ConnectionError, OSError) as e:
            log.warning("%s上游读取失败: %s (%s)", what, real_url, e)
            resp.close()
            return web.Response(status=502, text="upstream read error")
        if first and _looks_html(first):
            log.warning("%s上游返回 HTML 页面（疑似登录墙/防盗链页）: %s",
                        what, real_url)
            resp.close()
            return web.Response(status=502, text="upstream html")

        stream = web.StreamResponse(status=resp.status)
        stream.content_type = "video/mp4"  # 强制视频 MIME，避免按扩展名误判
        for h in ("Content-Length", "Content-Range", "Accept-Ranges", "Cache-Control"):
            if h in resp.headers:
                stream.headers[h] = resp.headers[h]
        prepared = False
        try:
            await stream.prepare(request)
            prepared = True
            if first:
                await stream.write(first)
            async for chunk in resp.content.iter_chunked(64 * 1024):
                try:
                    await stream.write(chunk)
                except (ConnectionResetError, ConnectionError, OSError):
                    log.debug("客户端提前断开（正常）")
                    break
        except asyncio.TimeoutError:
            if not prepared:
                raise
            log.warning("%s上游流式读取超时: %s", what, real_url)
        except asyncio.CancelledError:
            log.debug("%s传输中断", what)
            raise
        except (aiohttp.ClientError, ConnectionResetError, ConnectionError, OSError):
            if not prepared:
                raise
            log.debug("%s传输中断", what)
        finally:
            resp.close()
        try:
            await stream.write_eof()
        except (ConnectionResetError, ConnectionError, OSError):
            pass
        return stream


class HlsProxy(_BaseProxy):
    """m3u8 本地代理：重写后的播放列表 + 分片/密钥/初始化段转发。"""

    _endpoint = "/playlist.m3u8"

    def __init__(self) -> None:
        super().__init__()
        self._segments: list[str] = []
        self._keys: list[str] = []
        self._maps: list[str] = []
        self._key_cache: dict[str, bytes] = {}
        self._playlist: str = ""
        # 图像流模式：分片是真实图片（PNG/JPEG...），转 MJPEG/AVI 后播放
        self._mode: str = "video"  # "video" | "image" | "hybrid"
        self._jpeg_cache: dict[int, bytes] = {}
        self._jpeg_sizes: dict[int, tuple[int, int]] = {}
        self._jpeg_cache_order: list[int] = []  # LRU 顺序（上限 _JPEG_CACHE_LIMIT）
        # hybrid 模式：PNG 封面 + TS 视频混合分片 → 剥前缀缓存
        self._ts_cache: dict[int, bytes] = {}
        self._ts_cache_order: list[int] = []

    async def start(self, segments: list[str], playlist: str,
                    keys: Optional[list[str]] = None,
                    maps: Optional[list[str]] = None,
                    mode: str = "video", *, referer: str = "") -> None:
        """启动 HLS 代理。

        segments/keys/maps 是解析好的绝对 URL；playlist 是重写后的 m3u8
        文本（其中 ``{BASE}`` 占位符稍后替换为实际端口）。
        mode:
        - "video"：普通视频分片（转发，保留 Range/206）
        - "image"：纯图片分片（漫画页），转 MJPEG/AVI 提供
        - "hybrid"：PNG 封面+TS 视频混合分片，剥掉封面按 .ts 提供
        """
        self._segments = segments
        self._keys = list(keys or [])
        self._maps = list(maps or [])
        self._key_cache = {}
        self._playlist = playlist
        self._mode = mode
        self._jpeg_cache = {}
        self._jpeg_sizes = {}
        self._jpeg_cache_order = []
        self._ts_cache = {}
        self._ts_cache_order = []
        self._referer = referer
        await super().start("")

    def _register_routes(self, app: web.Application) -> None:  # noqa: ANN001
        app.router.add_get("/playlist.m3u8", self._handle_playlist)
        if self._mode == "image":
            app.router.add_get("/seg/{index}.avi", self._handle_image_segment)
        elif self._mode == "hybrid":
            app.router.add_get("/seg/{index}.ts", self._handle_hybrid_segment)
        else:
            app.router.add_get("/seg/{index}.mp4", self._handle_segment)
        app.router.add_get("/key/{index}.key", self._handle_key)
        app.router.add_get("/map/{index}.mp4", self._handle_map)

    # ------------------------------------------------------------------ #
    async def _handle_playlist(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=self._playlist,
            content_type="application/vnd.apple.mpegurl",
        )

    async def _handle_segment(self, request: web.Request) -> web.StreamResponse:
        index = int(request.match_info["index"])
        if index >= len(self._segments):
            return web.Response(status=404, text="segment not found")
        return await self._forward_url(request, self._segments[index], "分片")

    async def _handle_map(self, request: web.Request) -> web.StreamResponse:
        index = int(request.match_info["index"])
        if index >= len(self._maps):
            return web.Response(status=404, text="map not found")
        return await self._forward_url(request, self._maps[index], "初始化段")

    async def _handle_key(self, request: web.Request) -> web.Response:
        """转发 AES-128 密钥（内存缓存 + 16 字节校验）。

        ffmpeg 的 HLS demuxer 请求密钥时会带 Range 头，上游会因此返回
        206 部分内容；密钥只有 16 字节，这里忽略 Range 总是取完整内容，
        再以 200 返回。密钥不是 16 字节 = 上游返回了错误内容（常见：
        防盗链把 HTML 错误页伪装成 200），直接 502 让用户看到友好提示。
        """
        index = int(request.match_info["index"])
        if index >= len(self._keys):
            return web.Response(status=404, text="key not found")
        url = self._keys[index]
        if url not in self._key_cache:
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ("host", "connection", "accept-encoding",
                                            "range", "if-range")}
            status, message = await self._singleflight(
                ("key", url),
                lambda: self._load_key(url, headers),
                name=f"load-hls-key-{index}",
            )
            if status != 200:
                return web.Response(status=status, text=message)
        return web.Response(
            body=self._key_cache[url],
            content_type="application/octet-stream",
        )

    async def _load_key(
        self, url: str, headers: dict[str, str]
    ) -> tuple[int, str]:
        """抓取并缓存密钥；返回要提供给所有并发等待方的结果。"""
        resp = await self._get(url, headers=headers, timeout=_TIMEOUT)
        if resp is None:
            return 502, "proxy error"
        try:
            if resp.status not in (200, 206):
                log.warning("密钥 %s 上游返回 HTTP %s", url, resp.status)
                return resp.status, "upstream error"
            # 多读 1 字节识别"上游多给内容"：read(17) 若上游返回 ≥17 字节，
            # 说明不是真正的 16 字节 AES 密钥（常见：防盗链把 HTML 伪装成 200）
            data = await _read_aes128_key(resp)
        finally:
            resp.close()
        if data is None:
            log.warning("密钥长度异常（AES-128 应恰好为 16 字节），"
                        "可能被防盗链拦截: %s", url)
            return 502, "key length invalid"
        self._key_cache[url] = data
        log.debug("密钥已缓存: %s (16 字节)", url)
        return 200, ""

    # ------------------------------------------------------------------ #
    # hybrid 模式：PNG 封面 + TS 视频混合分片 → 剥掉封面按 TS 提供
    # ------------------------------------------------------------------ #
    async def _handle_hybrid_segment(self, request: web.Request) -> web.Response:
        index = int(request.match_info["index"])
        if index >= len(self._segments):
            return web.Response(status=404, text="segment not found")
        if index not in self._ts_cache:
            ok = await self._ensure_hybrid_segment(index, request)
            if not ok:
                return web.Response(status=502, text="hybrid fetch failed")
        self._schedule_warm(index)
        return web.Response(body=self._ts_cache[index],
                            content_type="video/mp2t")

    async def _ensure_hybrid_segment(
        self,
        index: int,
        request: Optional[web.Request] = None,
        *,
        retries: int = 1,
    ) -> bool:
        """等待该分片唯一的抓取任务，预热与播放器请求共享结果。"""
        if index in self._ts_cache:
            return True
        return await self._singleflight(
            ("hybrid", index),
            lambda: self._buffer_hybrid(index, request, retries=retries),
            name=f"load-hls-segment-{index}",
        )

    async def _buffer_hybrid(
        self,
        index: int,
        request: Optional[web.Request] = None,
        *,
        retries: int = 1,
    ) -> bool:
        """下载混合分片 → 剥掉封面 → 缓存。"""
        url = self._segments[index]
        headers = (
            {k: v for k, v in request.headers.items()
             if k.lower() not in ("host", "connection", "accept-encoding",
                                  "range", "if-range")}
            if request is not None else None
        )
        resp = await self._get(
            url, headers=headers, retries=retries, timeout=_TIMEOUT_BIG
        )
        if resp is None:
            log.warning("混合分片 %d 上游不可达: %s", index, url)
            return False
        if resp.status != 200:
            log.warning("混合分片 %d 上游返回 HTTP %s", index, resp.status)
            resp.close()
            return False
        try:
            data = await _read_capped(resp, f"混合分片 {index}")
        finally:
            resp.close()
        if data is None:
            return False
        if _looks_html(data):
            log.warning("混合分片 %d 上游返回 HTML（疑似登录墙/防盗链页）: %s",
                        index, url)
            return False
        off = _find_ts_offset(data)
        if off is None:
            log.warning("混合分片 %d 未找到 TS 同步字节（%d 字节）", index, len(data))
            return False
        self._ts_cache[index] = data[off:]
        self._ts_cache_order.append(index)
        log.debug("混合分片 %d: 剥掉 %d 字节封面，TS %d 字节",
                  index, off, len(self._ts_cache[index]))
        self._trim_cache()
        return True

    def _schedule_warm(self, index: int) -> None:
        """后台预取后续几个分片（hls 顺序播放，提前下载避免 mpv 等待）。"""
        for nxt in (index + 1, index + 2, index + 3):
            if nxt >= len(self._segments) or nxt in self._ts_cache:
                continue
            key = ("hybrid", nxt)
            if key in self._inflight:
                continue
            self._singleflight_task(
                key,
                lambda nxt=nxt: self._buffer_hybrid(nxt, retries=0),
                name=f"warm-hls-segment-{nxt}",
            )

    async def _warm_hybrid_startup(self) -> None:
        """启动期只等待首片，后续分片交给受管后台任务预取。"""
        if not self._segments:
            return
        # 先注册后续预热：首片下载一旦发生网络 await，1-3 就能并发开始，
        # 避免 setup 返回后 mpv 立即请求下一片时后台任务还没有启动。
        self._schedule_warm(0)
        await self._warm_hybrid_segment(0)

    def _trim_cache(self) -> None:
        """简单 LRU：缓存上限 8 个分片（每个约 1~2MB），防止内存膨胀。"""
        while len(self._ts_cache) > 8:
            oldest = self._ts_cache_order.pop(0)
            self._ts_cache.pop(oldest, None)

    async def _warm_hybrid_segment(self, index: int) -> None:
        """预取一个混合分片；若播放器也在请求则复用同一抓取任务。"""
        await self._ensure_hybrid_segment(index, retries=0)

    # ------------------------------------------------------------------ #
    # 图像流模式：图片 → JPEG → 单帧 AVI（惰性转换 + 内存缓存）
    # ------------------------------------------------------------------ #
    async def _handle_image_segment(self, request: web.Request) -> web.StreamResponse:
        index = int(request.match_info["index"])
        if index >= len(self._segments):
            return web.Response(status=404, text="segment not found")
        if index not in self._jpeg_cache:
            ok = await self._singleflight(
                ("image", index),
                lambda: self._convert_segment(index, request),
                name=f"convert-hls-image-{index}",
            )
            if ok is None:
                # 像素炸弹不能回退原始转发，否则只是把危险输入改交给 mpv 解码。
                return web.Response(status=413, text="image pixel limit exceeded")
            if not ok:
                # 转换失败（比如混合流里某段其实是视频）→ 回退原始转发，
                # mpv 也许还能直接吃下
                log.warning("图片分片 %d 转换失败，回退原始转发", index)
                return await self._forward_url(request, self._segments[index], "分片")
        jpeg = self._jpeg_cache[index]
        self._trim_jpeg_cache(index)  # 命中即提升为最近使用
        w, h = self._jpeg_sizes[index]
        avi = _make_avi(jpeg, w, h)
        return web.Response(body=avi, content_type="video/x-msvideo")

    async def _convert_segment(
        self, index: int, request: web.Request
    ) -> Optional[bool]:
        """图片转 JPEG；成功 True、普通失败 False、像素超限 None。"""
        url = self._segments[index]
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "connection", "accept-encoding",
                                        "range", "if-range")}
        resp = await self._get(url, headers=headers, timeout=_TIMEOUT_BIG)
        if resp is None:
            log.warning("图片分片 %d 上游不可达: %s", index, url)
            return False
        if resp.status != 200:
            log.warning("图片分片 %d 上游返回 HTTP %s", index, resp.status)
            resp.close()
            return False
        try:
            data = await _read_capped(resp, f"图片分片 {index}")
        finally:
            resp.close()
        if data is None:
            return False
        try:
            from PIL import Image

            jpeg, size = _image_to_jpeg(data)
            self._jpeg_sizes[index] = size
            self._jpeg_cache[index] = jpeg
            self._trim_jpeg_cache(index)
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
            # 解压炸弹：像素超 MAX_IMAGE_PIXELS（_set_pil_pixel_limit 设的 4096²）
            # Warning 在超限但不足 2× 时触发，Error 在超 2× 时触发，两者都拒
            log.warning("图片分片 %d 像素超限，疑似解压炸弹: %s", index, e)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("图片分片 %d 转换失败: %s", index, e)
            return False
        log.debug("图片分片 %d 已转 JPEG (%d 字节)", index, len(self._jpeg_cache[index]))
        return True

    def _trim_jpeg_cache(self, just_added: int) -> None:
        """图像流 JPEG 缓存 LRU：保留最近 _JPEG_CACHE_LIMIT 张。"""
        # 记录访问顺序（刚写入的放最后）
        if just_added in self._jpeg_cache_order:
            self._jpeg_cache_order.remove(just_added)
        self._jpeg_cache_order.append(just_added)
        while len(self._jpeg_cache) > _JPEG_CACHE_LIMIT and self._jpeg_cache_order:
            oldest = self._jpeg_cache_order.pop(0)
            self._jpeg_cache.pop(oldest, None)
            self._jpeg_sizes.pop(oldest, None)


class DirectProxy(_BaseProxy):
    """非 m3u8 直链的本地代理：防盗链头 + 重试 + 内容模式兼容。

    模式（按首 64KB 探测）：
    - "video"：普通媒体 → 原样转发（保留 Range/206）
    - "image"：纯图片（漫画页直链）→ 转单帧 AVI
    - "hybrid"：PNG 封面 + TS 视频 → 剥掉封面按 TS 提供（缓存 + Range 支持）
    """

    _endpoint = "/stream"

    def __init__(self) -> None:
        super().__init__()
        self._mode: str = "video"
        self._data: Optional[bytes] = None  # image/hybrid 模式的缓冲（剥离/转换后）

    def _register_routes(self, app: web.Application) -> None:  # noqa: ANN001
        app.router.add_get("/stream", self._handle_stream)

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        if self._mode == "video":
            return await self._forward_url(request, self._url, "媒体")
        if self._data is None:
            ok = await self._singleflight(
                ("direct", self._url),
                lambda: self._buffer_once(request),
                name="buffer-direct-media",
            )
            if not ok:
                return web.Response(status=502, text="buffer failed")
            if self._mode == "video":
                # 图片转换失败回退：原样转发
                return await self._forward_url(request, self._url, "媒体")
        return self._serve_cached(request)

    async def _buffer_once(self, request: web.Request) -> bool:
        """整段下载直链 → 按模式剥离/转换 → 缓存。"""
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "connection", "accept-encoding",
                                        "range", "if-range")}
        resp = await self._get(self._url, headers=headers, timeout=_TIMEOUT_BIG)
        if resp is None:
            log.warning("直链上游不可达: %s", self._url)
            return False
        if resp.status != 200:
            log.warning("直链上游返回 HTTP %s: %s", resp.status, self._url)
            resp.close()
            return False
        try:
            data = await _read_capped(resp, "直链媒体")
        finally:
            resp.close()
        if data is None or _looks_html(data):
            return False
        if self._mode == "hybrid":
            off = _find_ts_offset(data)
            if off is None:
                log.warning("直链未找到 TS 同步字节，无法剥离封面")
                return False
            self._data = data[off:]
            log.info("直链已剥离 %d 字节封面，TS %d 字节", off, len(self._data))
        else:  # image
            try:
                from PIL import Image

                jpeg, size = _image_to_jpeg(data)
                self._data = _make_avi(jpeg, *size)
                log.info("直链图片已转 AVI (%d 字节)", len(self._data))
            except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
                log.warning("直链图片像素超限，疑似解压炸弹，拒绝原始转发: %s", e)
                return False
            except Exception as e:  # noqa: BLE001
                log.warning("直链图片转换失败，回退原始转发: %s", e)
                self._mode = "video"
        return True

    def _serve_cached(self, request: web.Request) -> web.Response:
        """从内存缓冲提供内容（支持 Range，mpv seek 会发）。"""
        data = self._data
        assert data is not None
        start, end, status = 0, len(data) - 1, 200
        rng = request.headers.get("Range", "")
        if rng.startswith("bytes="):
            try:
                a, _, b = rng[6:].partition("-")
                s = int(a) if a else 0
                e = int(b) if b else len(data) - 1
                if s >= len(data):
                    return web.Response(
                        status=416,
                        headers={"Content-Range": f"bytes */{len(data)}"},
                    )
                start, end, status = s, min(e, len(data) - 1), 206
            except ValueError:
                pass
        resp = web.Response(body=data[start:end + 1], status=status)
        resp.content_type = ("video/mp2t" if self._mode == "hybrid"
                             else "video/x-msvideo")
        resp.headers["Accept-Ranges"] = "bytes"
        if status == 206:
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        return resp


# --------------------------------------------------------------------------- #
# m3u8 下载 + 重写
# --------------------------------------------------------------------------- #
async def setup_hls_proxy(
    m3u8_url: str,
    depth: int = 0,
    *,
    allow_intranet: Optional[bool] = None,
) -> Optional[HlsProxy]:
    """下载 m3u8、启动本地代理、重写分片/密钥/初始化段 URL。

    返回已启动的 HlsProxy（其 playlist_url 可直接交给 mpv 播放），
    普通初始化失败返回 None；安全拒绝抛 UrlBlockedError。depth 用于主播放列表递归跟进。
    """
    proxy = HlsProxy()
    if allow_intranet is not None:
        proxy._allow_intranet = bool(allow_intranet)
    proxy._session = make_session(allow_intranet=proxy._allow_intranet)
    session = proxy._session

    async def abort(reason: str) -> None:
        """失败路径：关掉会话并返回 None。"""
        log.warning(reason)
        await session.close()
        return None

    try:
        # 防盗链 Referer 先从原始 URL 取（m3u8 本身也可能校验 Referer）
        proxy._referer = _origin(m3u8_url)
        # SSRF 入口校验：被拦截时抛 UrlBlockedError（区别于普通失败，避免 fallback 到 mpv）
        validate_upstream_url(m3u8_url, allow_intranet=proxy._allow_intranet)
        resp = await proxy._get(m3u8_url, retries=1, timeout=_TIMEOUT)
        if resp is None:
            return await abort(f"下载 m3u8 失败（网络错误）: {m3u8_url}")
        if resp.status != 200:
            return await abort(f"下载 m3u8 失败: HTTP {resp.status}")
        try:
            body = await _read_capped(resp, "m3u8 播放列表", cap=_MAX_M3U8)
        finally:
            resp.close()
        if body is None:
            return await abort(f"m3u8 超过 {_MAX_M3U8 // 1024}KB，疑似异常: {m3u8_url}")
        if _looks_html(body):
            return await abort(f"m3u8 返回 HTML（疑似登录墙/防盗链页）: {m3u8_url}")
        text = body.decode("utf-8", errors="replace")

        # 基址用重定向后的最终地址（302 换域名时相对分片按旧地址解析会 404）
        final_url = str(resp.url)
        base = urljoin(final_url, ".")
        origin = _origin(final_url)
        proxy._referer = origin

        # 主播放列表（只有 #EXT-X-STREAM-INF + 变体）无法直接播放 → 跟进第一个变体
        if "#EXT-X-STREAM-INF" in text and "#EXTINF" not in text:
            variant = next(
                (line.strip() for line in text.splitlines()
                 if line.strip() and not line.strip().startswith("#")),
                None,
            )
            if variant and depth < 3:
                variant_url = urljoin(base, variant)
                try:
                    validate_upstream_url(variant_url, allow_intranet=proxy._allow_intranet)
                except UrlBlockedError:
                    await session.close()
                    raise
                log.info("主播放列表，跟进变体: %s", variant_url)
                await session.close()
                return await setup_hls_proxy(
                    variant_url, depth + 1, allow_intranet=proxy._allow_intranet
                )
            return await abort("主播放列表无可用变体，跳过代理")

        if "#EXTINF" not in text:
            return await abort("m3u8 无 #EXTINF（也不是主播放列表），跳过代理")

        if "#EXT-X-ENDLIST" not in text:
            return await abort("直播流（无 #EXT-X-ENDLIST）当前不支持安全代理")

        if "METHOD=SAMPLE-AES" in text:
            log.warning("检测到 SAMPLE-AES（DRM）加密，mpv 无法解密")

        # 先收集分片，探测第一个分片的真实内容：
        # 图片（漫画/图文番）→ 图像流模式（转 MJPEG/AVI，否则 ffmpeg 播不了）
        raw_segments = [
            urljoin(base, line.strip())
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not raw_segments:
            return await abort("m3u8 无分片可重写")

        # SSRF 防护：校验所有分片 / 密钥 / 初始化段 URL。被拦截的整段 m3u8 拒绝代理
        # （单条 m3u8 混入内网地址基本就是恶意构造，不部分代理）。
        intranet = proxy._allow_intranet
        for seg in raw_segments:
            try:
                validate_upstream_url(seg, allow_intranet=intranet)
            except UrlBlockedError:
                await session.close()
                raise

        image_mode = False
        hybrid_mode = False
        try:
            probe_resp = await proxy._get(
                raw_segments[0],
                headers={"Range": "bytes=0-65535"},
                retries=1,
                timeout=_TIMEOUT,
            )
            if probe_resp is not None and probe_resp.status in (200, 206):
                try:
                    probe = await _read_capped(
                        probe_resp, "分片探测", cap=_MAX_PROBE
                    )
                finally:
                    probe_resp.close()
                if probe is None:
                    probe = b""  # 探测超限按未知类型处理（走 video 模式）
                kind = _detect_image(probe)
                ts_off = _find_ts_offset(probe)
                if kind and ts_off is not None:
                    hybrid_mode = True
                    log.info("检测到混合分片（%s 封面 + TS 视频，TS 起点 %d），"
                             "启用封面剥离模式", kind, ts_off)
                elif kind:
                    image_mode = True
                    log.info("检测到图像流（%s 分片），启用图片→MJPEG/AVI 转换模式",
                             kind)
            elif probe_resp is not None:
                probe_resp.close()
        except UrlBlockedError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("分片类型探测失败（按视频流处理）: %s", e)

        segments: list[str] = []
        keys: list[str] = []
        maps: list[str] = []
        rewritten: list[str] = []
        if hybrid_mode:
            seg_ext = ".ts"
        elif image_mode:
            seg_ext = ".avi"
        else:
            seg_ext = ".mp4"
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                # 密钥 / 初始化段：把 URI="..." 换成代理端点
                if s.startswith("#EXT-X-KEY") or s.startswith("#EXT-X-MAP"):
                    m = _ATTR_URI_RE.search(s)
                    if m:
                        full = urljoin(base, m.group(1))
                        try:
                            validate_upstream_url(full, allow_intranet=intranet)
                        except UrlBlockedError:
                            await session.close()
                            raise
                        if s.startswith("#EXT-X-KEY"):
                            idx = len(keys)
                            keys.append(full)
                            new_uri = f"{{BASE}}/key/{idx}.key"
                        else:
                            idx = len(maps)
                            maps.append(full)
                            new_uri = f"{{BASE}}/map/{idx}.mp4"
                        s = s[:m.start(1)] + new_uri + s[m.end(1):]
                rewritten.append(s)
            else:
                # 分片（相对或绝对都解析成绝对 URL，再指向代理）
                segments.append(urljoin(base, s))
                rewritten.append(f"{{BASE}}/seg/{len(segments) - 1}{seg_ext}")

        await proxy.start(segments, "\n".join(rewritten), keys, maps,
                          mode="hybrid" if hybrid_mode else "image" if image_mode else "video",
                          referer=origin)
        # 用实际端口替换占位符
        proxy._playlist = proxy._playlist.replace("{BASE}", f"http://127.0.0.1:{proxy.port}")
        # 混合流：setup 只等待首片；后续分片在首片网络等待期间已由
        # BackgroundTasks 并发预取，既不串行阻塞 SOAP，也尽量赶在 mpv 前面。
        if hybrid_mode:
            await proxy._warm_hybrid_startup()
        log.info("m3u8 重写完成: %d 个分片, %d 个密钥, %d 个初始化段 → %s",
                 len(segments), len(keys), len(maps), proxy.playlist_url)
        return proxy
    except UrlBlockedError:
        # SSRF 安全拒绝：必须向上抛，由 renderer_bridge 走 ERROR_OCCURRED，
        # 不能被这里吞成 None（否则会被当普通失败 fallback 到 mpv，guard 白做）
        await proxy.stop()
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("HLS 代理初始化失败: %s", e)
        try:
            await proxy.stop()
        except Exception:  # noqa: BLE001
            pass
        return None


async def setup_direct_proxy(
    url: str, *, allow_intranet: Optional[bool] = None
) -> Optional[DirectProxy]:
    """非 HLS 直链的本地代理（防盗链头 + 重试 + 内容模式探测）。

    返回已启动的 DirectProxy（playlist_url 交给 mpv），失败返回 None。
    """
    proxy = DirectProxy()
    if allow_intranet is not None:
        proxy._allow_intranet = bool(allow_intranet)
    proxy._session = make_session(allow_intranet=proxy._allow_intranet)

    async def abort(reason: str) -> None:
        log.warning(reason)
        await proxy.stop()
        return None

    try:
        proxy._referer = _origin(url)
        # SSRF 入口校验：被拦截时抛 UrlBlockedError（区别于普通失败，避免 fallback 到 mpv）
        validate_upstream_url(url, allow_intranet=proxy._allow_intranet)
        probe_resp = await proxy._get(
            url,
            headers={"Range": "bytes=0-65535"},
            retries=1,
            timeout=_TIMEOUT,
        )
        if probe_resp is None:
            return await abort(f"直链探测失败（网络错误）: {url}")
        if probe_resp.status not in (200, 206):
            status = probe_resp.status
            probe_resp.close()
            return await abort(f"直链探测失败: HTTP {status} ({url})")
        try:
            probe = await _read_capped(
                probe_resp, "直链探测", cap=_MAX_PROBE
            )
        finally:
            probe_resp.close()
        if probe is None:
            return await abort(
                f"直链探测超过 {_MAX_PROBE // 1024}KB，疑似异常: {url}"
            )

        # 实际是 m3u8 播放列表（URL 不带 .m3u8 后缀的情况）→ 转 HLS 代理
        if probe.startswith(b"#EXTM3U"):
            log.info("直链实为 m3u8 播放列表，转 HLS 代理")
            await proxy.stop()
            return await setup_hls_proxy(
                url, allow_intranet=proxy._allow_intranet
            )

        kind = _detect_image(probe)
        off = _find_ts_offset(probe)
        if kind and off is not None:
            proxy._mode = "hybrid"
            log.info("直链检测为混合内容（%s 封面 + TS），启用封面剥离", kind)
        elif kind:
            proxy._mode = "image"
            log.info("直链检测为图片，启用图片→AVI 转换")
        else:
            proxy._mode = "video"
        await proxy.start(url)
        log.info("直链代理就绪: %s（%s 模式）", proxy.playlist_url, proxy._mode)
        return proxy
    except UrlBlockedError:
        # SSRF 安全拒绝：透传，不 fallback 到 mpv
        await proxy.stop()
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("直链代理初始化失败: %s", e)
        try:
            await proxy.stop()
        except Exception:  # noqa: BLE001
            pass
        return None
