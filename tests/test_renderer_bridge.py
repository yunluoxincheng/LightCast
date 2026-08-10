from __future__ import annotations

import pytest

from ydlna.dlna.renderer_bridge import dlna_time_to_seconds


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("00:00:00", 0.0),
        (" 00:00:00 ", 0.0),
        ("1:02:03.5", 3723.5),
    ],
)
def test_dlna_time_to_seconds_parses_zero_and_valid_times(
    text: str, expected: float
) -> None:
    assert dlna_time_to_seconds(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "   ", "NOT_IMPLEMENTED", " NOT_IMPLEMENTED "])
def test_dlna_time_to_seconds_keeps_unimplemented_sentinel(text: str) -> None:
    assert dlna_time_to_seconds(text) is None
