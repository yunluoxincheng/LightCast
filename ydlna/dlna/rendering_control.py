"""RenderingControl service —— 音量 / 静音控制。

手机调节音量、静音都通过这个服务。所有 action 都需要 InstanceID 和 Channel 参数。

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

from ..constants import SERVICE_TYPE_RENDERING_CONTROL
from ..logger import get_logger

if TYPE_CHECKING:
    from .renderer_bridge import RendererBridge

log = get_logger("dlna.rctrl")


class RenderingControlService(UpnpServerService):
    """RenderingControl:1 服务（只实现 Master 声道的音量/静音）。"""

    SERVICE_DEFINITION = ServiceInfo(
        service_id="urn:upnp-org:serviceId:RenderingControl",
        service_type=SERVICE_TYPE_RENDERING_CONTROL,
        control_url="/upnp/control/RenderingControl",
        event_sub_url="/upnp/event/RenderingControl",
        scpd_url="/RenderingControl/scpd.xml",
        xml=ET.Element("service"),
    )

    STATE_VARIABLE_DEFINITIONS = {
        "A_ARG_TYPE_InstanceID": create_state_var("ui4"),
        "A_ARG_TYPE_Channel": create_state_var(
            "string",
            allowed=["Master", "LF", "RF", "CF", "LFE", "LS", "RS", "LFC", "RFC", "SD", "SL", "SR", "T", "B"],
            default="Master",
        ),
        "A_ARG_TYPE_Volume": create_state_var("ui2", allowed_range={"minimum": "0", "maximum": "100"}),
        "PresetNameList": create_state_var("string", default="FactoryDefaults"),
        "Volume": create_event_var("ui2", default="80", allowed_range={"minimum": "0", "maximum": "100"}),
        "Mute": create_event_var("boolean", default="0"),
        "Brightness": create_event_var("ui2"),
        "Contrast": create_event_var("ui2"),
        "Loudness": create_event_var("boolean"),
    }

    def __init__(self, requester) -> None:  # noqa: ANN001
        super().__init__(requester=requester)
        self.bridge: "RendererBridge | None" = None

    # ------------------------------------------------------------------ #
    # 音量
    # ------------------------------------------------------------------ #
    @callable_action(
        name="GetVolume",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID", "Channel": "A_ARG_TYPE_Channel"},
        out_args={"CurrentVolume": "Volume"},
    )
    async def get_volume(  # noqa: ANN001
        self, InstanceID: int, Channel: str  # pylint: disable=invalid-name
    ) -> dict[str, UpnpStateVariable]:
        return {"CurrentVolume": self.state_variable("Volume")}

    @callable_action(
        name="SetVolume",
        in_args={
            "InstanceID": "A_ARG_TYPE_InstanceID",
            "Channel": "A_ARG_TYPE_Channel",
            "DesiredVolume": "A_ARG_TYPE_Volume",
        },
        out_args={},
    )
    async def set_volume(  # noqa: ANN001
        self, InstanceID: int, Channel: str, DesiredVolume: int  # pylint: disable=invalid-name
    ) -> dict[str, UpnpStateVariable]:
        vol = max(0, min(100, int(DesiredVolume)))
        log.info("设置音量: %d (channel=%s)", vol, Channel)
        self.state_variable("Volume").value = vol  # 触发 GENA 事件
        if self.bridge is not None:
            self.bridge.on_set_volume(vol)
        return {}

    # ------------------------------------------------------------------ #
    # 静音
    # ------------------------------------------------------------------ #
    @callable_action(
        name="GetMute",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID", "Channel": "A_ARG_TYPE_Channel"},
        out_args={"CurrentMute": "Mute"},
    )
    async def get_mute(  # noqa: ANN001
        self, InstanceID: int, Channel: str  # pylint: disable=invalid-name
    ) -> dict[str, UpnpStateVariable]:
        return {"CurrentMute": self.state_variable("Mute")}

    @callable_action(
        name="SetMute",
        in_args={
            "InstanceID": "A_ARG_TYPE_InstanceID",
            "Channel": "A_ARG_TYPE_Channel",
            "DesiredMute": "Mute",
        },
        out_args={},
    )
    async def set_mute(  # noqa: ANN001
        self, InstanceID: int, Channel: str, DesiredMute: bool  # pylint: disable=invalid-name
    ) -> dict[str, UpnpStateVariable]:
        muted = bool(DesiredMute)
        log.info("设置静音: %s (channel=%s)", muted, Channel)
        self.state_variable("Mute").value = "1" if muted else "0"
        if self.bridge is not None:
            self.bridge.on_set_mute(muted)
        return {}

    # ------------------------------------------------------------------ #
    # Preset（占位实现，返回 FactoryDefaults）
    # ------------------------------------------------------------------ #
    @callable_action(
        name="ListPresets",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={"CurrentPresetNameList": "PresetNameList"},
    )
    async def list_presets(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        return {"CurrentPresetNameList": self.state_variable("PresetNameList")}

    @callable_action(
        name="SelectPresets",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={},
    )
    async def select_presets(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        # 占位：不真正应用 preset
        return {}
