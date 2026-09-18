"""线程化的 SSDP 设备发现监听器（原生阻塞 socket，不依赖 asyncio）。

为什么独立实现而不复用 async_upnp_client.server 的 SSDP？
--------------------------------------------------------
async_upnp_client.server 把 SSDP 收包塞进 asyncio 的事件循环（在 qasync 下是
Windows 的 ProactorEventLoop / IOCP）。Windows 的 IOCP 对 **多播 UDP 接收**
存在长期缺陷——socket 即使建好、IP_ADD_MEMBERSHIP 加了，也收不到手机发的
多播 M-SEARCH，导致手机搜不到设备。

本实现照搬 macast（https://github.com/xfangfang/Macast）验证过的模式：
独立线程 + 原生阻塞 socket + recvfrom 轮询，彻底绕开 IOCP，在 Windows 上稳定。

关键点（Windows）：
1. 只设 SO_REUSEADDR，**绝不设 SO_REUSEPORT**（Windows 没有真正的 SO_REUSEPORT
   语义，设了会导致 bind 抢包异常）。
2. 对本机每块网卡 IP 都 IP_ADD_MEMBERSHIP（多网卡机器上只 join 一次默认接口，
   从 WiFi 进来的包可能到不了）。
3. bind 0.0.0.0:1900，settimeout(1) 轮询 recvfrom。
4. 收到 M-SEARCH 后，用**同一个接收 socket 单播回复** HTTP/1.1 200 OK 到
   recvfrom 拿到的源地址（手机的 IP+端口）。
5. LOCATION 里的 IP 选「和手机源 IP 同子网」的网卡 IP。
6. 周期性多播 NOTIFY * ssdp:alive 加速并维持发现。

device/HTTP/SOAP/GENA 部分仍由 async_upnp_client 提供（那部分是 aiohttp TCP，
可靠）。本模块只负责 SSDP 这一层的「被发现」。
"""
from __future__ import annotations

import random
import socket
import sys
import threading
from email.utils import formatdate
from typing import Callable

from ..logger import get_logger
from ._net import list_local_ips, same_subnet

log = get_logger("dlna.ssdp")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_GROUP = (SSDP_ADDR, SSDP_PORT)
SSDP_START_TIMEOUT = 2.0
SSDP_STOP_TIMEOUT = 2.0


def _parse_headers(data: bytes) -> tuple[str, dict[str, str]]:
    """解析 SSDP 报文，返回 (首行命令, header 字典，key 小写)。"""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return "", {}
    # 头部与（可能的）body 用空行分隔
    head = text.split("\r\n\r\n", 1)[0]
    lines = head.split("\r\n")
    if not lines:
        return "", {}
    cmd = lines[0].strip()
    headers: dict[str, str] = {}
    for ln in lines[1:]:
        # 形如 "ST: urn:..."，冒号后可能有空格
        if ":" in ln:
            k, _, v = ln.partition(":")
            headers[k.strip().lower()] = v.strip()
    return cmd, headers


def filter_interfaces(
    ips: list[tuple[str, str]],
    allowed_ips: list[str] | None,
) -> list[tuple[str, str]]:
    """按 allowed_ips 白名单过滤网卡；白名单里枚举不到的 IP 补默认掩码。

    仅默认网卡模式下，多播 membership / sender / M-SEARCH 选卡都只在白名单内
    进行；白名单中的 IP 若被网卡枚举漏掉（如 Windows ICS 兜底段），仍补上，
    保证设备至少能在指定网卡上宣告。``allowed_ips=None`` 表示不过滤（默认）。
    """
    if allowed_ips is None:
        return ips
    allowed = set(allowed_ips)
    filtered = [(ip, mask) for ip, mask in ips if ip in allowed]
    present = {ip for ip, _ in filtered}
    for ip in sorted(allowed - present):
        filtered.append((ip, "255.255.255.0"))
    return filtered


class _MulticastSender:
    """绑定到特定网卡的发送 socket，用于主动多播 NOTIFY alive/byebye。

    通过 IP_MULTICAST_IF 指定出接口，避免多网卡下 NOTIFY 发到错误接口。
    """

    def __init__(self, ip: str) -> None:
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        except OSError as e:
            log.debug("sender 设 IP_MULTICAST_IF=%s 失败: %s", ip, e)
        try:
            self.sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(SSDP_ADDR) + socket.inet_aton(ip),
            )
        except OSError:
            pass
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        except OSError:
            pass

    def send(self, data: bytes) -> None:
        try:
            self.sock.sendto(data, SSDP_GROUP)
        except OSError as e:
            log.debug("sender %s 发送失败: %s", self.ip, e)

    def close(self) -> None:
        try:
            self.sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP,
                socket.inet_aton(SSDP_ADDR) + socket.inet_aton(self.ip),
            )
        except OSError:
            pass
        self.sock.close()


class SsdpListener(threading.Thread):
    """SSDP 设备发现监听线程。

    用法::

        listener = SsdpListener(udn, location_port, location_path, server_id)
        listener.start()
        ...
        listener.stop()
    """

    def __init__(
        self,
        udn: str,
        http_port: int,
        server_id: str,
        location_path: str = "/device.xml",
        announce_interval: float = 30.0,
        allowed_ips: list[str] | None = None,
    ) -> None:
        super().__init__(name="SsdpListener", daemon=True)
        # udn 形如 "uuid:xxxxxxxx-..."；USN 根用去掉 "uuid:" 前缀的部分
        self._udn = udn
        self._uuid = udn[5:] if udn.startswith("uuid:") else udn
        self._http_port = http_port
        self._server_id = server_id
        self._location_path = location_path
        self._announce_interval = announce_interval
        # 网卡白名单（仅默认网卡模式）；None 表示所有网卡都宣告
        self._allowed_ips = list(allowed_ips) if allowed_ips is not None else None

        self._sock: socket.socket | None = None
        self._senders: list[_MulticastSender] = []
        self._ips: list[tuple[str, str]] = []
        self._resources_lock = threading.RLock()
        self._running = threading.Event()
        self._running.set()  # 初始化为运行状态；stop() 时 clear
        self._startup_done = threading.Event()
        self._startup_error: Exception | None = None

        # 本设备响应的 ST 列表（macast 的 6 条；M-SEARCH 命中任一即回复）
        self._st_list = [
            "upnp:rootdevice",
            f"uuid:{self._uuid}",
            "urn:schemas-upnp-org:device:MediaRenderer:1",
            "urn:schemas-upnp-org:service:AVTransport:1",
            "urn:schemas-upnp-org:service:RenderingControl:1",
            "urn:schemas-upnp-org:service:ConnectionManager:1",
        ]

    # ------------------------------------------------------------------ #
    # 线程主循环
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            self._setup_socket()
        except Exception as e:  # noqa: BLE001
            self._startup_error = e
            self._startup_done.set()
            log.error("SSDP 监听 socket 建立失败，设备将无法被发现: %s", e)
            self._cleanup()
            return

        self._startup_done.set()
        log.info(
            "SsdpListener 已启动，监听 0.0.0.0:%d，多播组加入网卡: %s",
            SSDP_PORT, [ip for ip, _ in self._ips],
        )

        try:
            # 启动时立即广播一次 alive，加速被发现
            self._notify_alive()

            last_announce = 0.0
            while self._running.is_set():
                # 周期性广播 alive
                import time
                now = time.time()
                if now - last_announce >= self._announce_interval:
                    self._notify_alive()
                    last_announce = now

                try:
                    assert self._sock is not None
                    data, addr = self._sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError as e:
                    if self._running.is_set():
                        log.warning("SSDP recvfrom 异常: %s", e)
                    break
                self._handle_packet(data, addr)
        finally:
            self._cleanup()

    def _setup_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        with self._resources_lock:
            # 先保存引用；后续任一步失败时，run() 的 finally/异常分支都能关闭它。
            self._sock = sock
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)

            # ★ Windows 关键：只设 SO_REUSEADDR，绝不设 SO_REUSEPORT
            if sys.platform.startswith("win32"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            elif sys.platform == "darwin":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            elif hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # 对每块网卡都加入多播组 + 建 sender（仅默认网卡模式下按白名单过滤；
            # 后续 M-SEARCH 选卡与 fallback 都只在这个列表内进行）
            self._ips = filter_interfaces(list_local_ips(), self._allowed_ips)
            for ip, _mask in self._ips:
                mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(ip)
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                    self._senders.append(_MulticastSender(ip))
                    log.debug("已加入多播组，接口 %s", ip)
                except OSError as e:
                    log.debug("接口 %s 加入多播组失败: %s", ip, e)

            sock.bind(("0.0.0.0", SSDP_PORT))
            sock.settimeout(1.0)

    # ------------------------------------------------------------------ #
    # 收包处理
    # ------------------------------------------------------------------ #
    def _handle_packet(self, data: bytes, addr: tuple) -> None:
        cmd, headers = _parse_headers(data)
        if not cmd:
            return
        first = cmd.split(" ", 1)[0]
        if first == "M-SEARCH":
            self._reply_msearch(headers, addr, data)
        # NOTIFY 是别的设备发的 alive/byebye，我们作为 renderer 不处理

    def _reply_msearch(self, headers: dict[str, str], addr: tuple, raw: bytes) -> None:
        host, port = addr
        st_req = headers.get("st", "")
        if not st_req:
            return
        # 仅默认网卡模式下，直接忽略白名单子网之外的 M-SEARCH：
        # 不回复、不暴露设备存在（LOCATION 即便发出去也不可达）。
        if self._allowed_ips is not None and not any(
            same_subnet(ip, host, mask) for ip, mask in self._ips
        ):
            log.debug("忽略白名单子网外的 M-SEARCH from %s:%s, ST=%s", host, port, st_req)
            return
        log.info("收到 M-SEARCH from %s:%s, ST=%s", host, port, st_req)

        # 选和请求方同子网的网卡 IP 填 LOCATION
        chosen_ip = self._choose_iface_for(host)

        for st in self._st_list:
            if st == st_req or st_req == "ssdp:all":
                packet = self._build_search_response(st, chosen_ip)
                try:
                    assert self._sock is not None
                    self._sock.sendto(packet, (host, port))
                    log.info("回复 M-SEARCH → %s:%s, LOCATION=http://%s:%d%s",
                             host, port, chosen_ip, self._http_port, self._location_path)
                except OSError as e:
                    log.warning("回复 M-SEARCH 失败: %s", e)
                # ssdp:all 时每个 ST 都回；命中具体 ST 时回一次即可
                if st == st_req:
                    return

    def _choose_iface_for(self, remote_ip: str) -> str:
        """选一个和 remote_ip 同子网的网卡 IP；找不到就用第一个。"""
        for ip, mask in self._ips:
            if same_subnet(ip, remote_ip, mask):
                return ip
        return self._ips[0][0] if self._ips else "127.0.0.1"

    def _build_search_response(self, st: str, ip: str) -> bytes:
        """构造 M-SEARCH 的单播回复（HTTP/1.1 200 OK）。"""
        usn = f"uuid:{self._uuid}::{st}" if st.startswith("urn:") else (
            f"uuid:{self._uuid}::{st}" if st == "upnp:rootdevice" else f"uuid:{self._uuid}"
        )
        location = f"http://{ip}:{self._http_port}{self._location_path}"
        lines = [
            "HTTP/1.1 200 OK",
            f"CACHE-CONTROL: max-age=1900",
            f"DATE: {formatdate(usegmt=True)}",
            "EXT:",
            f"LOCATION: {location}",
            f"SERVER: {self._server_id}",
            f"ST: {st}",
            f"USN: {usn}",
            "",
            "",
        ]
        return "\r\n".join(lines).encode("utf-8")

    # ------------------------------------------------------------------ #
    # 主动广播 NOTIFY alive / byebye
    # ------------------------------------------------------------------ #
    def _notify_alive(self) -> None:
        with self._resources_lock:
            # stop() 可能在本线程通过 while 条件后先获得锁、发送 byebye
            # 并 clear。取得锁后必须复查，禁止 byebye 之后再次广播 alive。
            if not self._running.is_set():
                return
            for st in self._st_list:
                packet = self._build_notify(st, alive=True)
                for sender in self._senders:
                    # LOCATION 里的 IP 用 sender 绑定的网卡 IP
                    p = packet.replace(b"{{IP}}", sender.ip.encode())
                    sender.send(p)
            sender_count = len(self._senders)
        log.debug("已多播 NOTIFY alive（%d 个 ST × %d 个网卡）",
                  len(self._st_list), sender_count)

    def _build_notify(self, st: str, alive: bool) -> bytes:
        usn = f"uuid:{self._uuid}::{st}" if st.startswith("urn:") or st == "upnp:rootdevice" else f"uuid:{self._uuid}"
        # {{IP}} 占位符在发送时按各 sender 的网卡 IP 替换
        location = f"http://{{IP}}:{self._http_port}{self._location_path}"
        lines = [
            "NOTIFY * HTTP/1.1",
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
            "LOCATION: " + location,
            f"SERVER: {self._server_id}",
            f"NT: {st}",
            "NTS: ssdp:alive" if alive else "NTS: ssdp:byebye",
            f"USN: {usn}",
            "CACHE-CONTROL: max-age=1900",
            "",
            "",
        ]
        return "\r\n".join(lines).encode("utf-8")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def wait_until_ready(self, timeout: float = SSDP_START_TIMEOUT) -> None:
        """等待 socket 初始化完成，并把监听线程内的启动失败同步给调用方。"""
        if not self._startup_done.wait(timeout):
            raise TimeoutError(f"SsdpListener 启动超过 {timeout:g} 秒仍未就绪")
        if self._startup_error is not None:
            raise RuntimeError("SSDP listener 启动失败") from self._startup_error
        if not self.is_alive():
            raise RuntimeError("SsdpListener 启动后意外退出")

    def stop(self, timeout: float = SSDP_STOP_TIMEOUT) -> None:
        log.info("停止 SsdpListener")
        # sender 的 byebye 发送和监听线程的 _cleanup 共用同一把锁，保证
        # socket 不会在 sendto 过程中被另一线程关闭。
        with self._resources_lock:
            for st in self._st_list:
                packet = self._build_notify(st, alive=False)
                for sender in self._senders:
                    sender.send(packet.replace(b"{{IP}}", sender.ip.encode()))
            self._running.clear()

        # 发个空包唤醒阻塞的 recvfrom（虽然 settimeout(1) 会自己醒，保险起见）
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wake:
                wake.sendto(b"\r\n", ("127.0.0.1", SSDP_PORT))
        except OSError:
            pass

        if self.ident is not None and threading.current_thread() is not self:
            self.join(timeout)
            if self.is_alive():
                raise TimeoutError(f"SsdpListener 停止超过 {timeout:g} 秒仍未退出")

    def _cleanup(self) -> None:
        with self._resources_lock:
            for ip, _ in self._ips:
                mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(ip)
                try:
                    if self._sock is not None:
                        self._sock.setsockopt(
                            socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq,
                        )
                except OSError:
                    pass
            for sender in self._senders:
                try:
                    sender.close()
                except OSError:
                    pass
            self._senders.clear()
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
        log.debug("SsdpListener 已清理 socket")
