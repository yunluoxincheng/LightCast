"""GitHub Release 自动更新：检查、下载、安装流程。

设计
----
- 纯 asyncio（复用应用的 qasync 事件循环）：检查/下载都流式进行，
  不阻塞 UI；下载期间按 256KB 分块写盘并回调进度。
- 弹窗一律用 Qt 原生 QMessageBox + 非阻塞 open() + 信号等待
  （qfluentwidgets 模态 exec() 在打包版会卡死应用，见 docs/DEVELOPMENT.md）。
- 检查失败静默（记日志），绝不打扰用户。
"""
from __future__ import annotations

import asyncio
import html as _html
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import aiohttp

from . import __version__
from .logger import get_logger

log = get_logger("updater")

# 项目主页（与设置页一致）
GITHUB_URL = "https://github.com/yunluoxincheng/LightCast"
# GitHub API：最新 Release
RELEASES_API = GITHUB_URL.replace("github.com/", "api.github.com/repos/") + "/releases/latest"

_UA = f"LightCast/{__version__}"
_TIMEOUT = aiohttp.ClientTimeout(total=15)


@dataclass
class UpdateInfo:
    """最新 Release 的信息。"""

    version: str          # 如 "0.1.2"（不含 v）
    tag: str              # 如 "v0.1.2"
    notes: str            # Release 说明
    setup_url: str        # 安装版（Windows .exe）
    portable_url: str     # 便携版 zip
    published_at: str


def parse_version(text: str) -> tuple[int, int, int]:
    """解析 "v0.1.2" / "0.1.2" → (0, 1, 2)；无法解析返回 (0,0,0)。"""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", text or "")
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def is_newer(remote: str, current: str) -> bool:
    return parse_version(remote) > parse_version(current)


async def check_for_update() -> Optional[UpdateInfo]:
    """查 GitHub 最新 Release。

    - 无新版本 → None
    - 有更新 → UpdateInfo
    - 网络/API 异常 → 抛异常（调用方决定是否提示）
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(
            RELEASES_API, timeout=_TIMEOUT, headers={"User-Agent": _UA}
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"GitHub API 返回 HTTP {resp.status}")
            data = await resp.json()
    tag = data.get("tag_name", "") or ""
    version = tag.lstrip("v")
    if not is_newer(version, __version__):
        return None
    assets = {
        a.get("name", ""): a.get("browser_download_url", "")
        for a in data.get("assets", [])
    }
    setup_url = next((u for n, u in assets.items() if n.endswith(".exe")), "")
    portable_url = next((u for n, u in assets.items() if n.endswith(".zip")), "")
    if not setup_url:
        raise RuntimeError("最新 Release 没有安装包资产")
    return UpdateInfo(
        version=version,
        tag=tag,
        notes=(data.get("body") or "").strip(),
        setup_url=setup_url,
        portable_url=portable_url,
        published_at=data.get("published_at", "") or "",
    )


def _md_to_html(text: str) -> str:
    """把更新说明（markdown 子集：## / ### 标题、- 列表）转成 HTML。

    发布说明来自 CHANGELOG.md 的小节，格式固定，做轻量转换即可，
    交给 QMessageBox 富文本渲染。
    """
    out: list[str] = []
    in_list = False
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if s.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h4>{_html.escape(s[4:])}</h4>")
        elif s.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{_html.escape(s[3:])}</h3>")
        elif s.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_html.escape(s[2:])}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{_html.escape(s)}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def download_dir() -> Path:
    """更新包下载目录（APPDATA/LightCast/updates）。"""
    from .constants import CONFIG_PATH
    d = Path(CONFIG_PATH).parent / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def download_update(
    url: str,
    dest: Path,
    on_progress: Optional[Callable[[int, int], None]] = None,
    parts: int = 4,
) -> Path:
    """下载更新包。

    - 默认 4 段 Range 并发下载（多线程效果，速度显著提升）；
      服务器不支持 Range / 大小未知时自动回退单线程流式下载
    - 分块写盘，不阻塞事件循环；on_progress(done, total) 在主线程协程内回调
    """
    timeout = aiohttp.ClientTimeout(total=None, connect=15)

    # 探测：Range 支持 + 总大小（GET bytes=0-0，206 + Content-Range）
    total = 0
    ranges_ok = False
    async with aiohttp.ClientSession() as probe_session:
        async with probe_session.get(
            url, headers={"Range": "bytes=0-0"}, timeout=timeout
        ) as probe:
            if probe.status == 206:
                m = re.search(r"/\s*(\d+)\s*$", probe.headers.get("Content-Range", ""))
                if m:
                    total = int(m.group(1))
                    ranges_ok = True
    if not ranges_ok:
        # 单线程流式（原有逻辑）
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"下载失败: HTTP {resp.status}")
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        if on_progress:
                            on_progress(done, total)
        return dest

    # 分段并发
    n = max(1, min(parts, total // (256 * 1024)))  # 每段至少 256KB，避免段过碎
    if n <= 1:
        n = 1
    ranges = []
    step = total // n
    for i in range(n):
        start = i * step
        end = total - 1 if i == n - 1 else (i + 1) * step - 1
        ranges.append((start, end))
    done = [0] * n
    part_paths = [Path(str(dest) + f".part{i}") for i in range(n)]

    async def fetch(i: int, start: int, end: int) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"Range": f"bytes={start}-{end}"},
                timeout=timeout,
            ) as resp:
                if resp.status != 206:
                    raise RuntimeError(f"分段 {i} 下载失败: HTTP {resp.status}")
                with open(part_paths[i], "wb") as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                        done[i] += len(chunk)
                        if on_progress:
                            on_progress(sum(done), total)

    try:
        await asyncio.gather(*(fetch(i, s, e) for i, (s, e) in enumerate(ranges)))
    except Exception:
        # 失败清理半成品
        for p in part_paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    # 按序拼接
    with open(dest, "wb") as out:
        for p in part_paths:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, 1024 * 1024)
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    return dest


# --------------------------------------------------------------------------- #
# 交互流程（非阻塞弹窗，杜绝嵌套事件循环）
# --------------------------------------------------------------------------- #
async def _await_signal(signal, timeout: float = 0) -> None:
    """等待 Qt 信号（把信号转成 asyncio future）。已触发则立即返回。"""
    loop = asyncio.get_event_loop()
    fut: "asyncio.Future" = loop.create_future()
    signal.connect(lambda *_a: None if fut.done() else fut.set_result(None))
    if timeout > 0:
        await asyncio.wait_for(fut, timeout)
    else:
        await fut


async def run_update_flow(parent, info: UpdateInfo) -> bool:
    """完整更新流程：提示 → 下载（带进度）→ 安装提示 → 启动安装程序。

    返回 True 表示已启动安装程序（应用即将退出）；False 表示用户取消或失败。
    """
    from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QVBoxLayout
    from qfluentwidgets import InfoBar, InfoBarPosition, ProgressBar

    from .i18n import tr

    # 1. 发现新版本提示（非阻塞；说明按 markdown 渲染成富文本）
    from PySide6.QtCore import Qt
    box = QMessageBox(parent)
    box.setWindowTitle(tr("dialog.update.available.title"))
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(tr("dialog.update.available.body").format(version=info.version))
    if info.notes:
        box.setInformativeText(_md_to_html(info.notes))
    dl_btn = box.addButton(tr("dialog.update.download"), QMessageBox.ButtonRole.AcceptRole)
    box.addButton(tr("dialog.update.later"), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(dl_btn)
    box.open()
    await _await_signal(box.finished)
    box.deleteLater()
    if box.clickedButton() is not dl_btn:
        return False

    # 2. 下载（非模态进度窗，进度条由协程内回调刷新）
    dlg = QDialog(parent)
    dlg.setWindowTitle(tr("dialog.update.downloading"))
    dlg.setMinimumWidth(360)
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(tr("dialog.update.downloading"), dlg))
    bar = ProgressBar(dlg)
    bar.setRange(0, 1000)
    lay.addWidget(bar)
    dlg.open()

    def _progress(done: int, total: int) -> None:
        bar.setValue(int(done * 1000 / total) if total else 0)

    dest = download_dir() / f"LightCast-Setup-{info.version}.exe"
    try:
        await download_update(info.setup_url, dest, _progress)
    except Exception as e:  # noqa: BLE001
        dlg.close()
        dlg.deleteLater()
        log.warning("下载更新失败: %s", e)
        InfoBar.error(
            title=tr("dialog.update.failed"),
            content=str(e),
            orient=0,  # Qt.Horizontal
            isClosable=True,
            duration=8000,
            parent=parent if parent is not None else None,
            position=InfoBarPosition.TOP,
        )
        return False
    dlg.close()
    dlg.deleteLater()

    # 3. 安装提示
    size_mb = max(1, round(dest.stat().st_size / (1024 * 1024)))
    box2 = QMessageBox(parent)
    box2.setWindowTitle(tr("dialog.update.available.title"))
    box2.setText(tr("dialog.update.downloaded").format(size=size_mb))
    install_btn = box2.addButton(
        tr("dialog.update.install"), QMessageBox.ButtonRole.AcceptRole
    )
    box2.addButton(tr("dialog.update.later"), QMessageBox.ButtonRole.RejectRole)
    box2.setDefaultButton(install_btn)
    box2.open()
    await _await_signal(box2.finished)
    box2.deleteLater()
    if box2.clickedButton() is not install_btn:
        return False

    # 4. 启动安装程序并退出（安装版会替换程序文件，必须先释放占用）
    log.info("启动安装程序: %s", dest)
    os.startfile(str(dest))  # type: ignore[attr-defined]  # Windows only
    QApplication.instance().quit()
    return True
