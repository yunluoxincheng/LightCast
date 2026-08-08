# 开发文档

轻投（LightCast）的技术架构、踩坑记录与开发要点。普通用户请阅读 [README](../README.md)。

---

## 一、项目结构

```
LightCast/
├── main.py                     # 入口（注意导入顺序：PySide6 → qasync → ydlna.player）
├── requirements.txt
├── LightCast.spec              # PyInstaller 打包配置
├── i18n/zh.json, en.json       # 中英文案
├── assets/                     # 应用图标（icon.png / icon.ico）
├── packaging/installer.iss     # Inno Setup 安装脚本
├── tools/make_icon.py          # 图标生成脚本
└── ydlna/
    ├── app.py                  # 应用编排：组装组件、启动 DLNA、桥接投屏事件
    ├── config.py               # 配置持久化（config.json，单例）
    ├── autostart.py            # 开机自启（HKCU Run 注册表）
    ├── i18n.py                 # 翻译函数 tr() + 运行时语言切换
    ├── logger.py               # 日志（控制台 + 轮转文件，含 async_upnp_client 流量日志）
    ├── constants.py            # 常量、frozen 模式路径重定向（%APPDATA%\LightCast）
    ├── dlna/                   # DLNA 协议层（与 UI 解耦）
    │   ├── avtransport.py      # AVTransport（SetAVTransportURI/Play/Seek/进度回传）
    │   ├── rendering_control.py# RenderingControl（音量/静音）
    │   ├── connection_manager.py
    │   ├── device.py           # MediaRenderer 设备声明
    │   ├── renderer_bridge.py  # 协议层 ↔ 播放器桥接 + 投屏事件回调
    │   ├── server.py           # UpnpServer 启停封装（HTTP/SOAP/GENA）
    │   ├── ssdp_listener.py    # 线程化原生 SSDP 监听（Windows 多播发现）
    │   └── _net.py             # 网卡枚举 / 子网判断
    ├── player/                 # 播放器层
    │   ├── mpv_player.py       # python-mpv 封装 + Qt 信号桥（含缓冲状态）
    │   ├── mpv_widget.py       # 嵌入 libmpv 的 QWidget（原生窗口句柄）
    │   ├── hls_rewriter.py     # 流媒体代理层（HLS 重写 + 直链代理）
    │   ├── player_interface.py # 播放器页面（渲染区 + 缓冲动画 + 空状态）
    │   ├── control_bar.py      # 悬浮控制栏（独立顶层窗口，媒体门控）
    │   └── player_window.py    # 独立播放窗口（可选）
    └── ui/                     # UI 层
        ├── main_window.py      # MSFluentWindow 框架（16:9 默认窗口、全屏、托盘提示）
        ├── home_interface.py   # 主页（状态 / 设备信息 / 投屏引导）
        ├── settings_interface.py # 设置页（滚动区域布局）
        ├── media_controls.py   # Fluent 风格播放控制条
        ├── tray.py             # 系统托盘
        └── widgets.py          # 状态指示灯等小组件
```

## 二、技术要点（踩坑记录）

### 2.1 必须用 `qasync`，不能只用 `asyncio`

DLNA 服务（async-upnp-client）是 asyncio 库，mpv 的播放事件回调在独立线程，UI 是 Qt 主线程。三者必须共享同一个事件循环。

`qasync` 提供的 `QEventLoop` 把 asyncio loop 嫁接到 Qt 事件循环上。**入口必须用 `qasync.run()` 或 `QEventLoop`**，不能用标准 `asyncio.run()`——否则 mpv 事件线程 emit 的 Qt 信号无法跨线程投递到主线程（信号到达不了槽，UI 不刷新）。参考 `main.py` 的 `_bootstrap()`。

### 2.2 导入顺序：先 PySide6，后 libmpv（Windows 专属坑）

Windows 上，`import mpv` 会通过 `ctypes` 加载 `libmpv-2.dll`（110 MB 的巨型 dll）。**如果先加载 libmpv 再导入 PySide6，会触发 `ImportError: DLL load failed while importing QtWidgets`**——libmpv 的依赖 dll 会污染进程的 DLL 解析表，干扰 Qt6 的延迟加载 dll。

**正确顺序**（见 `main.py`）：

```python
from PySide6.QtWidgets import QApplication   # 1. 先完全加载 PySide6
import qasync                                # 2. qasync
from ydlna.player.mpv_player import ...      # 3. 最后加载 libmpv
```

`ydlna/player/mpv_player.py` 在 import 时临时把 `bin/` 加入 `PATH`（**不**用 `os.add_dll_directory`，那个会持久影响 Qt 的 dll 搜索），让 `ctypes.util.find_library` 找到 libmpv，import 完后恢复 PATH。

### 2.3 DLNA service 文件不能用 `from __future__ import annotations`

`async_upnp_client.server` 的 `@callable_action` 装饰器在运行时比较方法参数的**类型注解对象**（如 `<class 'int'>`）与状态变量声明的 `data_type`。PEP 563（`from __future__ import annotations`）会把注解变成字符串（`'int'`），导致 `assert state_var.data_type_mapping["type"] == annotation` 失败。所以 `avtransport.py` / `rendering_control.py` / `connection_manager.py` **不能**用 future annotations。

### 2.4 `device.services` 是 dict 不是 list

`async_upnp_client` 的 `UpnpDevice.services` 是 `dict[service_type_URN, UpnpService]`。遍历找 service 时要遍历 `.values()`。见 `server.py` 的 `_find_service`。

### 2.5 mpv 回调线程安全

`python-mpv` 的 `property_observer` / `event_callback` 在 `MPVEventHandlerThread` 线程触发。直接 emit Qt 信号是安全的（跨线程 queued，由 qasync loop 处理），但**不要在回调里直接操作 Qt 控件**。所有 UI 更新通过 `PlayerSignals` 的信号 marshal 回主线程。

### 2.6 SSDP 设备发现：不能用 asyncio 的 UDP 多播

Windows 的 IOCP（`ProactorEventLoop`）对**多播 UDP 接收**有长期缺陷——socket 建好、membership 也加了，就是收不到手机发的 M-SEARCH。**解决方案**（`ssdp_listener.py`）：独立线程 + 原生阻塞 socket（照 Macast 验证过的模式），按网卡逐个加入多播组，收到 M-SEARCH 后选「与请求方同子网」的网卡 IP 拼 LOCATION 单播回复，并周期性主动广播 `ssdp:alive` 加速发现。HTTP/SOAP/GENA 部分仍用 async_upnp_client（可靠）。

### 2.7 模态对话框会卡死应用（打包版）

qfluentwidgets 的 `MessageBox.exec()`（嵌套事件循环 + acrylic 特效）在打包版中点击按钮会冻结应用。**用 Qt 原生 `QMessageBox` + 非阻塞 `open()` + `finished` 回调**替代，从架构上杜绝嵌套事件循环。见 `main_window.py` 的 `_ask_minimize_to_tray`。

### 2.8 设置页会撑大窗口最小高度

设置页内容纵向堆叠（约 1000px）会撑大窗口 `minimumSizeHint`，导致窗口无法缩小。**内容包进 `ScrollArea` 滚动区**，窗口最小高度即解放。改窗口默认尺寸时记得升 `window_geometry_vN` 配置标记，否则旧几何会覆盖新默认。

### 2.9 流媒体代理层（`hls_rewriter.py`）

投屏源五花八门，代理层在交给播放器前修好各种「不按规范来」的流：

| 场景 | 处理 |
|---|---|
| 分片 `.jpg` 等非标准扩展名（新版 ffmpeg 白名单拒绝） | 分片改名 `.mp4/.ts/.avi` 再提供 |
| AES-128 加密 HLS | 密钥 URI 重写到本地代理并转发（16 字节校验） |
| fMP4 初始化段（`#EXT-X-MAP`） | 重写到本地端点 |
| 漫画/图文番（分片是真实图片） | 图片惰性转 MJPEG/AVI |
| 「PNG 封面 + TS 视频」混合分片 | 探测并剥离封面（4 连续 0x47 校验），带预取 |
| 防盗链 403 | 自动附加 Referer + 浏览器 UA，复用 cookie，5xx 自动重试 |
| 302 重定向 / 主播放列表 | 按最终地址解析；跟进第一个变体 |
| 直播流（无 ENDLIST） | 不静态代理，交 mpv 原生播放 |
| 非 m3u8 直链 | DirectProxy：防盗链 + 重试 + 内容模式兼容 |
| 源站返回 HTML 错误页（伪装 200） | 识别并返回 502，给用户友好提示 |

## 三、打包

### 1. 构建 exe（PyInstaller）

```bash
pip install pyinstaller
pyinstaller LightCast.spec --noconfirm
```

产物：`dist/LightCast/LightCast.exe`（onedir 模式，约 400MB）。要点：

- `bin/libmpv-2.dll` 会随包打入 `_internal/bin/`，运行时自动注入 PATH
- 安装版把配置/日志写入 `%APPDATA%\LightCast`，不污染安装目录
- spec 排除了 PyQt5/PyQt6（多 Qt 绑定冲突）和整套 ML 栈（numpy/scipy 是 qfluentwidgets 运行时依赖，会保留；torch/transformers 等与本应用无关）
- 打包前请退出正在运行的轻投（exe 被占用会导致构建失败）

### 2. 制作安装程序（Inno Setup 6）

```bash
ISCC.exe /DMyAppVersion=1.2.0 packaging\installer.iss
```

产物：`dist/LightCast-Setup-<版本>.exe`（安装向导、开始菜单/桌面快捷方式、卸载程序、中英双语）。

### 3. 发布（GitHub Actions）

推送 `v*` 标签即自动构建并发布 Release（安装版 + 便携版），Release 说明自动取自 [`CHANGELOG.md`](../CHANGELOG.md) 对应版本的小节。手动触发（workflow_dispatch）只出包不发布，产物挂在 run 的 Artifacts。

### 4. 自动更新（`ydlna/updater.py`）

- 启动时（延迟 4s）查 GitHub latest release，`__version__` 低于最新标签则提示更新（默认开启，`config.auto_update` 控制）
- 下载用 aiohttp 流式分块（不阻塞事件循环），弹窗全部是 Qt 原生 QMessageBox + 非阻塞 `open()` + 信号转 future 等待——**不要**改成 `exec()`（打包版会卡死）
- 安装包下载到 `%APPDATA%\LightCast\updates\`，安装前先退出应用（释放文件占用）
