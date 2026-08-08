"""上游 URL 安全校验 —— 防 SSRF。

投屏来的 URL（``SetAVTransportURI``）以及 m3u8 内的分片 / 密钥 / 初始化段 URL，
都来自局域网其他设备，属不可信输入。本模块在代理真正发起抓取前对这些 URL
做白名单 / 黑名单过滤，防止把本机当作跳板去访问其本机服务、内网管理面板、
云元数据服务等（SSRF）。

策略
----
1. **scheme 白名单**：只允许 ``http`` / ``https``（显式拒绝 ``file://`` / ``ftp://`` /
   ``data:`` / ``edl://`` 等，这些会被 mpv / aiohttp 当作本地资源或特殊协议）。
2. **host → IP 解析**：用 ``socket.getaddrinfo`` 解析所有 A / AAAA 记录，只要任一
   地址命中黑名单就拒绝。这样能挡住 ``localhost`` / ``nic.local`` / DNS rebinding
   （域名解析后才指向内网）等绕过手段。
3. **IP 黑名单**（解析后判断）：
   - loopback：``127/8``、``::1``（本机服务，始终挡）
   - link-local：``169.254/16``（含云元数据 ``169.254.169.254``）、``fe80::/10``（始终挡）
   - private：``10/8``、``172.16/12``、``192.168/16``、``fc00::/7``（默认挡，
     ``allow_intranet=True`` 时放行，供 NAS 投屏场景）
   - ``0.0.0.0`` / ``::``（始终挡）
4. DNS 解析失败（域名无法解析）→ 拒绝（避免攻击者用不解析的域名试探）。

注意：这是本地局域网投屏接收软件的纵深防御，不是面向公网的服务。
最薄弱环节仍是「同 Wi-Fi 任意设备都能投屏」（见审查报告 H7），这里堵的是
「借投屏之名让本机访问本机/内网」这一具体 SSRF 路径。
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlsplit

from ..logger import get_logger

log = get_logger("player.url_guard")

# 解析超时：DNS 查询一般 <1s，给 3s 余量；避免恶意/不可达域名拖慢投屏体感
_DNS_TIMEOUT = 3.0


class UrlBlockedError(PermissionError):
    """URL 被安全策略拦截。``reason`` 描述具体原因（用于用户提示）。"""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"{reason}: {url}")


def _resolve_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """解析 host 的所有 IPv4/IPv6 地址。解析失败返回空列表。"""
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        # getaddrinfo 会按系统配置同时查 A/AAAA；AI_NUMERICHOST 不影响（host 是域名）
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return []
    for info in infos:
        sockaddr = info[4]
        try:
            ip_str = sockaddr[0]
            # 去掉 IPv6 zone id（如 fe80::1%eth0）
            if "%" in ip_str:
                ip_str = ip_str.split("%", 1)[0]
            addrs.append(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
    # 去重保序
    seen: set[str] = set()
    unique: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for a in addrs:
        if str(a) not in seen:
            seen.add(str(a))
            unique.append(a)
    return unique


def _is_blocked_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_intranet: bool
) -> Optional[str]:
    """判断单个 IP 是否命中黑名单。命中则返回原因字符串，否则 None。"""
    if ip.is_loopback:
        return "指向回环地址（本机服务）"
    if ip.is_link_local:
        # 169.254.x.x 含云元数据 169.254.169.254；fe80::/10
        return "指向链路本地地址（含云元数据）"
    if ip.is_unspecified:
        # 0.0.0.0 / ::
        return "指向未指定地址"
    if ip.is_multicast:
        return "指向多播地址"
    # 私网段：默认挡，allow_intranet 时放行（NAS 投屏场景）
    if not allow_intranet and ip.is_private:
        return "指向内网私有地址（可在设置中开启「允许内网投屏源」）"
    return None


def is_url_allowed(url: str, *, allow_intranet: bool = False) -> bool:
    """URL 是否允许作为代理上游。不抛异常，命中黑名单返回 False。"""
    try:
        validate_upstream_url(url, allow_intranet=allow_intranet)
    except UrlBlockedError as e:
        log.warning("投屏 URL 被安全策略拦截: %s", e.reason)
        return False
    return True


def validate_upstream_url(
    url: str, *, allow_intranet: bool = False
) -> str:
    """校验上游 URL，合法则原样返回，非法抛 :class:`UrlBlockedError`。

    ``allow_intranet=True`` 时放行私网段（仍挡 loopback/link-local/未指定）。
    """
    if not url or not isinstance(url, str):
        raise UrlBlockedError(str(url), "空或非法的 URL")

    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlBlockedError(url, f"不允许的协议 {scheme!r}（仅支持 http/https）")

    host = urlsplit(url).hostname
    if not host:
        raise UrlBlockedError(url, "URL 缺少主机名")

    # 先处理 host 本身就是字面 IP 的情况（含 IPv6 字面量，getaddrinfo 也能处理，
    # 但直接判可省一次系统调用且避免 zone-id 解析差异）
    try:
        literal = ipaddress.ip_address(host)
        reason = _is_blocked_ip(literal, allow_intranet=allow_intranet)
        if reason:
            raise UrlBlockedError(url, reason)
        return url
    except ValueError:
        pass  # host 是域名，走 DNS 解析

    addrs = _resolve_ips(host)
    if not addrs:
        # DNS 解析失败：拒绝。攻击者可能用不解析的域名试探，或域名暂时失效。
        # 对正常投屏，此刻解析失败也意味着播放会失败，拒绝更安全。
        raise UrlBlockedError(url, f"无法解析主机 {host}")

    for ip in addrs:
        reason = _is_blocked_ip(ip, allow_intranet=allow_intranet)
        if reason:
            raise UrlBlockedError(url, f"{reason}（{host} → {ip}）")
    return url
