"""上游 URL 安全校验 —— 防 SSRF。

投屏来的 URL（``SetAVTransportURI``）以及 m3u8 内的分片 / 密钥 / 初始化段 URL，
都来自局域网其他设备，属不可信输入。本模块在代理真正发起抓取前对这些 URL
做白名单 / 黑名单过滤，防止把本机当作跳板去访问其本机服务、云元数据服务等（SSRF）。

策略（三道关，纵深防御）
------------------------
1. **入口 / 重定向每跳的 URL 校验**：:func:`validate_upstream_url` 做 scheme 白名单
   （仅 http/https）+ host 字面量 IP 检查。用于 ``on_set_uri`` 入口和重定向每一跳。
2. **连接器层 IP 校验（解 DNS rebinding / TOCTOU）**：:class:`SSRFSafeConnector` 在
   aiohttp 的连接解析阶段过滤实际候选 IP，再由同一次连接流程使用过滤后的结果建立
   TCP 连接。这样「校验」与「连接」用的是**同一个解析结果**，攻击者无法用
   「第一次解析→公网，第二次解析→内网」的 rebinding 绕过。
3. **重定向每跳校验（解 302 绕过）**：所有上游 GET 关闭自动重定向
   （``allow_redirects=False``），由 :func:`safe_get` 手动跟进，每一跳的 Location
   都过 :func:`validate_upstream_url`，目标非法即拒绝。

IP 黑名单
---------
始终挡（与 ``allow_intranet`` 无关）：
- loopback：``127/8``、``::1``（本机服务）
- link-local：``169.254/16``（含云元数据 ``169.254.169.254``）、``fe80::/10``
- 未指定：``0.0.0.0`` / ``::``
- 多播
- 其他非公网保留地址（CGNAT / benchmark / 文档段等）

私网段（``10/8``、``172.16/12``、``192.168/16``、``fc00::/7``）默认**放行**——
DLNA 投屏本就是同局域网场景（VLC / 相册常开 ``http://192.168.x.x`` 本地 server），
默认挡会破坏正常功能。``allow_intranet=False`` 时可收紧（挡私网）。

注意：这是本地局域网投屏接收软件的纵深防御，不是面向公网的服务。
"""
from __future__ import annotations

import ipaddress
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import ClientResponse, ClientSession, ClientTimeout, TCPConnector

from ..logger import get_logger

log = get_logger("player.url_guard")

# 重定向跟进上限（防无限循环 / 重定向炸弹）
_MAX_REDIRECTS = 5

_INTRANET_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_INTRANET_V6 = (ipaddress.ip_network("fc00::/7"),)


class UrlBlockedError(PermissionError):
    """URL 被安全策略拦截。``reason`` 描述具体原因（用于用户提示）。"""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"{reason}: {url}")


class SSRFBlockedError(aiohttp.ClientError):
    """连接器在建立 TCP 连接前因 SSRF 策略拒绝。

    :func:`safe_get` 会把它转换为 :class:`UrlBlockedError`，确保调用方不会
    把安全拒绝降级成普通网络失败。
    """

    def __init__(self, host: str, reason: str) -> None:
        super().__init__(f"{reason}: {host}")
        self.host = host
        self.reason = reason


def _is_blocked_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_intranet: bool
) -> Optional[str]:
    """判断单个 IP 是否命中黑名单。命中则返回原因字符串，否则 None。

    loopback / link-local / 未指定 / 多播 **始终挡**（与本机服务/云元数据相关，
    不是合法的投屏上游）；私网段按 ``allow_intranet`` 决定。
    """
    # IPv4-mapped IPv6 必须按其内嵌 IPv4 判断；例如 ::ffff:127.0.0.1
    # 在部分 Python 版本里不直接报告 is_loopback。
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if ip.is_loopback:
        return "指向回环地址（本机服务）"
    if ip.is_link_local:
        # 169.254.x.x 含云元数据 169.254.169.254；fe80::/10
        return "指向链路本地地址（含云元数据）"
    if ip.is_unspecified:
        return "指向未指定地址"
    if ip.is_multicast:
        return "指向多播地址"
    intranet_networks = _INTRANET_V4 if ip.version == 4 else _INTRANET_V6
    is_intranet = any(ip in network for network in intranet_networks)
    if is_intranet:
        if allow_intranet:
            return None
        return "指向内网私有地址（当前配置禁止内网投屏源）"
    # 除明确允许的 RFC1918 / ULA 外，其余非公网地址（CGNAT、文档段、
    # benchmark、保留段等）没有合法投屏用途，保守拒绝。
    if not ip.is_global:
        return "指向非公网保留地址"
    return None


def validate_upstream_url(url: str, *, allow_intranet: bool = True) -> str:
    """入口 / 重定向每跳的 URL 校验：scheme 白名单 + host 字面量 IP 检查。

    合法则原样返回，非法抛 :class:`UrlBlockedError`。

    只做**字面量**层面的快速校验（URL 本身写的 IP）。真正的 DNS 解析后 IP 校验
    由 :class:`SSRFSafeConnector` 在连接时执行，避免 TOCTOU。

    ``allow_intranet`` 默认 True（与全局配置默认值一致）：放行私网 host。
    """
    if not url or not isinstance(url, str):
        raise UrlBlockedError(str(url), "空或非法的 URL")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UrlBlockedError(url, f"URL 格式非法（{exc}）") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlBlockedError(url, f"不允许的协议 {scheme!r}（仅支持 http/https）")

    try:
        host = parts.hostname
    except ValueError as exc:
        raise UrlBlockedError(url, f"主机名格式非法（{exc}）") from exc
    if not host:
        raise UrlBlockedError(url, "URL 缺少主机名")
    try:
        parts.port
    except ValueError as exc:
        raise UrlBlockedError(url, f"端口格式非法（{exc}）") from exc

    # host 本身是字面 IP 时直接判（含 IPv6 字面量）
    try:
        literal = ipaddress.ip_address(host)
        reason = _is_blocked_ip(literal, allow_intranet=allow_intranet)
        if reason:
            raise UrlBlockedError(url, reason)
    except ValueError:
        pass  # host 是域名，留给 connector 在连接时校验解析结果
    return url


def _filter_resolved_hosts(
    host: str,
    hosts: list[dict[str, Any]],
    *,
    allow_intranet: bool,
) -> list[dict[str, Any]]:
    """过滤 aiohttp 已解析出的候选 IP；全部被拒时抛安全异常。"""
    kept: list[dict[str, Any]] = []
    blocked: list[str] = []
    for item in hosts:
        ip_text = str(item.get("host", ""))
        # IPv6 link-local 可能带 zone id（如 fe80::1%12）。
        plain_ip = ip_text.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(plain_ip)
        except ValueError:
            # 系统 resolver 的契约是返回数字 IP。出现非 IP 结果时保守拒绝，
            # 避免第三方 resolver 用未校验的主机名绕过连接层策略。
            raise SSRFBlockedError(host, f"DNS 解析器返回了非法地址 {ip_text!r}") from None
        reason = _is_blocked_ip(ip, allow_intranet=allow_intranet)
        if reason:
            blocked.append(f"{ip}（{reason}）")
            log.warning("SSRF 连接器拒绝目标 %s → %s（%s）", host, ip, reason)
            continue
        kept.append(item)

    if not hosts:
        raise SSRFBlockedError(host, "DNS 未返回任何候选地址")
    if not kept:
        detail = "、".join(blocked)
        raise SSRFBlockedError(host, f"所有候选地址被 SSRF 策略拦截：{detail}")
    return kept


class SSRFSafeConnector(TCPConnector):
    """在连接解析阶段过滤实际候选 IP 的 aiohttp 连接器。

    ``TCPConnector._create_direct_connection`` 只消费本方法返回的解析结果，因此
    校验与建连使用同一批 IP；这里不修改实例方法或共享状态，并发请求互不干扰。
    """

    def __init__(self, *, allow_intranet: bool = True, **kwargs):  # noqa: ANN003
        super().__init__(**kwargs)
        self._ssrf_allow_intranet = allow_intranet

    async def _resolve_host(self, host, port, traces=None):  # type: ignore[override]  # noqa: ANN001
        resolved = await super()._resolve_host(host, port, traces=traces)
        return _filter_resolved_hosts(
            str(host), resolved, allow_intranet=self._ssrf_allow_intranet
        )


def make_session(
    *, allow_intranet: bool = True, **kwargs  # noqa: ANN003
) -> ClientSession:
    """创建带 SSRF 防护连接器的 aiohttp 会话。"""
    connector = SSRFSafeConnector(
        allow_intranet=allow_intranet,
        limit=64,
        limit_per_host=16,
    )
    return ClientSession(connector=connector, **kwargs)


def _strip_sensitive_redirect_headers(headers: dict) -> None:
    """跨 origin 重定向时移除可能泄露凭据的显式请求头。"""
    sensitive = {"authorization", "proxy-authorization", "cookie"}
    for name in list(headers):
        if str(name).lower() in sensitive:
            headers.pop(name, None)


def _url_origin(url: str) -> tuple[str, str, Optional[int]]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


async def safe_get(
    session: ClientSession,
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: Optional[ClientTimeout] = None,
    max_redirects: int = _MAX_REDIRECTS,
    allow_intranet: bool = True,
) -> ClientResponse:
    """安全 GET：关闭自动重定向，每一跳过 URL 校验后手动跟进。

    - 入口 url 与每一跳 Location 都过 :func:`validate_upstream_url`
      （IP 由会话的 :class:`SSRFSafeConnector` 在连接时兜底）
    - 上限 ``max_redirects`` 跳，超限抛 ``RuntimeError``
    - 3xx 但无 Location / 非法 Location → 当作错误
    - 返回**最终的非 3xx 响应**（连接已建立，调用方负责 close）

    注意：调用方拿到响应后需自行处理状态码与读取。
    """
    current_url = url
    request_headers = dict(headers) if headers is not None else None
    validate_upstream_url(current_url, allow_intranet=allow_intranet)
    hops = 0
    while True:
        try:
            resp = await session.get(
                current_url,
                headers=request_headers,
                timeout=timeout,
                allow_redirects=False,
            )
        except SSRFBlockedError as exc:
            # 上层必须能区分安全拒绝与普通网络错误；前者绝不能触发 mpv fallback。
            raise UrlBlockedError(current_url, exc.reason) from exc
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location") or resp.headers.get("location")
            resp.close()
            hops += 1
            if hops > max_redirects:
                raise RuntimeError(f"重定向次数超过上限 {max_redirects}")
            if not location:
                raise RuntimeError(f"重定向响应缺少 Location: {current_url}")
            # 相对 Location 按当前 url 解析成绝对
            next_url = urljoin(current_url, location)
            validate_upstream_url(next_url, allow_intranet=allow_intranet)
            if request_headers is not None and _url_origin(next_url) != _url_origin(current_url):
                _strip_sensitive_redirect_headers(request_headers)
            current_url = next_url
            continue
        return resp
