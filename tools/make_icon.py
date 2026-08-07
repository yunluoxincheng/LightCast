"""生成轻投（LightCast）应用图标。

设计：Win11 Fluent 风格圆角方块 + 蓝色渐变背景 + 白色「投屏」图形
（设备圆点 + 三道 Wi-Fi 波纹）。

输出：
- assets/icon.png   512x512 主图
- assets/icon.ico   多尺寸（16/24/32/48/64/128/256，PNG 压缩条目）

重新生成：python tools/make_icon.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
)

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
MASTER_SIZE = 512

WHITE = QColor("#ffffff")


def draw_icon(size: int) -> QImage:
    """绘制单个尺寸的图标（ARGB32）。"""
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = float(size)

    # ---- 背景：圆角方块 + 蓝色渐变 ----
    margin = s * 0.045
    radius = s * 0.22
    grad = QLinearGradient(0, 0, 0, s)
    grad.setColorAt(0.0, QColor("#57c8ff"))
    grad.setColorAt(0.5, QColor("#1a97e8"))
    grad.setColorAt(1.0, QColor("#0e63c0"))
    p.setBrush(grad)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(margin, margin, s - 2 * margin, s - 2 * margin),
                      radius, radius)

    # ---- 顶部高光（左上柔光） ----
    glow = QRadialGradient(QPointF(s * 0.32, s * 0.28), s * 0.72)
    glow.setColorAt(0.0, QColor(255, 255, 255, 46))
    glow.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(glow)
    p.drawRoundedRect(QRectF(margin, margin, s - 2 * margin, s - 2 * margin),
                      radius, radius)

    # ---- 投屏图形：设备圆点 + 三道波纹 ----
    pen_w = s * 0.045
    dot_cx, dot_cy = s * 0.5, s * 0.74
    dot_r = s * 0.05
    p.setBrush(WHITE)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(dot_cx, dot_cy), dot_r, dot_r)

    p.setPen(QPen(WHITE, pen_w, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap))
    # 三道以圆点为圆心的上弧（0° 在 3 点钟方向，正角度逆时针）
    for radius_frac in (0.10, 0.165, 0.235):
        r = s * radius_frac
        p.drawArc(QRectF(dot_cx - r, dot_cy - r, 2 * r, 2 * r),
                  180 * 16, 180 * 16)

    p.end()
    return img


def write_ico(path: Path, images: list[QImage]) -> None:
    """把多张 PNG 以条目形式写入 .ico（Vista+ 支持 PNG 压缩条目）。"""
    blobs: list[bytes] = []
    for img in images:
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if not img.save(buf, "PNG"):
            raise RuntimeError(f"PNG 编码失败（{img.width()}x{img.height()}）")
        blobs.append(bytes(ba))

    count = len(blobs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    out = bytearray(header)
    for img, blob in zip(images, blobs):
        w = img.width()
        out += struct.pack(
            "<BBBBHHII",
            0 if w >= 256 else w,  # 0 表示 256
            0 if w >= 256 else w,
            0,   # 调色板大小
            0,   # 保留
            1,   # 颜色平面数
            32,  # 位深
            len(blob),
            offset,
        )
        offset += len(blob)
    for blob in blobs:
        out += blob
    path.write_bytes(bytes(out))


def main() -> int:
    ASSETS.mkdir(exist_ok=True)

    master = draw_icon(MASTER_SIZE)
    master.save(str(ASSETS / "icon.png"), "PNG")
    print(f"已生成 assets/icon.png ({MASTER_SIZE}x{MASTER_SIZE})")

    images = [draw_icon(sz) for sz in ICO_SIZES]
    write_ico(ASSETS / "icon.ico", images)
    sizes = ", ".join(str(i.width()) for i in images)
    print(f"已生成 assets/icon.ico（尺寸: {sizes}）")

    # 校验：QIcon 能正常加载（QIcon 构造需要 QGuiApplication 实例）
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    icon = QIcon(str(ASSETS / "icon.ico"))
    if icon.isNull():
        print("警告: QIcon 加载 icon.ico 失败", file=sys.stderr)
        return 1
    print("QIcon 校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
