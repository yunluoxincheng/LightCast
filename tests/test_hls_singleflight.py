from __future__ import annotations

import asyncio

import pytest

from ydlna.player import hls_rewriter


class _Request:
    headers: dict[str, str] = {}

    def __init__(self, index: int | None = None) -> None:
        self.match_info = {} if index is None else {"index": str(index)}


def test_key_requests_share_fetch_and_one_waiter_can_cancel(monkeypatch) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        get_calls = 0

        class Content:
            async def readexactly(self, size: int) -> bytes:
                assert size == 17
                started.set()
                await release.wait()
                raise asyncio.IncompleteReadError(b"k" * 16, size)

        class Response:
            status = 200
            content = Content()

            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        response = Response()
        proxy = hls_rewriter.HlsProxy()
        proxy._keys = ["https://public.example/key"]

        async def get(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            nonlocal get_calls
            get_calls += 1
            return response

        monkeypatch.setattr(proxy, "_get", get)
        first = asyncio.create_task(proxy._handle_key(_Request(0)))  # type: ignore[arg-type]
        await started.wait()
        second = asyncio.create_task(proxy._handle_key(_Request(0)))  # type: ignore[arg-type]
        await asyncio.sleep(0)

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        release.set()
        result = await second
        await asyncio.sleep(0)

        assert result.body == b"k" * 16
        assert get_calls == 1
        assert response.closed
        assert proxy._inflight == {}
        assert len(proxy._background_tasks) == 0

    asyncio.run(scenario())


def test_failed_singleflight_is_shared_but_does_not_block_retry(monkeypatch) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        proxy = hls_rewriter.HlsProxy()
        proxy._keys = ["https://public.example/key"]

        async def load(_url: str, _headers: dict[str, str]) -> tuple[int, str]:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return 502, "key length invalid"

        monkeypatch.setattr(proxy, "_load_key", load)
        first = asyncio.create_task(proxy._handle_key(_Request(0)))  # type: ignore[arg-type]
        await started.wait()
        second = asyncio.create_task(proxy._handle_key(_Request(0)))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        release.set()

        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.status == second_result.status == 502
        assert calls == 1

        await asyncio.sleep(0)
        retry = await proxy._handle_key(_Request(0))  # type: ignore[arg-type]
        assert retry.status == 502
        assert calls == 2

    asyncio.run(scenario())


def test_hybrid_warm_and_player_request_share_segment_fetch(monkeypatch) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        proxy = hls_rewriter.HlsProxy()
        proxy._segments = ["seg-0", "seg-1"]

        async def buffer(index: int, _request=None, **_kwargs) -> bool:  # noqa: ANN001
            nonlocal calls
            assert index == 1
            calls += 1
            started.set()
            await release.wait()
            proxy._ts_cache[index] = b"shared-ts"
            proxy._ts_cache_order.append(index)
            return True

        monkeypatch.setattr(proxy, "_buffer_hybrid", buffer)
        proxy._schedule_warm(0)
        await started.wait()

        request_task = asyncio.create_task(
            proxy._handle_hybrid_segment(_Request(1))  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
        assert not request_task.done()

        release.set()
        result = await request_task
        await asyncio.sleep(0)

        assert result.body == b"shared-ts"
        assert calls == 1
        assert proxy._inflight == {}

    asyncio.run(scenario())


def test_player_requests_share_retry_after_joined_warm_flight_fails(monkeypatch) -> None:
    async def scenario() -> None:
        warm_started = asyncio.Event()
        release_warm = asyncio.Event()
        calls: list[tuple[object | None, int]] = []
        active = 0
        max_active = 0

        proxy = hls_rewriter.HlsProxy()
        proxy._segments = ["seg-0", "seg-1"]
        player_request = _Request(1)
        player_request.headers = {"X-Playback": "foreground"}
        second_request = _Request(1)
        second_request.headers = {"X-Playback": "second-foreground"}

        async def buffer(
            index: int,
            request=None,  # noqa: ANN001
            *,
            retries: int = 1,
        ) -> bool:
            nonlocal active, max_active
            assert index == 1
            calls.append((request, retries))
            active += 1
            max_active = max(max_active, active)
            try:
                if retries == 0:
                    warm_started.set()
                    await release_warm.wait()
                    return False
                proxy._ts_cache[index] = b"foreground-retry"
                proxy._ts_cache_order.append(index)
                return True
            finally:
                active -= 1

        monkeypatch.setattr(proxy, "_buffer_hybrid", buffer)
        proxy._schedule_warm(0)
        await warm_started.wait()

        player_task = asyncio.create_task(
            proxy._handle_hybrid_segment(player_request)  # type: ignore[arg-type]
        )
        second_player_task = asyncio.create_task(
            proxy._handle_hybrid_segment(second_request)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
        assert not player_task.done()
        assert not second_player_task.done()

        release_warm.set()
        result, second_result = await asyncio.gather(
            player_task, second_player_task
        )
        await asyncio.sleep(0)

        assert result.status == second_result.status == 200
        assert result.body == second_result.body == b"foreground-retry"
        assert calls[0] == (None, 0)
        assert calls[1][0] in (player_request, second_request)
        assert calls[1][1] == 1
        assert len(calls) == 2
        assert max_active == 1
        assert proxy._inflight == {}
        assert proxy._hybrid_warm_flights == {}

    asyncio.run(scenario())


def test_image_requests_share_conversion(monkeypatch) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        proxy = hls_rewriter.HlsProxy()
        proxy._segments = ["image-0"]

        async def convert(index: int, _request) -> bool:  # noqa: ANN001
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            proxy._jpeg_cache[index] = b"jpeg"
            proxy._jpeg_sizes[index] = (1, 1)
            return True

        monkeypatch.setattr(proxy, "_convert_segment", convert)
        first = asyncio.create_task(
            proxy._handle_image_segment(_Request(0))  # type: ignore[arg-type]
        )
        await started.wait()
        second = asyncio.create_task(
            proxy._handle_image_segment(_Request(0))  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
        release.set()

        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.body == second_result.body
        assert calls == 1

    asyncio.run(scenario())


def test_direct_requests_share_buffer(monkeypatch) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        proxy = hls_rewriter.DirectProxy()
        proxy._mode = "hybrid"
        proxy._url = "https://public.example/media"

        async def buffer(_request) -> bool:  # noqa: ANN001
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            proxy._data = b"shared-media"
            return True

        monkeypatch.setattr(proxy, "_buffer_once", buffer)
        first = asyncio.create_task(proxy._handle_stream(_Request()))  # type: ignore[arg-type]
        await started.wait()
        second = asyncio.create_task(proxy._handle_stream(_Request()))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        release.set()

        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.body == second_result.body == b"shared-media"
        assert calls == 1

    asyncio.run(scenario())
