from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent

from ydlna.player.player_interface import _MediaShortcutFilter


class _ShortcutHandler(QObject):
    def __init__(self, *, visible: bool) -> None:
        super().__init__()
        self.visible = visible
        self.calls: list[str] = []
        self._shortcut_actions = {
            Qt.Key.Key_Space: lambda: self.calls.append("play_pause"),
        }

    def isVisible(self) -> bool:  # noqa: N802
        return self.visible


def _space_key_event() -> QKeyEvent:
    return QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Space,
        Qt.KeyboardModifier.NoModifier,
    )


def test_media_shortcut_is_ignored_when_player_page_is_hidden() -> None:
    handler = _ShortcutHandler(visible=False)
    shortcut_filter = _MediaShortcutFilter(handler)  # type: ignore[arg-type]

    consumed = shortcut_filter.eventFilter(None, _space_key_event())

    assert consumed is False
    assert handler.calls == []


def test_media_shortcut_is_consumed_when_player_page_is_visible() -> None:
    handler = _ShortcutHandler(visible=True)
    shortcut_filter = _MediaShortcutFilter(handler)  # type: ignore[arg-type]

    consumed = shortcut_filter.eventFilter(None, _space_key_event())

    assert consumed is True
    assert handler.calls == ["play_pause"]
