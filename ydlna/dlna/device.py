"""MediaRenderer 设备声明。

把三个服务组装成一个 UPnP MediaRenderer 设备。device_type 前导冒号是
async_upnp_client server 模块的约定（见其 tests/test_server.py）。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Sequence, Type

from async_upnp_client.const import DeviceInfo
from async_upnp_client.server import UpnpServerDevice, UpnpServerService

from ..constants import APP_NAME, DEVICE_TYPE_MEDIA_RENDERER
from .avtransport import AVTransportService
from .connection_manager import ConnectionManagerService
from .rendering_control import RenderingControlService


def make_device_class(friendly_name: str, udn: str) -> Type[UpnpServerDevice]:
    """动态构造 MediaRendererDevice 类。

    friendly_name 和 udn 来自配置，运行时确定，因此用工厂函数生成类。
    """
    class MediaRendererDevice(UpnpServerDevice):
        DEVICE_DEFINITION = DeviceInfo(
            device_type=DEVICE_TYPE_MEDIA_RENDERER,  # 含前导冒号，库的约定
            friendly_name=friendly_name,
            manufacturer=APP_NAME,
            manufacturer_url=None,
            model_description="DLNA MediaRenderer powered by PySide6 + libmpv",
            model_name=APP_NAME,
            model_number="0.1",
            model_url=None,
            serial_number="0001",
            udn=udn,  # 形如 "uuid:xxxxxxxx-..."
            upc=None,
            presentation_url=None,
            url="/device.xml",
            icons=[],
            xml=ET.Element("device"),
        )
        EMBEDDED_DEVICES: Sequence[Type[UpnpServerDevice]] = []
        SERVICES = [AVTransportService, RenderingControlService, ConnectionManagerService]

    return MediaRendererDevice
