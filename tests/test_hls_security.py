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
