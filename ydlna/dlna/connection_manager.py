"""ConnectionManager service —— 声明本渲染器支持的协议。

只有 GetProtocolInfo 一个 action，告诉控制点我们能接收什么样的流。

注意：不使用 ``from __future__ import annotations``（见 avtransport.py 说明）。
"""

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from async_upnp_client.client import UpnpStateVariable
from async_upnp_client.const import ServiceInfo
from async_upnp_client.server import (
    UpnpServerService,
    callable_action,
    create_event_var,
    create_state_var,
)

from ..constants import SERVICE_TYPE_CONNECTION_MANAGER

if TYPE_CHECKING:
    from .renderer_bridge import RendererBridge


class ConnectionManagerService(UpnpServerService):
    """ConnectionManager:1 服务。"""

    SERVICE_DEFINITION = ServiceInfo(
        service_id="urn:upnp-org:serviceId:ConnectionManager",
        service_type=SERVICE_TYPE_CONNECTION_MANAGER,
        control_url="/upnp/control/ConnectionManager",
        event_sub_url="/upnp/event/ConnectionManager",
        scpd_url="/ConnectionManager/scpd.xml",
        xml=ET.Element("service"),
    )

    STATE_VARIABLE_DEFINITIONS = {
        # 声明支持的协议。手机会先查这个判断能否投。
        # http-get:*:*:* 表示接受任意 HTTP 流（DLNA 最常见）
        "SourceProtocolInfo": create_event_var("string", default=""),
        "SinkProtocolInfo": create_event_var(
            "string",
            default=(
                "http-get:*:video/*:*,"
                "http-get:*:audio/*:*,"
                "http-get:*:image/*:*,"
                "http-get:*:application/octet-stream:*"
            ),
        ),
        "A_ARG_TYPE_ConnectionStatus": create_event_var(
            "string", default="OK", allowed=["OK", "ContentFormatMismatch", "InsufficientBandwidth", "UnreliableChannel", "Unknown"]
        ),
        "A_ARG_TYPE_Direction": create_state_var("string", allowed=["Output", "Input"]),
        "A_ARG_TYPE_ProtocolInfo": create_state_var("string"),
        "A_ARG_TYPE_ConnectionID": create_state_var("i4"),
        "A_ARG_TYPE_AVTransportID": create_state_var("i4"),
        "A_ARG_TYPE_RcsID": create_state_var("i4"),
        "A_ARG_TYPE_ConnectionManager": create_state_var("string"),
    }

    def __init__(self, requester) -> None:  # noqa: ANN001
        super().__init__(requester=requester)
        # 由 RendererBridge 注入，便于在 action 里回调播放器
        self.bridge: "RendererBridge | None" = None

    @callable_action(
        name="GetProtocolInfo",
        in_args={},
        out_args={"Source": "SourceProtocolInfo", "Sink": "SinkProtocolInfo"},
    )
    async def get_protocol_info(self) -> dict[str, UpnpStateVariable]:
        return {
            "Source": self.state_variable("SourceProtocolInfo"),
            "Sink": self.state_variable("SinkProtocolInfo"),
        }

    @callable_action(
        name="GetCurrentConnectionIDs",
        in_args={},
        out_args={"ConnectionIDs": "A_ARG_TYPE_ConnectionID"},
    )
    async def get_current_connection_ids(self) -> dict[str, UpnpStateVariable]:
        # 单实例渲染器，只返回 "0"
        return {"ConnectionIDs": self.state_variable("A_ARG_TYPE_ConnectionID")}

    @callable_action(
        name="GetCurrentConnectionInfo",
        in_args={"ConnectionID": "A_ARG_TYPE_ConnectionID"},
        out_args={
            "RcsID": "A_ARG_TYPE_RcsID",
            "AVTransportID": "A_ARG_TYPE_AVTransportID",
            "ProtocolInfo": "A_ARG_TYPE_ProtocolInfo",
            "PeerConnectionManager": "A_ARG_TYPE_ConnectionManager",
            "PeerConnectionID": "A_ARG_TYPE_ConnectionID",
            "Direction": "A_ARG_TYPE_Direction",
            "Status": "A_ARG_TYPE_ConnectionStatus",
        },
    )
    async def get_current_connection_info(  # noqa: ANN001
        self, ConnectionID: int  # pylint: disable=invalid-name
    ) -> dict[str, UpnpStateVariable]:
        return {
            "RcsID": self.state_variable("A_ARG_TYPE_RcsID"),
            "AVTransportID": self.state_variable("A_ARG_TYPE_AVTransportID"),
            "ProtocolInfo": self.state_variable("A_ARG_TYPE_ProtocolInfo"),
            "PeerConnectionManager": self.state_variable("A_ARG_TYPE_ConnectionManager"),
            "PeerConnectionID": self.state_variable("A_ARG_TYPE_ConnectionID"),
            "Direction": self.state_variable("A_ARG_TYPE_Direction"),
            "Status": self.state_variable("A_ARG_TYPE_ConnectionStatus"),
        }
