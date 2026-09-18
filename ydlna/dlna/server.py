"""DlnaServer —— 封装 UpnpServer（HTTP/SOAP/GENA）+ 自研 SsdpListener（设备发现）。

架构
----
- **HTTP/SOAP/GENA**：由 ``async_upnp_client.server.UpnpServer`` 提供（aiohttp TCP，
  在 qasync 下可靠）。包括 device.xml / scpd.xml / SOAP action / GENA 订阅事件。
- **SSDP 设备发现**：由 ``SsdpListener``（独立线程 + 原生阻塞 socket）提供，
  **不**用 UpnpServer 自带的 SSDP。原因：UpnpServer 的 SSDP 跑在 asyncio 事件循环
  里，在 Windows 的 ProactorEventLoop(IOCP) 下收不到多播 UDP，手机搜不到设备。

为了让 UpnpServer 只启 HTTP 不启它的 SSDP，这里 monkey-patch
``UpnpServer.async_start``，跳过 ``_async_start_ssdp``。

同时修正 UpnpServer 的 ``base_uri``：它默认用 ``self.source[0]``（我们传 0.0.0.0）
拼 URL，会导致 device.xml / LOCATION 里的地址是 ``http://0.0.0.0:port``，手机连不上。
这里把 base_uri 的 host 改成真实局域网 IP。
"""
from __future__ import annotations

import asyncio
import platform
import sys
from typing import Optional

from async_upnp_client.server import UpnpServer
import async_upnp_client.server as _upnp_server_module

from ..config import Config
from ..constants import APP_NAME, APP_VERSION, DEFAULT_SSDP_PORT
from ..logger import get_logger
from ._control_context import reset_controller_ip, set_controller_ip
from ._net import get_local_ip
from .avtransport import AVTransportService
from .connection_manager import ConnectionManagerService
from .device import make_device_class
from .rendering_control import RenderingControlService
from .renderer_bridge import RendererBridge
from .ssdp_listener import SsdpListener

log = get_logger("dlna.server")

# 标记是否已经 patch 过 UpnpServer（全局只 patch 一次）
_UPNP_PATCHED = False


def _patch_upnp_server() -> None:
    """对 async_upnp_client 库做两处全局补丁（只打一次）：

    1. ``UpnpServer.async_start`` 只启动 HTTP server，不启动它自带的 SSDP。
       SSDP 由我们的 SsdpListener 线程接管（在 Windows 的 IOCP 下更可靠）。
    2. 包装模块级 ``action_handler``：SOAOP 控制请求入口用 ``request.remote``
       把真实控制点 IP 登记到 ContextVar，供 Bridge 做「控制点授权」
       （H7：Play/Pause/Stop/Seek/音量等状态变更 action 需要区分来源）。
       aiohttp 每个请求是独立任务，ContextVar 按任务复制，天然隔离并发。
       注意：`_async_start_http_server` 里 ``partial(action_handler, ...)``
       是运行时模块全局查找，本补丁必须在 async_start 之前生效。
    """
    global _UPNP_PATCHED
    if _UPNP_PATCHED:
        return
    _UPNP_PATCHED = True

    _orig_async_start = UpnpServer.async_start

    async def _async_start_no_ssdp(self) -> None:  # noqa: ANN001, ANN202
        self._create_device()
        # 把 base_uri 里的 host 改成真实局域网 IP（self.source[0] 可能是 0.0.0.0）
        real_ip = get_local_ip()
        is_ipv6 = ":" in self.source[0]
        port = self.http_port if self.http_port else 0
        self.base_uri = (
            f"http://[{real_ip}]:{port}" if is_ipv6 else f"http://{real_ip}:{port}"
        )
        # device 实例是用旧 base_uri（0.0.0.0）建的，也要刷新它持有的 base_uri 与 host。
        # 注意库 UpnpServerDevice 的属性是无下划线的 self.base_uri / self.host
        # （async_upnp_client/server.py:418-419）；此前误写成 _base_uri 导致刷新失效。
        if self._device is not None:
            try:
                self._device.base_uri = self.base_uri  # noqa: SLF001
                self._device.host = real_ip  # noqa: SLF001
            except AttributeError:
                pass
        log.debug("UpnpServer base_uri = %s", self.base_uri)
        await self._async_start_http_server()
        # 注意：不调用 self._async_start_ssdp()

    UpnpServer.async_start = _async_start_no_ssdp
    log.debug("已 patch UpnpServer.async_start（跳过自带 SSDP，base_uri 用 %s）", get_local_ip())

    _orig_action_handler = getattr(_upnp_server_module, "action_handler", None)
    if _orig_action_handler is None:
        # 库升级改变了内部结构：控制点 IP 拿不到，授权将 fail-closed，
        # 所有状态变更 action 都会被拒绝——必须在这里就大声报错。
        log.error(
            "async_upnp_client.server.action_handler 不存在（库结构已变化），"
            "控制点授权上下文不可用；请检查库版本兼容性"
        )
        return

    async def _action_handler_with_context(service, request):  # noqa: ANN001, ANN202
        token = set_controller_ip(request.remote)
        try:
            return await _orig_action_handler(service, request)
        finally:
            reset_controller_ip(token)

    _upnp_server_module.action_handler = _action_handler_with_context
    log.debug("已 patch action_handler（登记控制点 IP 上下文）")


class DlnaServer:
    """DLNA MediaRenderer 服务封装（HTTP + 自研 SSDP）。"""

    def __init__(self, bridge: RendererBridge, config: Config) -> None:
        self._bridge = bridge
        self._config = config
        self._server: Optional[UpnpServer] = None
        self._ssdp: Optional[SsdpListener] = None
        self._running = False
        self._http_port: int = 0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def http_port(self) -> int:
        return self._http_port

    async def async_start(self) -> None:
        """启动 DLNA 服务（HTTP server + SSDP listener 线程）。"""
        if self._running:
            return

        # 库补丁（跳过自带 SSDP + 控制点 IP 上下文）
        _patch_upnp_server()

        friendly_name = self._config.get("friendly_name", "轻投")
        udn = self._config.get("udn")
        http_port = int(self._config.get("http_port", 0))

        device_cls = make_device_class(friendly_name, udn)
        # source 决定 HTTP/SOAP 的监听地址：默认 0.0.0.0 绑全部网卡（兼容行为）；
        # 「仅默认网卡」开启时只绑 get_local_ip() 所在网卡，SSDP 宣告白名单同步
        # 收窄，多网卡 / 虚拟网卡 / 热点环境下设备不再向其他网卡暴露。base_uri
        # patch 本来就用 get_local_ip() 拼 LOCATION，两种模式下地址都一致。
        ssdp_allowed_ips: Optional[list[str]] = None
        bind_host = "0.0.0.0"
        if bool(self._config.get("bind_default_interface_only", False)):
            bind_host = get_local_ip()
            ssdp_allowed_ips = [bind_host]
            log.info("仅通过默认网卡提供投屏服务: %s", bind_host)
        source = (bind_host, DEFAULT_SSDP_PORT)
        log.info("启动 DLNA 服务: name=%r udn=%s http_port=%s", friendly_name, udn, http_port)
        self._server = UpnpServer(
            server_device=device_cls,
            source=source,
            http_port=http_port or None,
        )
        await self._server.async_start()

        # 启动后从 device 实例拿 service，注入 bridge
        device = self._server._device  # noqa: SLF001  库未暴露公开访问器
        avt = _find_service(device, AVTransportService)
        rc = _find_service(device, RenderingControlService)
        cm = _find_service(device, ConnectionManagerService)
        if avt is None or rc is None or cm is None:
            # 缺 service 时继续提供服务会进入「安全组件未装配但 SOAP 写接口
            # 仍开放」的半失效状态（各 service 已 fail-closed，这里再让启动
            # 干脆失败，避免用户误以为投屏服务正常）。
            log.error(
                "无法从设备实例获取 service（avt=%s rc=%s cm=%s），启动失败",
                avt, rc, cm,
            )
            raise RuntimeError("无法从设备实例获取 DLNA service，DLNA 服务启动失败")
        self._bridge.set_services(avt, rc, cm)

        # 读取实际 HTTP 端口（http_port=0 时由系统分配）
        self._http_port = _resolve_http_port(self._server)
        if self._http_port == 0:
            # 兜底：从 base_uri 解析
            self._http_port = _port_from_base_uri(self._server.base_uri)

        # 启动自研 SSDP listener 线程
        server_id = f"{sys.platform}/{platform.version()} UPnP/1.1 {APP_NAME}/{APP_VERSION}"
        self._ssdp = SsdpListener(
            udn=udn,
            http_port=self._http_port,
            server_id=server_id,
            location_path="/device.xml",
            allowed_ips=ssdp_allowed_ips,
        )
        self._ssdp.start()
        # Thread.start() 只表示线程已调度，不代表 bind/join multicast 成功。
        # 同步 Event.wait 放到工作线程，避免阻塞 qasync/Qt 主事件循环。
        await asyncio.to_thread(self._ssdp.wait_until_ready)

        self._running = True
        log.info(
            "DLNA 服务已启动: http_port=%s 本机IP=%s SSDP=独立线程",
            self._http_port, get_local_ip(),
        )

    async def async_stop(self) -> None:
        """停止 DLNA 服务。"""
        # 启动协程可能在 _running=True 前失败或被取消；只要已有部分资源，
        # 仍须执行清理，避免 HTTP server / SSDP 线程残留。
        if not self._running and self._server is None and self._ssdp is None:
            return
        log.info("停止 DLNA 服务")
        # 先停 SSDP（发 byebye）
        ssdp = self._ssdp
        if ssdp is not None:
            # stop() 内部有有界 join，同样不能阻塞 qasync/Qt 主事件循环。
            await asyncio.to_thread(ssdp.stop)
            if self._ssdp is ssdp:
                self._ssdp = None
        self._bridge.shutdown()
        server = self._server
        if server is not None:
            try:
                await server.async_stop()
            except Exception as e:  # noqa: BLE001
                log.warning("async_stop 异常: %s", e)
                # 底层 listener 是否真正关闭未知：保留引用与 running 状态，
                # 让调用方可重试清理，不能宣称已停止后丢失唯一资源句柄。
                raise
        self._running = False
        self._server = None


def _find_service(device, service_cls):  # noqa: ANN001
    """从 UpnpServerDevice 实例上找指定类型的 service。

    ``device.services`` 是 ``dict[str, UpnpService]``（key 为 service_type URN）。
    """
    services = getattr(device, "services", {})
    iterable = services.values() if isinstance(services, dict) else services
    for svc in iterable:
        if isinstance(svc, service_cls):
            return svc
    return None


def _resolve_http_port(server: UpnpServer) -> int:
    """从 UpnpServer 内部拿到实际监听的 HTTP 端口。"""
    for attr in ("_site", "_http_site"):
        site = getattr(server, attr, None)
        if site is None:
            continue
        srv = getattr(site, "_server", None)
        if srv is not None:
            socks = getattr(srv, "sockets", None)
            if socks:
                for sock in socks:
                    port = sock.getsockname()[1]
                    if port:
                        return port
    return 0


def _port_from_base_uri(base_uri: Optional[str]) -> int:
    """从 base_uri 解析端口号。"""
    if not base_uri:
        return 0
    try:
        # 形如 http://192.168.0.104:8080
        _, _, hostport = base_uri.partition("://")
        _, _, port_str = hostport.partition(":")
        return int(port_str.split("/")[0]) if port_str else 0
    except (ValueError, IndexError):
        return 0
