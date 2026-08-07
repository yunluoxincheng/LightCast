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

方案（直接接收播放，无文件、无额外步骤）
--------------------------------------
1. 收到 m3u8 URL → 下载 m3u8 文本
2. 启动本地代理（aiohttp，内存态），提供端点：
   - ``/playlist.m3u8``：返回重写后的 m3u8
   - ``/seg/{n}.mp4``：转发真实分片（保留 Range/206 语义 + 防盗链 header）
   - ``/key/{n}.key``：转发 AES-128 密钥（内存缓存，密钥很小）
   - ``/map/{n}.mp4``：转发 fMP4 初始化段
   所有 URI（分片/密钥/初始化段）都按 m3u8 的基地址解析成绝对 URL 后转发
3. mpv 只播放一个 URL：``http://127.0.0.1:{port}/playlist.m3u8`` —— 走标准 HLS 路径
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urljoin

import aiohttp
from aiohttp import web

from ..logger import get_logger

log = get_logger("player.hls")

# #EXT-X-KEY / #EXT-X-MAP 里的 URI="..." 属性
_ATTR_URI_RE = re.compile(r'URI="([^"]*)"')


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
    ) -> None:
        """启动代理。

        segments/keys/maps 是解析好的绝对 URL；playlist 是重写后的 m3u8
        文本（其中 ``{BASE}`` 占位符稍后替换为实际端口）。
        """
        if self._site is not None:
            await self.stop()
        self._segments = segments
        self._keys = list(keys or [])
        self._maps = list(maps or [])
        self._key_cache = {}
        self._playlist = playlist
        self._session = aiohttp.ClientSession()

        app = web.Application()
        app.router.add_get("/playlist.m3u8", self._handle_playlist)
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

        segments: list[str] = []
        keys: list[str] = []
        maps: list[str] = []
        rewritten: list[str] = []
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
                rewritten.append(f"{{BASE}}/seg/{len(segments) - 1}.mp4")

        if not segments:
            log.warning("m3u8 无分片可重写")
            return None

        await proxy.start(segments, "\n".join(rewritten), keys, maps)
        # 用实际端口替换占位符
        proxy._playlist = proxy._playlist.replace("{BASE}", f"http://127.0.0.1:{proxy.port}")
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
