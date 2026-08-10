from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ydlna.player import hls_rewriter


class _Request:
    headers: dict[str, str] = {}

    def __init__(self, raw_index: str) -> None:
        self.match_info = {"index": raw_index}


@pytest.mark.parametrize(
    ("raw_index", "size", "expected"),
    [
        ("0", 1, 0),
        ("01", 2, 1),
        ("1", 1, None),
        ("abc", 1, None),
        ("-1", 1, None),
        ("+0", 1, None),
        ("١", 2, None),  # Unicode 十进制数字也不属于代理路由白名单
        ("9" * 5000, 1, None),
    ],
)
def test_route_index_accepts_only_bounded_ascii_decimal(
    raw_index: str, size: int, expected: int | None
) -> None:
    request = _Request(raw_index)
    assert hls_rewriter._route_index(request, size) == expected  # type: ignore[arg-type]


def test_all_index_handlers_reject_invalid_or_out_of_range_values() -> None:
    async def scenario() -> None:
        proxy = hls_rewriter.HlsProxy()
        proxy._segments = ["segment-0"]
        proxy._keys = ["key-0"]
        proxy._maps = ["map-0"]
        handlers = (
            proxy._handle_segment,
            proxy._handle_hybrid_segment,
            proxy._handle_image_segment,
            proxy._handle_key,
            proxy._handle_map,
        )

        for raw_index in ("abc", "-1", "+0", "١", "1", "9" * 5000):
            for handler in handlers:
                response = await handler(_Request(raw_index))  # type: ignore[arg-type]
                assert response.status == 404

    asyncio.run(scenario())


def test_registered_routes_reject_non_decimal_indices_before_handlers(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        for mode, extension, handler_name in (
            ("video", "mp4", "_handle_segment"),
            ("image", "avi", "_handle_image_segment"),
            ("hybrid", "ts", "_handle_hybrid_segment"),
        ):
            proxy = hls_rewriter.HlsProxy()
            proxy._mode = mode
            reached: list[str] = []

            async def handler(request: web.Request) -> web.Response:
                reached.append(request.path)
                return web.Response(status=204)

            monkeypatch.setattr(proxy, handler_name, handler)
            monkeypatch.setattr(proxy, "_handle_key", handler)
            monkeypatch.setattr(proxy, "_handle_map", handler)

            app = web.Application()
            proxy._register_routes(app)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                valid_paths = (
                    f"/seg/0.{extension}",
                    "/key/0.key",
                    "/map/0.mp4",
                )
                for path in valid_paths:
                    response = await client.get(path)
                    assert response.status == 204
                    response.release()

                valid_call_count = len(reached)
                invalid_paths = (
                    f"/seg/abc.{extension}",
                    f"/seg/-1.{extension}",
                    f"/seg/+0.{extension}",
                    f"/seg/١.{extension}",
                    "/key/-1.key",
                    "/key/abc.key",
                    "/map/-1.mp4",
                    "/map/abc.mp4",
                )
                for path in invalid_paths:
                    response = await client.get(path)
                    assert response.status == 404
                    response.release()
                assert len(reached) == valid_call_count
            finally:
                await client.close()

    asyncio.run(scenario())
