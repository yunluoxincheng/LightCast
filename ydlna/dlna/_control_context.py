"""SOAP 控制请求上下文：把真实控制点 IP 传递给 action 处理层。

async_upnp_client 的 action handler 只收业务参数，拿不到 HTTP 请求；
server 层的补丁在 ``action_handler`` 入口用 ``request.remote`` 设置本
ContextVar，Bridge 据此做「控制点授权」。每个 aiohttp 请求是独立任务，
ContextVar 按任务复制，天然隔离并发请求。
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

_controller_ip: ContextVar[Optional[str]] = ContextVar(
    "controller_ip", default=None
)


def current_controller_ip() -> Optional[str]:
    """当前 SOAP 请求的控制点 IP；不在请求上下文中时为 None。"""
    return _controller_ip.get()


def set_controller_ip(ip: Optional[str]) -> Token:
    """在当前请求上下文登记控制点 IP（由 server 层补丁调用）。"""
    return _controller_ip.set(ip)


def reset_controller_ip(token: Token) -> None:
    """恢复登记前的上下文值（与 set_controller_ip 配对）。"""
    _controller_ip.reset(token)
