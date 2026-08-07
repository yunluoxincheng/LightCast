"""HLS 流本地代理 —— 解决 ffmpeg 对 .jpg 分片误判为图片的问题。

背景
----
某些 DLNA 投屏源（如部分看番软件）的 m3u8 分片 URL 用 ``.jpg`` 扩展名伪装，
但实际内容是 mp4 容器包裹的 h264 视频。ffmpeg 的 HLS demuxer 探测分片时，
``.jpg`` 扩展名让 image2 demuxer 得分碾压 mp4 → 把视频误判成图片 → 解码失败
（症状：媒体已装载但黑屏、"Invalid data found when processing input"）。

已验证：分片扩展名改成 ``.mp4`` 后 mpv 能正确识别 h264 1080p 并正常播放。

方案（直接接收播放，无文件、无额外步骤）
--------------------------------------
1. 收到 m3u8 URL → 下载 m3u8 文本
2. 启动本地代理（aiohttp，内存态），提供两个端点：
   - ``/playlist.m3u8``：返回重写后的 m3u8（分片 URL 指向本代理 + .mp4 扩展名）
   - ``/seg/{n}.mp4``：转发到真实分片（保留 Range/206 语义 + 防盗链 header）
3. mpv 只播放一个 URL：``http://127.0.0.1:{port}/playlist.m3u8`` —— 走标准 HLS 路径
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

import aiohttp
from aiohttp import web

from ..logger import get_logger

log = get_logger("player.hls")

_URL_RE = re.compile(r"(https?://\S+)")


class HlsProxy:
    """m3u8 本地代理：提供重写后的播放列表 + 分片转发。"""

    def __init__(self) -> None:
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._port: int = 0
        self._segments: list[str] = []
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

    async def start(self, segments: list[str], playlist: str) -> None:
        """启动代理。segments 是真实分片 URL，playlist 是重写后的 m3u8 文本。"""
        if self._site is not None:
            await self.stop()
        self._segments = segments
        self._playlist = playlist
        self._session = aiohttp.ClientSession()

        app = web.Application()
        app.router.add_get("/playlist.m3u8", self._handle_playlist)
        app.router.add_get("/seg/{index}.mp4", self._handle_segment)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)  # 端口 0 = 自动分配
        await self._site.start()
        sockets = self._site._server.sockets  # noqa: SLF001
        if sockets:
            self._port = sockets[0].getsockname()[1]
        log.info("HLS 代理已启动: %s，%d 个分片", self.playlist_url, len(segments))

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
    async def _handle_playlist(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=self._playlist,
            content_type="application/vnd.apple.mpegurl",
        )

    async def _handle_segment(self, request: web.Request) -> web.StreamResponse:
        index = int(request.match_info["index"])
        if index >= len(self._segments):
            return web.Response(status=404, text="segment not found")
        real_url = self._segments[index]
        assert self._session is not None
        log.debug("代理分片 %d → %s", index, real_url)

        try:
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ("host", "connection", "accept-encoding")}
            resp = await self._session.get(real_url, headers=headers)
        except Exception as e:  # noqa: BLE001
            log.warning("代理分片 %s 失败: %s", real_url, e)
            return web.Response(status=502, text="proxy error")

        if resp.status not in (200, 206):
            log.warning("分片 %d 上游返回 HTTP %s", index, resp.status)
            return web.Response(status=resp.status, text="upstream error")

        # 流式转发：保留上游状态码 + Content-Range / Content-Length
        # （mpv 用 Range 分段拉取，上游返回 206；转成 200 会破坏分段语义）
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
                    log.debug("分片 %d 客户端提前断开（正常）", index)
                    break
        except (ConnectionResetError, asyncio.CancelledError, ConnectionError):
            log.debug("分片 %d 传输中断", index)
        finally:
            resp.close()
        try:
            await stream.write_eof()
        except (ConnectionResetError, ConnectionError, OSError):
            pass
        return stream


# --------------------------------------------------------------------------- #
# m3u8 下载 + 重写
# --------------------------------------------------------------------------- #
async def setup_hls_proxy(m3u8_url: str) -> Optional[HlsProxy]:
    """下载 m3u8、启动本地代理、重写分片 URL。

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

        # 解析分片 URL
        segments: list[str] = []
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and _URL_RE.match(s):
                segments.append(s)

        if not segments:
            log.warning("m3u8 无分片可重写（可能是主播放列表）")
            return None

        # 重写：分片 URL → http://127.0.0.1:port/seg/{n}.mp4
        lines = text.splitlines()
        seg_index = 0
        rewritten: list[str] = []
        for line in lines:
            s = line.strip()
            if s and not s.startswith("#") and _URL_RE.match(s):
                rewritten.append(f"{{BASE}}/seg/{seg_index}.mp4")
                seg_index += 1
            else:
                rewritten.append(line)

        await proxy.start(segments, "\n".join(rewritten))
        # 用实际端口替换占位符
        proxy._playlist = proxy._playlist.replace("{BASE}", f"http://127.0.0.1:{proxy.port}")
        log.info("m3u8 重写完成: %d 个分片 → %s", seg_index, proxy.playlist_url)
        return proxy
    except Exception as e:  # noqa: BLE001
        log.warning("HLS 代理初始化失败: %s", e)
        try:
            await proxy.stop()
        except Exception:  # noqa: BLE001
            pass
        return None
