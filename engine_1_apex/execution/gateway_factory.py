"""Gateway factory — paper vs live execution mode."""

from __future__ import annotations

import asyncio
import os

from engine_1_apex.execution.execution_wrapper import ExecutionWrapper
from engine_1_apex.execution.rpc_proxy import RPCFailoverProxy
from engine_1_apex.gateway import PaperGateway
from engine_1_apex.live_gateway import LiveGateway


def execution_mode() -> str:
    return os.getenv("EXECUTION_MODE", "paper").lower()


def create_gateway_for_mode(
    mode: str,
    *,
    rpc: RPCFailoverProxy | None = None,
    wrapper: ExecutionWrapper | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
):
    normalized = mode.lower()
    if normalized == "live":
        if rpc is None or wrapper is None or loop is None:
            raise RuntimeError(
                "Live execution requires rpc, wrapper, and asyncio loop instances"
            )
        return LiveGateway(wrapper=wrapper, loop=loop)
    return PaperGateway()


def create_gateway(
    *,
    rpc: RPCFailoverProxy | None = None,
    wrapper: ExecutionWrapper | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    mode: str | None = None,
):
    selected = (mode or execution_mode()).lower()
    return create_gateway_for_mode(
        selected, rpc=rpc, wrapper=wrapper, loop=loop
    )
