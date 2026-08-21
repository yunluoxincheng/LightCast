from __future__ import annotations

import os
import subprocess
import sys
import textwrap

from ydlna.config import DEFAULTS
from ydlna.ui.main_window import (
    DEFAULT_WINDOW_SIZE,
    WINDOW_GEOMETRY_VERSION_KEY,
    _geometry_to_restore,
    _is_geometry_visible,
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


def test_window_v8_uses_exact_1200x800_and_discards_collapsed_geometry() -> None:
    config = _Config(
        {
            WINDOW_GEOMETRY_VERSION_KEY: False,
            # 开机自启首次显示被 Qt 布局压到 minimumSize 后保存的外框尺寸。
            "window_geometry": [586, 364, 914, 614],
        }
    )

    assert DEFAULT_WINDOW_SIZE == (1200, 800)
    assert all(f"window_geometry_v{version}" not in DEFAULTS for version in range(1, 8))
    assert DEFAULTS[WINDOW_GEOMETRY_VERSION_KEY] is False
    assert _geometry_to_restore(config) is None  # type: ignore[arg-type]
    assert config.data["window_geometry"] is None
    assert config.data[WINDOW_GEOMETRY_VERSION_KEY] is True
    assert config.set_calls == [
        ("window_geometry", None, False),
        (WINDOW_GEOMETRY_VERSION_KEY, True, True),
    ]


def test_window_v8_preserves_custom_geometry_during_migration() -> None:
    config = _Config(
        {
            WINDOW_GEOMETRY_VERSION_KEY: False,
            "window_geometry": [100, 100, 1300, 900],
        }
    )

    assert _geometry_to_restore(config) == (100, 100, 1300, 900)  # type: ignore[arg-type]
    assert config.data["window_geometry"] == [100, 100, 1300, 900]
    assert config.set_calls == [(WINDOW_GEOMETRY_VERSION_KEY, True, True)]


def test_window_v8_restores_geometry_saved_after_migration() -> None:
    config = _Config(
        {
            WINDOW_GEOMETRY_VERSION_KEY: True,
            "window_geometry": [20, 30, 1280, 850],
        }
    )

    assert _geometry_to_restore(config) == (20, 30, 1280, 850)  # type: ignore[arg-type]
    assert config.set_calls == []


def test_geometry_visibility_accepts_window_on_any_connected_screen() -> None:
    screens = [(0, 0, 1920, 1040), (1920, -200, 2560, 1400)]

    assert _is_geometry_visible((2100, 100, 1200, 800), screens, 138) is True
    assert _is_geometry_visible((-2000, 100, 1200, 800), screens, 138) is False


def test_geometry_visibility_accepts_visible_top_left_title_bar_area() -> None:
    screens = [(0, 0, 1920, 1040)]

    assert _is_geometry_visible((1840, 992, 1200, 800), screens, 138) is True


def test_geometry_visibility_rejects_bottom_right_sliver_without_title_bar() -> None:
    screens = [(0, 0, 1920, 1040)]

    # 整个窗口仍有 80×48 可见，但露出的是右下角，顶部拖动区完全离屏。
    assert _is_geometry_visible((-1120, -752, 1200, 800), screens, 138) is False
    # 右侧只剩 50px 标题栏也不足以可靠拖回。
    assert _is_geometry_visible((1870, 100, 1200, 800), screens, 138) is False


def test_geometry_visibility_rejects_right_side_title_bar_buttons_only() -> None:
    screens = [(0, 0, 1920, 1040)]

    # 屏幕中只剩窗口最右侧 80px；它完全位于 3×46px 控制按钮区内。
    assert _is_geometry_visible((-1120, 100, 1200, 800), screens, 138) is False


def test_geometry_restore_discards_position_left_on_disconnected_monitor() -> None:
    config = _Config(
        {
            WINDOW_GEOMETRY_VERSION_KEY: True,
            "window_geometry": [2200, 100, 1200, 800],
        }
    )

    assert _geometry_to_restore(config, [(0, 0, 1920, 1040)], 138) is None  # type: ignore[arg-type]
    assert config.data["window_geometry"] is None
    assert config.set_calls == [("window_geometry", None, True)]


def test_geometry_restore_keeps_position_on_secondary_monitor() -> None:
    config = _Config(
        {
            WINDOW_GEOMETRY_VERSION_KEY: True,
            "window_geometry": [2200, 100, 1200, 800],
        }
    )
    screens = [(0, 0, 1920, 1040), (1920, 0, 2560, 1400)]

    assert _geometry_to_restore(config, screens, 138) == (2200, 100, 1200, 800)  # type: ignore[arg-type]
    assert config.set_calls == []


def test_hidden_window_first_show_reapplies_exact_default_geometry() -> None:
    """用真实 QWidget Show/polish/timer 生命周期锁定开机自启修复。"""
    script = textwrap.dedent(
        """
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QWidget

        from ydlna.ui.main_window import DEFAULT_WINDOW_SIZE, _StartupGeometryGuard


        app = QApplication([])
        window = QWidget()
        window.setMinimumSize(900, 600)
        window.resize(914, 614)
        guard = _StartupGeometryGuard(window, None)
        assert not guard.can_save

        window.show()
        QTimer.singleShot(20, app.quit)
        app.exec()

        assert guard.can_save
        assert (window.width(), window.height()) == DEFAULT_WINDOW_SIZE
        """
    )
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
