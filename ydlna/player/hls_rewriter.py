"""HLS 流本地代理 —— 解决 ffmpeg 对 .jpg 分片误判为图片的问题。

背景
----
某些 DLNA 投屏源（如部分看番软件）的 m3u8 分片 URL 用 ``.jpg`` 扩展名伪装，
但实际内容是 mp4 容器包裹的 h264 视频。ffmpeg 的 HLS demuxer 探测分片时，
``.jpg`` 扩展名让 image2 demuxer 得分碾压 mp4 → 把视频误判成图片 → 解码失败
（症状：媒体已装载但黑屏、"Invalid data found when processing input"）。

已验证：分片扩展名改成 ``.mp4`` 后 mpv 能正确识别 h264 1080p 并正常播放。

另外，加密流（``#EXT-X-KEY``，AES-128）的密钥 URI 和 fMP4 的初始化段
（``#EXT-X-MAP``）若保持相对地址，会解析到本地代理的 404 —— 也必须
一并重写到本地端点并转发。

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

方案（直接接收播放，无文件、无额外步骤）
--------------------------------------
1. 收到 m3u8 URL → 下载 m3u8 文本
2. 启动本地代理（aiohttp，内存态），提供端点：
   - ``/playlist.m3u8``：返回重写后的 m3u8
   - ``/seg/{n}.mp4``：转发真实分片（保留 Range/206 语义 + 防盗链 header）
   - ``/seg/{n}.avi``：图像流分片（PNG→JPEG→AVI，惰性转换 + 内存缓存）
   - ``/key/{n}.key``：转发 AES-128 密钥（内存缓存，密钥很小）
   - ``/map/{n}.mp4``：转发 fMP4 初始化段
   所有 URI（分片/密钥/初始化段）都按 m3u8 的基地址解析成绝对 URL 后转发
3. mpv 只播放一个 URL：``http://127.0.0.1:{port}/playlist.m3u8`` —— 走标准 HLS 路径
"""
from __future__ import annotations

import asyncio
import io
import re
import struct
from typing import Optional
from urllib.parse import urljoin

import aiohttp
from aiohttp import web

from ..logger import get_logger

log = get_logger("player.hls")

# #EXT-X-KEY / #EXT-X-MAP 里的 URI="..." 属性
_ATTR_URI_RE = re.compile(r'URI="([^"]*)"')

# 常见图片魔数（探测"图文流"用）
_IMAGE_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF8", "gif"),
    (b"BM", "bmp"),
    (b"RIFF", "webp"),
)


def _detect_image(data: bytes) -> Optional[str]:
    """按魔数判断数据是否为图片，返回类型名或 None。"""
    for magic, kind in _IMAGE_MAGICS:
        if data.startswith(magic):
            if kind == "webp" and data[8:12] != b"WEBP":
                continue
            return kind
    return None


def _find_ts_offset(data: bytes) -> Optional[int]:
    """找 MPEG-TS 数据起点：第一个 0x47 同步字节（且 188 字节后有第二个同步）。

    某些视频站的分片是"小 PNG 封面 + 附加完整 TS 视频"的混合文件，
    播放时必须剥掉 PNG 前缀、从 TS 起点提供。
    """
    for i in range(len(data) - 188):
        if data[i] == 0x47 and data[i + 188] == 0x47:
            return i
    return None


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


class HlsProxy:
    """m3u8 本地代理：提供重写后的播放列表 + 分片/密钥/初始化段转发。"""

    def __init__(self) -> None:
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._port: int = 0
        self._segments: list[str] = []
        self._keys: list[str] = []
        self._maps: list[str] = []
        self._key_cache: dict[str, bytes] = {}
        self._playlist: str = ""
        self._session: Optional[aiohttp.ClientSession] = None
        # 图像流模式：分片是真实图片（PNG/JPEG...），转 MJPEG/AVI 后播放
        self._mode: str = "video"  # "video" | "image" | "hybrid"
        self._jpeg_cache: dict[int, bytes] = {}
        self._jpeg_sizes: dict[int, tuple[int, int]] = {}
        # hybrid 模式：PNG 封面 + TS 视频混合分片 → 剥前缀缓存
        self._ts_cache: dict[int, bytes] = {}
        self._ts_cache_order: list[int] = []

    @property
    def port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        return self._site is not None

    @property
    def playlist_url(self) -> str:
        return f"http://127.0.0.1:{self._port}/playlist.m3u8"

    async def start(
        self,
        segments: list[str],
        playlist: str,
        keys: Optional[list[str]] = None,
        maps: Optional[list[str]] = None,
        mode: str = "video",
    ) -> None:
        """启动代理。

        segments/keys/maps 是解析好的绝对 URL；playlist 是重写后的 m3u8
        文本（其中 ``{BASE}`` 占位符稍后替换为实际端口）。
        mode:
        - "video"：普通视频分片（转发，保留 Range/206）
        - "image"：纯图片分片（漫画页），转 MJPEG/AVI 提供
        - "hybrid"：PNG 封面+TS 视频混合分片，剥掉封面按 .ts 提供
        """
        if self._site is not None:
            await self.stop()
        self._segments = segments
        self._keys = list(keys or [])
        self._maps = list(maps or [])
        self._key_cache = {}
        self._playlist = playlist
        self._session = aiohttp.ClientSession()
        self._mode = mode
        self._jpeg_cache = {}
        self._jpeg_sizes = {}
        self._ts_cache = {}
        self._ts_cache_order = []

        app = web.Application()
        app.router.add_get("/playlist.m3u8", self._handle_playlist)
        if mode == "image":
            app.router.add_get("/seg/{index}.avi", self._handle_image_segment)
        elif mode == "hybrid":
            app.router.add_get("/seg/{index}.ts", self._handle_hybrid_segment)
        else:
            app.router.add_get("/seg/{index}.mp4", self._handle_segment)
        app.router.add_get("/key/{index}.key", self._handle_key)
        app.router.add_get("/map/{index}.mp4", self._handle_map)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)  # 端口 0 = 自动分配
        await self._site.start()
        sockets = self._site._server.sockets  # noqa: SLF001
        if sockets:
            self._port = sockets[0].getsockname()[1]
        log.info("HLS 代理已启动: %s，%d 个分片，%d 个密钥，%d 个初始化段",
                 self.playlist_url, len(segments), len(self._keys), len(self._maps))

    async def stop(self) -> None:
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
        log.info("HLS 代理已停止")

    # ------------------------------------------------------------------ #
    # 上游请求（转发客户端 header，防止防盗链）
    # ------------------------------------------------------------------ #
    async def _open_upstream(
        self,
        url: str,
        request: web.Request,
        *,
        strip_range: bool = False,
    ) -> Optional[aiohttp.ClientResponse]:
        assert self._session is not None
        try:
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ("host", "connection", "accept-encoding")
                       and not (strip_range and k.lower() in ("range", "if-range"))}
            return await self._session.get(url, headers=headers)
        except Exception as e:  # noqa: BLE001
            log.warning("代理上游 %s 失败: %s", url, e)
            return None

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

    async def _forward_url(
        self, request: web.Request, real_url: str, what: str
    ) -> web.StreamResponse:
        """转发单个上游 URL（保留 Range/206 语义，mpv 依赖分段拉取）。"""
        log.debug("代理%s %d → %s", what, request.match_info.get("index"), real_url)
        resp = await self._open_upstream(real_url, request)
        if resp is None:
            return web.Response(status=502, text="proxy error")
        if resp.status not in (200, 206):
            log.warning("%s上游返回 HTTP %s: %s", what, resp.status, real_url)
            return web.Response(status=resp.status, text="upstream error")

        stream = web.StreamResponse(status=resp.status)
        stream.content_type = "video/mp4"  # 强制视频 MIME，避免按扩展名误判
        for h in ("Content-Length", "Content-Range", "Accept-Ranges", "Cache-Control"):
            if h in resp.headers:
                stream.headers[h] = resp.headers[h]
        await stream.prepare(request)
        try:
            async for chunk in resp.content.iter_chunked(64 * 1024):
                try:
                    await stream.write(chunk)
                except (ConnectionResetError, ConnectionError, OSError):
                    log.debug("客户端提前断开（正常）")
                    break
        except (ConnectionResetError, asyncio.CancelledError, ConnectionError):
            log.debug("%s传输中断", what)
        finally:
            resp.close()
        try:
            await stream.write_eof()
        except (ConnectionResetError, ConnectionError, OSError):
            pass
        return stream

    async def _handle_key(self, request: web.Request) -> web.Response:
        """转发 AES-128 密钥（内存缓存——ffmpeg 每个分片都会请求一次密钥）。

        ffmpeg 的 HLS demuxer 请求密钥时会带 Range 头，上游会因此返回
        206 部分内容；密钥只有 16 字节，这里忽略 Range 总是取完整内容，
        再以 200 返回。
        """
        index = int(request.match_info["index"])
        if index >= len(self._keys):
            return web.Response(status=404, text="key not found")
        url = self._keys[index]
        if url not in self._key_cache:
            resp = await self._open_upstream(url, request, strip_range=True)
            if resp is None:
                return web.Response(status=502, text="proxy error")
            if resp.status not in (200, 206):
                log.warning("密钥 %s 上游返回 HTTP %s", url, resp.status)
                return web.Response(status=resp.status, text="upstream error")
            self._key_cache[url] = await resp.read()
            log.debug("密钥已缓存: %s (%d 字节)", url, len(self._key_cache[url]))
        return web.Response(
            body=self._key_cache[url],
            content_type="application/octet-stream",
        )

    # ------------------------------------------------------------------ #
    # hybrid 模式：PNG 封面 + TS 视频混合分片 → 剥掉封面按 TS 提供
    # ------------------------------------------------------------------ #
    async def _handle_hybrid_segment(self, request: web.Request) -> web.Response:
        index = int(request.match_info["index"])
        if index >= len(self._segments):
            return web.Response(status=404, text="segment not found")
        if index not in self._ts_cache:
            ok = await self._buffer_hybrid(index, request)
            if not ok:
                return web.Response(status=502, text="hybrid fetch failed")
        self._schedule_warm(index)
        return web.Response(body=self._ts_cache[index],
                            content_type="video/mp2t")

    async def _buffer_hybrid(self, index: int, request: web.Request) -> bool:
        """下载混合分片 → 剥掉封面 → 缓存。"""
        url = self._segments[index]
        resp = await self._open_upstream(url, request, strip_range=True)
        if resp is None:
            log.warning("混合分片 %d 上游不可达: %s", index, url)
            return False
        if resp.status != 200:
            log.warning("混合分片 %d 上游返回 HTTP %s", index, resp.status)
            return False
        data = await resp.read()
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
            if nxt in self._ts_cache_order:  # 已在预取队列
                continue
            self._ts_cache_order.append(nxt)
            asyncio.create_task(self._warm_hybrid_segment(nxt))

    def _trim_cache(self) -> None:
        """简单 LRU：缓存上限 8 个分片（每个约 1~2MB），防止内存膨胀。"""
        while len(self._ts_cache) > 8:
            oldest = self._ts_cache_order.pop(0)
            self._ts_cache.pop(oldest, None)

    async def _warm_hybrid_segment(self, index: int) -> None:
        """预取一个混合分片到缓存。"""
        url = self._segments[index]
        try:
            assert self._session is not None
            resp = await self._session.get(url)
            if resp.status != 200:
                resp.close()
                return
            data = await resp.read()
        except Exception as e:  # noqa: BLE001
            log.debug("混合分片 %d 预热失败: %s", index, e)
            return
        off = _find_ts_offset(data)
        if off is not None:
            self._ts_cache[index] = data[off:]
            log.debug("混合分片 %d 已预热（TS %d 字节）", index,
                      len(self._ts_cache[index]))
        self._trim_cache()

    # ------------------------------------------------------------------ #
    # 图像流模式：图片 → JPEG → 单帧 AVI（惰性转换 + 内存缓存）
    # ------------------------------------------------------------------ #
    async def _handle_image_segment(self, request: web.Request) -> web.Response:
        index = int(request.match_info["index"])
        if index >= len(self._segments):
            return web.Response(status=404, text="segment not found")
        if index not in self._jpeg_cache:
            ok = await self._convert_segment(index, request)
            if not ok:
                return web.Response(status=502, text="image conversion failed")
        jpeg = self._jpeg_cache[index]
        w, h = self._jpeg_sizes[index]
        avi = _make_avi(jpeg, w, h)
        return web.Response(body=avi, content_type="video/x-msvideo")

    async def _convert_segment(self, index: int, request: web.Request) -> bool:
        """下载原始图片分片 → 转 JPEG → 缓存。失败返回 False。"""
        url = self._segments[index]
        resp = await self._open_upstream(url, request, strip_range=True)
        if resp is None:
            log.warning("图片分片 %d 上游不可达: %s", index, url)
            return False
        if resp.status != 200:
            log.warning("图片分片 %d 上游返回 HTTP %s", index, resp.status)
            return False
        data = await resp.read()
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(data))
            self._jpeg_sizes[index] = img.size
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=88)
            self._jpeg_cache[index] = buf.getvalue()
        except Exception as e:  # noqa: BLE001
            log.warning("图片分片 %d 转换失败: %s", index, e)
            return False
        log.debug("图片分片 %d 已转 JPEG (%d 字节)", index, len(self._jpeg_cache[index]))
        return True


async def _probe_first_bytes(url: str, size: int = 32) -> bytes:
    """取 URL 内容的前 size 字节（用 Range 请求，避免下载整个文件）。"""
    async with aiohttp.ClientSession() as session:
        headers = {"Range": f"bytes=0-{size - 1}"}
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status not in (200, 206):
                raise OSError(f"探测失败: HTTP {resp.status}")
            return await resp.read()


# --------------------------------------------------------------------------- #
# m3u8 下载 + 重写
# --------------------------------------------------------------------------- #
async def setup_hls_proxy(m3u8_url: str) -> Optional[HlsProxy]:
    """下载 m3u8、启动本地代理、重写分片/密钥/初始化段 URL。

    返回已启动的 HlsProxy（其 playlist_url 可直接交给 mpv 播放），
    失败返回 None。
    """
    proxy = HlsProxy()
    try:
        async with aiohttp.ClientSession() as session:
            timeout = aiohttp.ClientTimeout(total=15)
            async with session.get(m3u8_url, timeout=timeout) as resp:
                if resp.status != 200:
                    log.warning("下载 m3u8 失败: HTTP %s", resp.status)
                    return None
                text = await resp.text()

        # 主播放列表（只有 #EXT-X-STREAM-INF + 变体 URL）无法直接播放，跳过
        if "#EXTINF" not in text:
            log.warning("m3u8 无 #EXTINF（可能是主播放列表），跳过代理")
            return None

        # m3u8 的基地址：用于把相对 URI（分片/密钥/初始化段）解析成绝对 URL
        base = urljoin(m3u8_url, ".")

        # 先收集分片，探测第一个分片的真实内容：
        # 图片（漫画/图文番）→ 图像流模式（转 MJPEG/AVI，否则 ffmpeg 播不了）
        raw_segments = [
            urljoin(base, line.strip())
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not raw_segments:
            log.warning("m3u8 无分片可重写")
            return None

        image_mode = False
        hybrid_mode = False
        try:
            probe = await _probe_first_bytes(raw_segments[0], size=64 * 1024)
            kind = _detect_image(probe)
            ts_off = _find_ts_offset(probe)
            if kind and ts_off is not None:
                hybrid_mode = True
                log.info("检测到混合分片（%s 封面 + TS 视频，TS 起点 %d），"
                         "启用封面剥离模式", kind, ts_off)
            elif kind:
                image_mode = True
                log.info("检测到图像流（%s 分片），启用图片→MJPEG/AVI 转换模式", kind)
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
                          mode="hybrid" if hybrid_mode else "image" if image_mode else "video")
        # 用实际端口替换占位符
        proxy._playlist = proxy._playlist.replace("{BASE}", f"http://127.0.0.1:{proxy.port}")
        # 混合流：同步预热前 5 个分片——hls demuxer 打开分片 0 后会立即
        # 预取分片 1（prefetch），冷下载 ~1.5s 超过 mpv 耐心会直接中止播放；
        # 后续分片由 _schedule_warm 边播边预取
        if hybrid_mode:
            warm_count = min(5, len(segments))
            for i in range(warm_count):
                proxy._ts_cache_order.append(i)
                await proxy._warm_hybrid_segment(i)
        log.info("m3u8 重写完成: %d 个分片, %d 个密钥, %d 个初始化段 → %s",
                 len(segments), len(keys), len(maps), proxy.playlist_url)
        return proxy
    except Exception as e:  # noqa: BLE001
        log.warning("HLS 代理初始化失败: %s", e)
        try:
            await proxy.stop()
        except Exception:  # noqa: BLE001
            pass
        return None
