"""轻量国际化：JSON 字典 + 运行时切换语言。

用法::

    from ydlna.i18n import tr
    label.setText(tr("player.play"))

切换语言时发射 ``languageChanged`` 信号，各界面应连接该信号并实现 ``retranslate_ui()``。
"""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, Signal

from .constants import I18N_DIR
from .logger import get_logger

log = get_logger("i18n")

_SUPPORTED = {"zh": "zh.json", "en": "en.json"}


class Translator(QObject):
    """翻译器单例。"""

    # 语言切换信号
    languageChanged = Signal(str)

    _instance: "Translator | None" = None

    def __init__(self) -> None:
        super().__init__()
        self._lang: str = "zh"
        self._dicts: dict[str, dict[str, str]] = {}
        for code, fname in _SUPPORTED.items():
            self._dicts[code] = self._load(fname)

    @classmethod
    def instance(cls) -> "Translator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def _load(fname: str) -> dict[str, str]:
        path = I18N_DIR / fname
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: str(v) for k, v in data.items()}
        except (OSError, json.JSONDecodeError) as e:
            log.error("加载语言文件 %s 失败: %s", path, e)
        return {}

    @property
    def language(self) -> str:
        return self._lang

    def set_language(self, code: str) -> None:
        if code not in _SUPPORTED or code == self._lang:
            return
        self._lang = code
        log.info("切换语言: %s", code)
        self.languageChanged.emit(code)

    def translate(self, key: str, **kwargs: Any) -> str:
        d = self._dicts.get(self._lang, {})
        text = d.get(key)
        if text is None:
            # 回退到另一语言
            for fallback in self._dicts.values():
                if key in fallback:
                    text = fallback[key]
                    break
        if text is None:
            log.debug("缺少翻译 key: %s", key)
            return key
        try:
            return text.format(**kwargs) if kwargs else text
        except (KeyError, IndexError):
            return text


def tr(key: str, **kwargs: Any) -> str:
    """全局翻译函数。"""
    return Translator.instance().translate(key, **kwargs)


def set_language(code: str) -> None:
    """全局切换语言。"""
    Translator.instance().set_language(code)
