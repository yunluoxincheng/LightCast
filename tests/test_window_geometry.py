from __future__ import annotations

from ydlna.config import DEFAULTS
from ydlna.ui.main_window import (
    DEFAULT_WINDOW_SIZE,
    WINDOW_GEOMETRY_VERSION_KEY,
    _geometry_to_restore,
)


class _Config:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.set_calls: list[tuple[str, object, bool]] = []

    def get(self, key: str, default=None):  # noqa: ANN001, ANN202
        return self.data.get(key, default)

    def set(self, key: str, value, *, persist: bool = True) -> None:  # noqa: ANN001
        self.data[key] = value
        self.set_calls.append((key, value, persist))


def test_window_v7_uses_exact_1200x800_and_discards_all_legacy_geometry() -> None:
    config = _Config(
        {
            WINDOW_GEOMETRY_VERSION_KEY: False,
            # 迁移范围包含旧默认尺寸和用户自定义尺寸；旧版 v1-v6 统一退场。
            "window_geometry": [100, 100, 1300, 900],
        }
    )

    assert DEFAULT_WINDOW_SIZE == (1200, 800)
    assert all(f"window_geometry_v{version}" not in DEFAULTS for version in range(1, 7))
    assert DEFAULTS[WINDOW_GEOMETRY_VERSION_KEY] is False
    assert _geometry_to_restore(config) is None  # type: ignore[arg-type]
    assert config.data["window_geometry"] is None
    assert config.data[WINDOW_GEOMETRY_VERSION_KEY] is True
    assert config.set_calls == [
        ("window_geometry", None, False),
        (WINDOW_GEOMETRY_VERSION_KEY, True, True),
    ]


def test_window_v7_restores_geometry_saved_after_migration() -> None:
    config = _Config(
        {
            WINDOW_GEOMETRY_VERSION_KEY: True,
            "window_geometry": [20, 30, 1280, 850],
        }
    )

    assert _geometry_to_restore(config) == (20, 30, 1280, 850)  # type: ignore[arg-type]
    assert config.set_calls == []
