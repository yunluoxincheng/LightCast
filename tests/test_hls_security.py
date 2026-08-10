from __future__ import annotations

import asyncio
import io
import warnings

import pytest
from PIL import Image

from ydlna.player import hls_rewriter


class _FakeContent:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.requested = 0

    async def readexactly(self, size: int) -> bytes:
        self.requested = size
        if len(self.data) < size:
            raise asyncio.IncompleteReadError(self.data, size)
        return self.data[:size]


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.content = _FakeContent(data)


@pytest.mark.parametrize(
    ("length", "accepted"),
    [(15, False), (16, True), (17, False), (100, False)],
)
def test_aes128_key_requires_exactly_16_bytes(length: int, accepted: bool) -> None:
    response = _FakeResponse(b"k" * length)
    result = asyncio.run(hls_rewriter._read_aes128_key(response))  # type: ignore[arg-type]
    assert (result is not None) is accepted
    assert response.content.requested == 17


def test_pillow_limit_is_4096_squared_pixels() -> None:
    assert Image.MAX_IMAGE_PIXELS == 4096 * 4096


def test_image_conversion_turns_decompression_warning_into_error(monkeypatch) -> None:
    def bomb(_stream):  # noqa: ANN001, ANN202
        warnings.warn("oversized", Image.DecompressionBombWarning)

    monkeypatch.setattr(Image, "open", bomb)
    with pytest.raises(Image.DecompressionBombWarning):
        hls_rewriter._image_to_jpeg(b"not-used")


def test_image_conversion_returns_jpeg_and_original_size() -> None:
    source = io.BytesIO()
    Image.new("RGBA", (12, 7), (255, 0, 0, 128)).save(source, "PNG")
    jpeg, size = hls_rewriter._image_to_jpeg(source.getvalue())
    assert size == (12, 7)
    assert jpeg.startswith(b"\xff\xd8\xff")


def test_direct_proxy_does_not_forward_pixel_bomb_to_mpv(monkeypatch) -> None:
    class ChunkContent:
        async def iter_chunked(self, _size: int):  # noqa: ANN202
            yield b"image-bytes"

    class Response:
        status = 200
        content = ChunkContent()

        def close(self) -> None:
            pass

    class Request:
        headers: dict[str, str] = {}

    proxy = hls_rewriter.DirectProxy()
    proxy._mode = "image"
    proxy._url = "https://public.example/large.png"

    async def get(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return Response()

    def bomb(_data: bytes):  # noqa: ANN202
        raise Image.DecompressionBombWarning("oversized")

    proxy._get = get
    monkeypatch.setattr(hls_rewriter, "_image_to_jpeg", bomb)

    assert asyncio.run(proxy._buffer_once(Request())) is False  # type: ignore[arg-type]
    assert proxy._mode == "image"


def test_forward_uses_bounded_stream_timeout(monkeypatch) -> None:
    class Content:
        async def read(self, _size: int) -> bytes:
            return b"video"

        async def iter_chunked(self, _size: int):  # noqa: ANN202
            yield b"-data"

    class Response:
        status = 200
        headers: dict[str, str] = {}
        content = Content()
        closed = False

        def close(self) -> None:
            self.closed = True

    class Request:
        headers: dict[str, str] = {}

    class Stream:
        def __init__(self, *, status: int) -> None:
            self.status = status
            self.headers: dict[str, str] = {}
            self.content_type = ""
            self.written: list[bytes] = []

        async def prepare(self, _request) -> None:  # noqa: ANN001
            return None

        async def write(self, data: bytes) -> None:
            self.written.append(data)

        async def write_eof(self) -> None:
            return None

    response = Response()
    seen_timeout = None

    async def safe_get(_session, _url, **kwargs):  # noqa: ANN001, ANN003
        nonlocal seen_timeout
        seen_timeout = kwargs.get("timeout")
        return response

    proxy = hls_rewriter.DirectProxy()
    proxy._session = object()  # type: ignore[assignment]
    monkeypatch.setattr(hls_rewriter, "safe_get", safe_get)
    monkeypatch.setattr(hls_rewriter.web, "StreamResponse", Stream)

    result = asyncio.run(
        proxy._forward_url(  # type: ignore[arg-type]
            Request(), "https://public.example/video", "媒体"
        )
    )

    assert seen_timeout is hls_rewriter._TIMEOUT_FORWARD
    assert seen_timeout.total is None
    assert seen_timeout.connect == 10
    assert seen_timeout.sock_read == 30
    assert result.written == [b"video", b"-data"]
    assert response.closed


def test_forward_timeout_before_local_response_returns_504(monkeypatch) -> None:
    class Content:
        async def read(self, _size: int) -> bytes:
            raise asyncio.TimeoutError

    class Response:
        status = 200
        headers: dict[str, str] = {}
        content = Content()
        closed = False

        def close(self) -> None:
            self.closed = True

    class Request:
        headers: dict[str, str] = {}

    response = Response()

    async def safe_get(_session, _url, **_kwargs):  # noqa: ANN001, ANN003
        return response

    proxy = hls_rewriter.DirectProxy()
    proxy._session = object()  # type: ignore[assignment]
    monkeypatch.setattr(hls_rewriter, "safe_get", safe_get)

    result = asyncio.run(
        proxy._forward_url(  # type: ignore[arg-type]
            Request(), "https://slow.example/video", "媒体"
        )
    )

    assert result.status == 504
    assert response.closed


def test_cancelled_forward_closes_upstream_response(monkeypatch) -> None:
    class Content:
        async def read(self, _size: int) -> bytes:
            raise asyncio.CancelledError

    class Response:
        status = 200
        headers: dict[str, str] = {}
        content = Content()
        closed = False

        def close(self) -> None:
            self.closed = True

    class Request:
        headers: dict[str, str] = {}

    response = Response()

    async def safe_get(_session, _url, **_kwargs):  # noqa: ANN001, ANN003
        return response

    proxy = hls_rewriter.DirectProxy()
    proxy._session = object()  # type: ignore[assignment]
    monkeypatch.setattr(hls_rewriter, "safe_get", safe_get)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            proxy._forward_url(  # type: ignore[arg-type]
                Request(), "https://slow.example/video", "媒体"
            )
        )

    assert response.closed


def test_cancelled_stream_prepare_closes_upstream_response(monkeypatch) -> None:
    class Content:
        async def read(self, _size: int) -> bytes:
            return b"video"

    class Response:
        status = 200
        headers: dict[str, str] = {}
        content = Content()
        closed = False

        def close(self) -> None:
            self.closed = True

    class Request:
        headers: dict[str, str] = {}

    class Stream:
        def __init__(self, *, status: int) -> None:
            self.status = status
            self.headers: dict[str, str] = {}
            self.content_type = ""

        async def prepare(self, _request) -> None:  # noqa: ANN001
            raise asyncio.CancelledError

    response = Response()

    async def safe_get(_session, _url, **_kwargs):  # noqa: ANN001, ANN003
        return response

    proxy = hls_rewriter.DirectProxy()
    proxy._session = object()  # type: ignore[assignment]
    monkeypatch.setattr(hls_rewriter, "safe_get", safe_get)
    monkeypatch.setattr(hls_rewriter.web, "StreamResponse", Stream)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            proxy._forward_url(  # type: ignore[arg-type]
                Request(), "https://public.example/video", "媒体"
            )
        )

    assert response.closed


def test_streaming_timeout_closes_upstream_response(monkeypatch) -> None:
    class Content:
        async def read(self, _size: int) -> bytes:
            return b"video"

        async def iter_chunked(self, _size: int):  # noqa: ANN202
            raise asyncio.TimeoutError
            yield b"unreachable"

    class Response:
        status = 200
        headers: dict[str, str] = {}
        content = Content()
        closed = False

        def close(self) -> None:
            self.closed = True

    class Request:
        headers: dict[str, str] = {}

    class Stream:
        def __init__(self, *, status: int) -> None:
            self.status = status
            self.headers: dict[str, str] = {}
            self.content_type = ""
            self.written: list[bytes] = []
            self.eof_written = False

        async def prepare(self, _request) -> None:  # noqa: ANN001
            return None

        async def write(self, data: bytes) -> None:
            self.written.append(data)

        async def write_eof(self) -> None:
            self.eof_written = True

    response = Response()

    async def safe_get(_session, _url, **_kwargs):  # noqa: ANN001, ANN003
        return response

    proxy = hls_rewriter.DirectProxy()
    proxy._session = object()  # type: ignore[assignment]
    monkeypatch.setattr(hls_rewriter, "safe_get", safe_get)
    monkeypatch.setattr(hls_rewriter.web, "StreamResponse", Stream)

    result = asyncio.run(
        proxy._forward_url(  # type: ignore[arg-type]
            Request(), "https://slow.example/video", "媒体"
        )
    )

    assert result.written == [b"video"]
    assert result.eof_written
    assert response.closed
