"""RenderingControl service —— 音量 / 静音控制。

手机调节音量、静音都通过这个服务。所有 action 都需要 InstanceID 和 Channel 参数。

注意：不使用 ``from __future__ import annotations``（见 avtransport.py 说明）。
"""

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from async_upnp_client.client import UpnpStateVariable
from async_upnp_client.const import ServiceInfo
from async_upnp_client.exceptions import UpnpActionError
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


def _require_bridge(bridge: "RendererBridge | None", action: str) -> "RendererBridge":
    """fail-closed：Bridge 未装配时状态变更类 action 一律 SOAP fault。"""
    if bridge is None:
        log.error("%s 被拒绝：RendererBridge 未装配", action)
        raise UpnpActionError(
            error_desc="服务未就绪", message="bridge not available"
        )
    return bridge


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
        # 授权在前：service 的 Volume 是 evented 状态变量，先写再查授权会让
        # 未授权控制点的请求经 GENA 生效（假象"已接受"）。未授权时
        # on_set_volume 抛 UpnpActionError，不产生任何状态副作用。
        _require_bridge(self.bridge, "SetVolume").on_set_volume(vol)
        self.state_variable("Volume").value = vol  # 触发 GENA 事件
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
        # 授权在前，理由同 set_volume：evented 状态不得在授权前变化。
        _require_bridge(self.bridge, "SetMute").on_set_mute(muted)
        # Mute 状态变量是 boolean 类型：必须写真实 bool，写 "1"/"0" 字符串
        # 会被库的 schema 拒绝（UpnpValueError），导致静音状态从未同步成功。
        self.state_variable("Mute").value = muted
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
