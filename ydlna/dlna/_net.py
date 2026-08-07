"""本机网卡枚举与子网判断工具。

用于 SSDP 多播：需要对本机每块网卡都加入多播组（IP_ADD_MEMBERSHIP），
否则多网卡 Windows 机器上从 WiFi 进来的多播包可能到不了监听 socket。
参考 macast 的 utils.Setting.get_ip 实现。
"""
from __future__ import annotations

import socket
import sys

from ..logger import get_logger

log = get_logger("dlna.net")


def _octets(ip: str) -> list[int]:
    return [int(x) for x in ip.split(".")]


def same_subnet(ip1: str, ip2: str, mask: str) -> bool:
    """判断两个 IPv4 是否在同一子网（按 mask）。"""
    try:
        a, b, m = _octets(ip1), _octets(ip2), _octets(mask)
        return all((a[i] & m[i]) == (b[i] & m[i]) for i in range(4))
    except (ValueError, IndexError):
        return False


def list_local_ips() -> list[tuple[str, str]]:
    """枚举本机所有 (ip, netmask)，供 SSDP 多播 membership 使用。

    优先用 ifaddr（纯 Python，无需 C 编译）；不可用时退化为 getaddrinfo。
    过滤回环和链路本地（169.254.x.x）。Windows 额外补 ICS 共享网卡 192.168.137.1。
    """
    ips: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(ip: str, mask: str = "255.255.255.0") -> None:
        if ip.startswith("127.") or ip.startswith("169.254.") or ip in seen:
            return
        seen.add(ip)
        ips.append((ip, mask))

    try:
        import ifaddr  # type: ignore
        for adapter in ifaddr.get_adapters():
            for item in adapter.ips:
                if not item.is_IPv4:
                    continue
                ip = item.ip
                # network_prefix 是位数（如 24），转成掩码字符串
                prefix = item.network_prefix
                if 0 <= prefix <= 32:
                    mask_bits = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
                    mask = ".".join(str((mask_bits >> (8 * (3 - i))) & 0xFF) for i in range(4))
                else:
                    mask = "255.255.255.0"
                _add(ip, mask)
    except ImportError:
        log.warning("ifaddr 未安装，网卡枚举退化为 getaddrinfo（多网卡支持受限）")
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                _add(info[4][0])
        except OSError:
            pass

    # Windows ICS 共享网卡兜底（netifaces/ifaddr 容易漏）
    if sys.platform.startswith("win32"):
        _add("192.168.137.1", "255.255.255.0")

    log.debug("本机网卡 IP: %s", ips)
    return ips


def get_local_ip() -> str:
    """获取本机主局域网 IP（用于显示和 base_uri）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"
