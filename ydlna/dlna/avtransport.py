"""AVTransport service —— DLNA 投屏的核心服务。

负责接收手机推送的媒体 URI、控制播放/暂停/停止/定位、并回报播放进度。
这是 MediaRenderer 的「大脑」。

注意：本文件**不**使用 ``from __future__ import annotations``，因为
async_upnp_client 的 ``@callable_action`` 会在运行时比较方法参数的类型注解
（真类型对象，非字符串），PEP 563 的字符串注解会导致断言失败。
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

from ..constants import SERVICE_TYPE_AVTRANSPORT
from ..logger import get_logger

if TYPE_CHECKING:
    from .renderer_bridge import RendererBridge

log = get_logger("dlna.avt")


def _seconds_to_str(seconds: float | None) -> str:
    """把秒转成 DLNA 的 H:MM:SS(.frac) 格式字符串。None 返回 NOT_IMPLEMENTED。"""
    if seconds is None or seconds < 0:
        return "NOT_IMPLEMENTED"
    total = float(seconds)
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"{h}:{m:02d}:{s:05.2f}"


class AVTransportService(UpnpServerService):
    """AVTransport:1 服务。"""

    SERVICE_DEFINITION = ServiceInfo(
        service_id="urn:upnp-org:serviceId:AVTransport",
        service_type=SERVICE_TYPE_AVTRANSPORT,
        control_url="/upnp/control/AVTransport",
        event_sub_url="/upnp/event/AVTransport",
        scpd_url="/AVTransport/scpd.xml",
        xml=ET.Element("service"),
    )

    STATE_VARIABLE_DEFINITIONS = {
        # ---- 参数类型（不入事件，仅用于 SCPD 声明）----
        "A_ARG_TYPE_InstanceID": create_state_var("ui4"),
        "A_ARG_TYPE_SeekMode": create_state_var(
            "string", allowed=["ABS_TIME", "REL_TIME", "ABS_COUNT", "REL_COUNT", "TRACK_NR"]
        ),
        "A_ARG_TYPE_SeekTarget": create_state_var("string"),
        "A_ARG_TYPE_Speed": create_state_var("string", default="1"),

        # ---- 传输状态（入事件，手机端据此刷新进度条/按钮）----
        "TransportState": create_event_var(
            "string", default="NO_MEDIA_PRESENT",
            allowed=["STOPPED", "PLAYING", "TRANSITIONING", "PAUSED_PLAYBACK", "NO_MEDIA_PRESENT"],
        ),
        "TransportStatus": create_event_var("string", default="OK", allowed=["OK", "ERROR_OCCURRED"]),
        "TransportPlaySpeed": create_state_var("string", default="1"),
        "PossiblePlaybackStorageMedia": create_state_var(
            "string", default="NONE, NETWORK"
        ),
        "PossibleRecordStorageMedia": create_state_var("string", default="NOT_IMPLEMENTED"),
        "PossibleRecordQualityModes": create_state_var("string", default="NOT_IMPLEMENTED"),
        "CurrentPlayMode": create_event_var("string", default="NORMAL"),
        "CurrentRecordQualityMode": create_event_var("string", default="NOT_IMPLEMENTED"),

        # ---- 媒体信息（入事件）----
        "AVTransportURI": create_event_var("string"),
        "AVTransportURIMetaData": create_event_var("string"),
        "NextAVTransportURI": create_event_var("string", default=""),
        "NextAVTransportURIMetaData": create_event_var("string", default=""),
        "CurrentMediaDuration": create_state_var("string", default="NOT_IMPLEMENTED"),
        "CurrentTrack": create_state_var("ui4", default="0"),
        "CurrentTrackDuration": create_state_var("string", default="NOT_IMPLEMENTED"),
        "CurrentTrackMetaData": create_state_var("string"),
        "CurrentTrackURI": create_state_var("string"),

        # ---- 进度（DLNA GetPositionInfo 返回这些；不入事件，按需查询）----
        "RelativeTimePosition": create_state_var("string", default="NOT_IMPLEMENTED"),
        "AbsoluteTimePosition": create_state_var("string", default="NOT_IMPLEMENTED"),
        "RelativeCounterPosition": create_state_var("i4", default="-1"),
        "AbsoluteCounterPosition": create_state_var("i4", default="-1"),

        # LastChange：AVTransport 用单变量聚合事件，部分控制点依赖
        "LastChange": create_event_var("string", default=""),

        # ---- 其它声明（GetMediaInfo / GetCurrentTransportActions 用）----
        "NumberOfTracks": create_state_var("ui4", default="0"),
        "PlaybackStorageMedium": create_state_var("string", default="NETWORK"),
        "RecordStorageMedium": create_state_var("string", default="NOT_IMPLEMENTED"),
        "RecordMediumWriteStatus": create_state_var("string", default="NOT_IMPLEMENTED"),
        "CurrentTransportActions": create_state_var("string", default="Play,Pause,Stop,Seek"),
    }

    def __init__(self, requester) -> None:  # noqa: ANN001
        super().__init__(requester=requester)
        self.bridge: "RendererBridge | None" = None

    # ------------------------------------------------------------------ #
    # 媒体设置
    # ------------------------------------------------------------------ #
    @callable_action(
        name="SetAVTransportURI",
        in_args={
            "InstanceID": "A_ARG_TYPE_InstanceID",
            "CurrentURI": "AVTransportURI",
            "CurrentURIMetaData": "AVTransportURIMetaData",
        },
        out_args={},
    )
    async def set_av_transport_uri(  # noqa: ANN001
        self,
        InstanceID: int,  # pylint: disable=invalid-name
        CurrentURI: str,  # pylint: disable=invalid-name
        CurrentURIMetaData: str = "",  # pylint: disable=invalid-name
    ) -> dict[str, UpnpStateVariable]:
        log.info("SetAVTransportURI: %s", CurrentURI)
        self.state_variable("AVTransportURI").value = CurrentURI
        self.state_variable("AVTransportURIMetaData").value = CurrentURIMetaData or ""
        self.state_variable("TransportState").value = "STOPPED"
        if self.bridge is not None:
            await self.bridge.on_set_uri(CurrentURI, CurrentURIMetaData or "")
        return {}

    @callable_action(
        name="SetNextAVTransportURI",
        in_args={
            "InstanceID": "A_ARG_TYPE_InstanceID",
            "NextURI": "NextAVTransportURI",
            "NextURIMetaData": "NextAVTransportURIMetaData",
        },
        out_args={},
    )
    async def set_next_av_transport_uri(  # noqa: ANN001
        self,
        InstanceID: int,  # pylint: disable=invalid-name
        NextURI: str,  # pylint: disable=invalid-name
        NextURIMetaData: str = "",  # pylint: disable=invalid-name
    ) -> dict[str, UpnpStateVariable]:
        log.info("SetNextAVTransportURI: %s", NextURI)
        self.state_variable("NextAVTransportURI").value = NextURI
        self.state_variable("NextAVTransportURIMetaData").value = NextURIMetaData or ""
        return {}

    # ------------------------------------------------------------------ #
    # 传输控制
    # ------------------------------------------------------------------ #
    @callable_action(
        name="Play",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID", "Speed": "TransportPlaySpeed"},
        out_args={},
    )
    async def play(  # noqa: ANN001
        self, InstanceID: int, Speed: str = "1"  # pylint: disable=invalid-name
    ) -> dict[str, UpnpStateVariable]:
        log.info("Play (speed=%s)", Speed)
        if self.bridge is not None:
            self.bridge.on_play()
        return {}

    @callable_action(
        name="Pause",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={},
    )
    async def pause(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        log.info("Pause")
        if self.bridge is not None:
            self.bridge.on_pause()
        return {}

    @callable_action(
        name="Stop",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={},
    )
    async def stop(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        log.info("Stop")
        if self.bridge is not None:
            self.bridge.on_stop()
        return {}

    @callable_action(
        name="Seek",
        in_args={
            "InstanceID": "A_ARG_TYPE_InstanceID",
            "Unit": "A_ARG_TYPE_SeekMode",
            "Target": "A_ARG_TYPE_SeekTarget",
        },
        out_args={},
    )
    async def seek(  # noqa: ANN001
        self,
        InstanceID: int,  # pylint: disable=invalid-name
        Unit: str,  # pylint: disable=invalid-name
        Target: str,  # pylint: disable=invalid-name
    ) -> dict[str, UpnpStateVariable]:
        log.info("Seek: unit=%s target=%s", Unit, Target)
        if self.bridge is not None:
            self.bridge.on_seek(Unit, Target)
        return {}

    @callable_action(
        name="Next",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={},
    )
    async def next_(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        return {}

    @callable_action(
        name="Previous",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={},
    )
    async def previous(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        return {}

    # ------------------------------------------------------------------ #
    # 信息查询
    # ------------------------------------------------------------------ #
    @callable_action(
        name="GetTransportInfo",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={
            "CurrentTransportState": "TransportState",
            "CurrentTransportStatus": "TransportStatus",
            "CurrentSpeed": "TransportPlaySpeed",
        },
    )
    async def get_transport_info(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        return {
            "CurrentTransportState": self.state_variable("TransportState"),
            "CurrentTransportStatus": self.state_variable("TransportStatus"),
            "CurrentSpeed": self.state_variable("TransportPlaySpeed"),
        }

    @callable_action(
        name="GetPositionInfo",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={
            "Track": "CurrentTrack",
            "TrackDuration": "CurrentMediaDuration",
            "TrackMetaData": "AVTransportURIMetaData",
            "TrackURI": "AVTransportURI",
            "RelTime": "RelativeTimePosition",
            "AbsTime": "AbsoluteTimePosition",
            "RelCount": "RelativeCounterPosition",
            "AbsCount": "AbsoluteCounterPosition",
        },
    )
    async def get_position_info(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        # 进度由 RendererBridge 周期性写回 RelativeTimePosition / CurrentMediaDuration
        return {
            "Track": self.state_variable("CurrentTrack"),
            "TrackDuration": self.state_variable("CurrentMediaDuration"),
            "TrackMetaData": self.state_variable("AVTransportURIMetaData"),
            "TrackURI": self.state_variable("AVTransportURI"),
            "RelTime": self.state_variable("RelativeTimePosition"),
            "AbsTime": self.state_variable("AbsoluteTimePosition"),
            "RelCount": self.state_variable("RelativeCounterPosition"),
            "AbsCount": self.state_variable("AbsoluteCounterPosition"),
        }

    @callable_action(
        name="GetMediaInfo",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={
            "NrTracks": "NumberOfTracks",
            "MediaDuration": "CurrentMediaDuration",
            "CurrentURI": "AVTransportURI",
            "CurrentURIMetaData": "AVTransportURIMetaData",
            "NextURI": "NextAVTransportURI",
            "NextURIMetaData": "NextAVTransportURIMetaData",
            "PlayMedium": "PlaybackStorageMedium",
            "RecordMedium": "RecordStorageMedium",
            "WriteStatus": "RecordMediumWriteStatus",
        },
    )
    async def get_media_info(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        return {
            "NrTracks": self.state_variable("NumberOfTracks"),
            "MediaDuration": self.state_variable("CurrentMediaDuration"),
            "CurrentURI": self.state_variable("AVTransportURI"),
            "CurrentURIMetaData": self.state_variable("AVTransportURIMetaData"),
            "NextURI": self.state_variable("NextAVTransportURI"),
            "NextURIMetaData": self.state_variable("NextAVTransportURIMetaData"),
            "PlayMedium": self.state_variable("PlaybackStorageMedium"),
            "RecordMedium": self.state_variable("RecordStorageMedium"),
            "WriteStatus": self.state_variable("RecordMediumWriteStatus"),
        }

    @callable_action(
        name="GetDeviceCapabilities",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={},
    )
    async def get_device_capabilities(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        return {}

    @callable_action(
        name="GetCurrentTransportActions",
        in_args={"InstanceID": "A_ARG_TYPE_InstanceID"},
        out_args={"Actions": "CurrentTransportActions"},
    )
    async def get_current_transport_actions(self, InstanceID: int) -> dict[str, UpnpStateVariable]:  # noqa: ANN001  # pylint: disable=invalid-name
        return {"Actions": self.state_variable("CurrentTransportActions")}
