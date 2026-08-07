"""设置页：语言、主题、设备名、DLNA 开关、端口、关于。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QGridLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    ComboBox,
    FluentIcon as FIF,
    LineEdit,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    TitleLabel,
)

from ..config import Config
from ..constants import APP_NAME, APP_VERSION
from ..i18n import tr, Translator
from ..logger import get_logger

if TYPE_CHECKING:
    pass

log = get_logger("ui.settings")

GITHUB_URL = "https://github.com/xfangfang/Macast"  # 参考实现；按需替换为项目主页


class _SettingCard(CardWidget):
    """单行设置卡片：标题 + 说明 + 控件。"""

    def __init__(self, title: str, desc: str = "", parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setFixedHeight(76)
        lay = QGridLayout(self)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setHorizontalSpacing(16)

        self.titleLabel = StrongBodyLabel(title)
        self.descLabel = BodyLabel(desc)
        self.descLabel.setEnabled(False)
        lay.addWidget(self.titleLabel, 0, 0)
        lay.addWidget(self.descLabel, 1, 0)
        # 控件容器（外部 setWidget 放入）
        self._widgetCol = QWidget()
        self._widgetCol.setLayout(QVBoxLayout())
        self._widgetCol.layout().setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._widgetCol, 0, 1, 2, 1, Qt.AlignRight | Qt.AlignVCenter)
        lay.setColumnStretch(0, 1)

    def setWidget(self, w: QWidget) -> None:  # noqa: ANN001
        self._widgetCol.layout().addWidget(w)


class SettingsInterface(QWidget):
    """设置页。"""

    # 部分设置需要重启生效
    restartRequested = Signal()

    def __init__(self, config: Config, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setObjectName("settings-interface")
        self._config = config
        self._build_ui()
        self._load_values()
        self._connect()
        self._retranslate()
        Translator.instance().languageChanged.connect(self._retranslate)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 24, 36, 24)
        root.setSpacing(14)

        self.titleLabel = TitleLabel(tr("settings.title"))

        # ---- 通用 ----
        self.generalTitle = SubtitleLabel(tr("settings.group.general"))

        self.languageCard = _SettingCard(tr("settings.language"))
        self.languageCombo = ComboBox()
        self.languageCombo.addItems(["简体中文", "English"])
        self.languageCard.setWidget(self.languageCombo)

        self.themeCard = _SettingCard(tr("settings.theme"))
        self.themeCombo = ComboBox()
        self.themeCombo.addItems([tr("settings.theme.light"), tr("settings.theme.dark"), tr("settings.theme.auto")])
        self.themeCard.setWidget(self.themeCombo)

        # ---- 投屏服务 ----
        self.serviceTitle = SubtitleLabel(tr("settings.group.service"))

        self.autostartCard = _SettingCard(tr("settings.service.enabled"), tr("settings.service.enabled.hint"))
        self.autostartSwitch = SwitchButton()
        self.autostartCard.setWidget(self.autostartSwitch)

        self.deviceNameCard = _SettingCard(tr("settings.device_name"), tr("settings.device_name.hint"))
        self.deviceNameEdit = LineEdit()
        self.deviceNameEdit.setFixedWidth(220)
        self.deviceNameCard.setWidget(self.deviceNameEdit)

        self.portCard = _SettingCard(tr("settings.http_port"), tr("settings.http_port.hint"))
        self.portEdit = LineEdit()
        self.portEdit.setFixedWidth(120)
        self.portEdit.setPlaceholderText("0")
        self.portCard.setWidget(self.portEdit)

        # ---- 关于 ----
        self.aboutTitle = SubtitleLabel(tr("settings.group.about"))
        self.aboutCard = CardWidget(self)
        about_lay = QVBoxLayout(self.aboutCard)
        about_lay.setContentsMargins(20, 16, 20, 16)
        about_lay.setSpacing(4)
        self.aboutName = StrongBodyLabel(f"{APP_NAME} v{APP_VERSION}")
        self.aboutDesc = BodyLabel(tr("settings.about.description"))
        self.aboutDesc.setWordWrap(True)
        self.aboutLicense = BodyLabel(tr("settings.about.license"))
        self.aboutLicense.setEnabled(False)
        self.githubButton = PushButton(FIF.LINK, tr("settings.about.github"))
        about_lay.addWidget(self.aboutName)
        about_lay.addWidget(self.aboutDesc)
        about_lay.addWidget(self.aboutLicense)
        about_lay.addWidget(self.githubButton, 0, Qt.AlignLeft)

        root.addWidget(self.titleLabel)
        root.addSpacing(4)
        root.addWidget(self.generalTitle)
        root.addWidget(self.languageCard)
        root.addWidget(self.themeCard)
        root.addSpacing(8)
        root.addWidget(self.serviceTitle)
        root.addWidget(self.autostartCard)
        root.addWidget(self.deviceNameCard)
        root.addWidget(self.portCard)
        root.addSpacing(8)
        root.addWidget(self.aboutTitle)
        root.addWidget(self.aboutCard)
        root.addStretch(1)

    def _load_values(self) -> None:
        lang = self._config.get("language", "zh")
        self.languageCombo.setCurrentIndex(0 if lang == "zh" else 1)
        theme = self._config.get("theme", "auto")
        self.themeCombo.setCurrentIndex({"light": 0, "dark": 1, "auto": 2}.get(theme, 2))
        self.autostartSwitch.setChecked(bool(self._config.get("dlna_enabled", True)))
        self.deviceNameEdit.setText(self._config.get("friendly_name", ""))
        self.portEdit.setText(str(self._config.get("http_port", 0)))

    def _connect(self) -> None:
        self.languageCombo.currentIndexChanged.connect(self._on_language_changed)
        self.themeCombo.currentIndexChanged.connect(self._on_theme_changed)
        self.autostartSwitch.checkedChanged.connect(self._on_autostart_changed)
        self.deviceNameEdit.textChanged.connect(self._on_device_name_changed)
        self.portEdit.textChanged.connect(self._on_port_changed)
        self.githubButton.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GITHUB_URL)))

    # ------------------------------------------------------------------ #
    def _on_language_changed(self, idx: int) -> None:
        code = "zh" if idx == 0 else "en"
        self._config.set("language", code)
        # 立即切换界面语言
        Translator.instance().set_language(code)

    def _on_theme_changed(self, idx: int) -> None:
        from qfluentwidgets import setTheme, Theme
        theme_map = {0: Theme.LIGHT, 1: Theme.DARK, 2: Theme.AUTO}
        code_map = {0: "light", 1: "dark", 2: "auto"}
        setTheme(theme_map[idx])
        self._config.set("theme", code_map[idx])

    def _on_autostart_changed(self, checked: bool) -> None:
        self._config.set("dlna_enabled", checked)

    def _on_device_name_changed(self, text: str) -> None:
        self._config.set("friendly_name", text.strip() or "YDLNA Renderer")

    def _on_port_changed(self, text: str) -> None:
        try:
            port = int(text.strip() or "0")
            self._config.set("http_port", max(0, min(65535, port)))
        except ValueError:
            pass

    # ------------------------------------------------------------------ #
    def _retranslate(self, *_args) -> None:
        self.titleLabel.setText(tr("settings.title"))
        self.generalTitle.setText(tr("settings.group.general"))
        self.languageCard.titleLabel.setText(tr("settings.language"))
        self.themeCard.titleLabel.setText(tr("settings.theme"))
        # 重新填充主题下拉
        self.themeCombo.setItemText(0, tr("settings.theme.light"))
        self.themeCombo.setItemText(1, tr("settings.theme.dark"))
        self.themeCombo.setItemText(2, tr("settings.theme.auto"))
        self.serviceTitle.setText(tr("settings.group.service"))
        self.autostartCard.titleLabel.setText(tr("settings.service.enabled"))
        self.autostartCard.descLabel.setText(tr("settings.service.enabled.hint"))
        self.deviceNameCard.titleLabel.setText(tr("settings.device_name"))
        self.deviceNameCard.descLabel.setText(tr("settings.device_name.hint"))
        self.portCard.titleLabel.setText(tr("settings.http_port"))
        self.portCard.descLabel.setText(tr("settings.http_port.hint"))
        self.aboutTitle.setText(tr("settings.group.about"))
        self.aboutDesc.setText(tr("settings.about.description"))
        self.aboutLicense.setText(tr("settings.about.license"))
        self.githubButton.setText(tr("settings.about.github"))

    def retranslate_ui(self) -> None:
        self._retranslate()
