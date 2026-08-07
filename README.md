# 轻投（LightCast）—— DLNA 投屏接收软件

把你的电脑变成一个 DLNA MediaRenderer（投屏接收端）。从手机（或任意 DLNA 控制点）把视频、音乐、图片投到电脑上，用内置的现代化播放器播放。

- 🎬 内嵌 libmpv 播放器，支持 HTTP/HTTPS 网络流、硬件解码、几乎所有格式（MKV/H.265/...）
- 🎨 Win11 Fluent Design 风格界面（基于 PySide6 + PyQt-Fluent-Widgets）
- 🌐 中英双语运行时切换
- 📡 完整实现 UPnP AVTransport / RenderingControl / ConnectionManager，进度实时回传给手机端
- 🖧 系统托盘常驻，关闭主窗口不退出
- 🪟 跨线程架构清晰：PySide6（UI）+ qasync（事件循环融合）+ async-upnp-client（DLNA 协议）

> 架构参考了 [xfangfang/Macast](https://github.com/xfangfang/Macast)，但 UI 用 Qt + Fluent 组件重写，DLNA 协议改用 async-upnp-client 的服务端实现。

---

## 一、环境要求

- **Windows 10/11**（64 位）
- **Python 3.10 ~ 3.12**（在 3.11.9 上开发测试）
- **libmpv**（`libmpv-2.dll`，见下文获取方式）

> 也可以直接下载打包好的安装包（见下文「十一、打包」）。

---

## 二、安装

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

核心依赖：`PySide6`、`PySide6-Fluent-Widgets`、`qasync`、`python-mpv`、`async-upnp-client`、`python-didl-lite`。

### 2. 获取 libmpv（关键，播放器依赖它）

轻投（LightCast）通过 `python-mpv` 调用 libmpv 渲染视频，**必须手动下载 `libmpv-2.dll` 放到 `bin/` 目录**（dll 体积大，不纳入仓库）。

**步骤：**

1. 访问 mpv libmpv dev 包下载页：
   https://sourceforge.net/projects/mpv-player-windows/files/libmpv/
2. 下载最新的 **`mpv-dev-x86_64-<日期>.7z`**（标准版，**不要**下 `v3` 后缀的精简版，那个不含独立 dll）
3. 用 7-Zip 解压，取出里面的 **`libmpv-2.dll`**（约 110 MB）
4. 把它放到项目的 **`bin/libmpv-2.dll`**：

```
LightCast/
├── bin/
│   └── libmpv-2.dll   ← 放这里
├── main.py
├── ydlna/
└── ...
```

> 文件名必须是 `libmpv-2.dll`、`mpv-2.dll` 或 `mpv-1.dll` 之一（`python-mpv` 的搜索约定）。
>
> 首次启动时若检测不到 dll，轻投会弹窗提示。

---

## 三、运行

```bash
python main.py
```

启动后：

1. 主窗口显示，左侧导航：**主页 / 播放器 / 设置**
2. DLNA 服务默认**自动启动**（可在设置里关闭）
3. 主页显示本机设备名和局域网 IP（确认手机和电脑在同一网络）
4. **确保 Windows 防火墙允许 Python 入站连接**（首次启动会弹窗，选「允许」）

---

## 四、如何投屏

1. 确保手机和电脑连同一个 Wi-Fi。
2. 在手机上打开支持 DLNA 的应用：
   - **通用**：[BubbleUPnP](https://play.google.com/store/apps/details?id=com.bubblesoft.android.bubbleupnp)（推荐，Android）
   - **视频**：VLC、MX Player、系统相册/文件的「投射」
   - **iOS**：建议用第三方 DLNA 应用（系统 AirPlay 不兼容 DLNA）
3. 在应用的设备列表里，选择名为 **`轻投`**（或你在设置里改的名字）的设备。
4. 选择要播放的视频/音乐/图片，电脑会自动开始播放。
5. 在轻投窗口的「播放器」页可以：拖动进度条定位、播放/暂停、停止、调音量。

手机端会实时显示播放进度（轻投每秒回传进度）。

---

## 五、设置说明

进入「设置」页：

| 设置 | 说明 | 是否需要重启 |
|---|---|---|
| 界面语言 | 简体中文 / English，**即时切换** | 否 |
| 主题 | 浅色 / 深色 / 跟随系统，即时生效 | 否 |
| 开机自动启动投屏服务 | 应用启动时是否自动开启 DLNA | 否 |
| 设备名称 | 手机投屏列表里显示的名字 | **是** |
| 服务端口 | HTTP 端口，`0` = 自动分配，一般无需改 | **是** |

需要重启的设置改完后，关闭并重新打开轻投 即可。

---

## 六、项目结构

```
LightCast/
├── main.py                     # 入口（注意导入顺序：PySide6 → qasync → ydlna.player）
├── requirements.txt
├── bin/libmpv-2.dll            # libmpv（用户自行放入，不纳入仓库）
├── i18n/zh.json, en.json       # 中英文案
├── ydlna/
│   ├── app.py                  # 应用编排：组装各组件、启动 DLNA、桥接投屏事件
│   ├── config.py               # 配置持久化（config.json）
│   ├── i18n.py                 # 翻译函数 tr() + 运行时语言切换
│   ├── logger.py               # 日志（控制台 + 轮转文件）
│   ├── constants.py            # 常量、PATH 设置
│   ├── dlna/                   # DLNA 协议层（与 UI 解耦）
│   │   ├── avtransport.py      # AVTransport 服务（SetAVTransportURI/Play/Seek/GetPositionInfo...）
│   │   ├── rendering_control.py# RenderingControl 服务（音量/静音）
│   │   ├── connection_manager.py
│   │   ├── device.py           # MediaRenderer 设备声明
│   │   ├── renderer_bridge.py  # 协议层 ↔ 播放器桥接 + 进度回传
│   │   └── server.py           # UpnpServer 启停封装
│   ├── player/                 # 播放器层
│   │   ├── mpv_player.py       # python-mpv 封装 + Qt 信号桥
│   │   ├── mpv_widget.py       # 嵌入 libmpv 的 QWidget（原生窗口句柄）
│   │   └── player_interface.py # 播放器页面（渲染区 + 控制条 + 空状态）
│   └── ui/                     # UI 层
│       ├── main_window.py      # MSFluentWindow 框架
│       ├── home_interface.py   # 主页（状态/设备信息/引导）
│       ├── settings_interface.py
│       ├── media_controls.py   # Fluent 风格播放控制条
│       ├── tray.py             # 系统托盘
│       └── widgets.py          # 状态指示灯等小组件
```

---

## 七、技术要点（开发文档）

实现过程中踩过的关键坑，记录于此供后续维护参考。

### 7.1 必须用 `qasync`，不能只用 `asyncio`

DLNA 服务（async-upnp-client）是 asyncio 库，mpv 的播放事件回调在独立线程，UI 是 Qt 主线程。三者必须共享同一个事件循环。

`qasync` 提供的 `QEventLoop` 把 asyncio loop 嫁接到 Qt 事件循环上。**入口必须用 `qasync.run()` 或 `QEventLoop`**，不能用标准 `asyncio.run()`——否则 mpv 事件线程 emit 的 Qt 信号无法跨线程投递到主线程（信号到达不了槽，UI 不刷新）。

参考 `main.py` 的 `_bootstrap()`。

### 7.2 导入顺序：先 PySide6，后 libmpv（Windows 专属坑）

Windows 上，`import mpv` 会通过 `ctypes` 加载 `libmpv-2.dll`（110 MB 的巨型 dll）。**如果先加载 libmpv 再导入 PySide6，会触发 `ImportError: DLL load failed while importing QtWidgets`**——libmpv 的依赖 dll 会污染进程的 DLL 解析表，干扰 Qt6 的延迟加载 dll。

**正确顺序**（见 `main.py`）：
```python
from PySide6.QtWidgets import QApplication   # 1. 先完全加载 PySide6
import qasync                                # 2. qasync
from ydlna.player.mpv_player import ...      # 3. 最后加载 libmpv
```

`ydlna/player/mpv_player.py` 在 import 时临时把 `bin/` 加入 `PATH`（**不**用 `os.add_dll_directory`，那个会持久影响 Qt 的 dll 搜索），让 `ctypes.util.find_library` 找到 libmpv，import 完后恢复 PATH。

### 7.3 DLNA service 文件不能用 `from __future__ import annotations`

`async_upnp_client.server` 的 `@callable_action` 装饰器在运行时比较方法参数的**类型注解对象**（如 `<class 'int'>`）与状态变量声明的 `data_type`。PEP 563（`from __future__ import annotations`）会把注解变成字符串（`'int'`），导致 `assert state_var.data_type_mapping["type"] == annotation` 失败。

所以 `avtransport.py` / `rendering_control.py` / `connection_manager.py` **不能**用 future annotations，必须用真类型注解。

### 7.4 `device.services` 是 dict 不是 list

`async_upnp_client` 的 `UpnpDevice.services` 是 `dict[service_type_URN, UpnpService]`。遍历找 service 时要遍历 `.values()`，否则遍历到的是字符串 key。见 `server.py` 的 `_find_service`。

### 7.5 mpv 回调线程安全

`python-mpv` 的 `property_observer` / `event_callback` 在 `MPVEventHandlerThread` 线程触发。直接 emit Qt 信号是安全的（跨线程 queued，由 qasync loop 处理），但**不要在回调里直接操作 Qt 控件**。所有 UI 更新通过 `PlayerSignals` 的信号 marshal 回主线程。

### 7.6 SSDP 的 Windows UDP 告警

启动时可能看到 `OSError: [WinError 10022]` 或 `[WinError 10038]`，来自 SSDP 的 UDP 多播协议。这是 Windows IOCP 与 asyncio 的已知交互问题，**不影响 DLNA 功能**——HTTP/SOAP/GENA 都正常工作，手机能正常发现设备。

---

## 八、故障排查

### 启动报「缺少 libmpv」
按「二、安装 → 2. 获取 libmpv」把 `libmpv-2.dll` 放进 `bin/`。

### 启动报 `DLL load failed while importing QtWidgets`
确认用 `python main.py` 启动（它处理了导入顺序）；不要在其他脚本里先 `import ydlna.player.mpv_player` 再 `import PySide6.QtWidgets`。

### 手机找不到设备
1. 确认手机和电脑在**同一个 Wi-Fi/局域网**。
2. 确认主页显示的本机 IP 是局域网地址（如 `192.168.x.x`），不是 `127.0.0.1`。
3. **Windows 防火墙**：首次启动会询问是否允许 Python 联网，选「允许」。若误点了拒绝，去「Windows Defender 防火墙 → 允许应用通过防火墙」找到 Python 勾选「专用」网络。
4. 路由器开启了「AP 隔离 / 客户端隔离」会阻断设备发现，需关闭。
5. 查看 `lightcast.log` 是否有「DLNA 服务已启动」。

### 投屏后电脑不播放
1. 查看 `lightcast.log` 里是否有 `SetAVTransportURI` / `桥接: 设置媒体` / `媒体已装载` 日志。
2. 确认 `bin/libmpv-2.dll` 存在。
3. 部分控制点推送的流可能需要转码；试试不同格式的视频。

### 进度不刷新
轻投每秒回传进度。若手机端进度条不动，查看日志是否有「更新 RelTime」相关。某些控制点不订阅 GENA 事件则看不到实时进度（但点查询按钮仍能看到）。

---

## 九、许可证

- **本项目代码**：GPLv3（因使用 PySide6-Fluent-Widgets，其社区版为 GPLv3）
- **libmpv**：GPLv2+（随程序分发需遵守其许可）
- **参考实现**：[Macast](https://github.com/xfangfang/Macast)（GPLv3）

## 十、致谢

- [xfangfang/Macast](https://github.com/xfangfang/Macast) —— DLNA 投屏接收的参考实现
- [StevenLooman/async_upnp_client](https://github.com/StevenLooman/async_upnp_client) —— DLNA/UPnP 协议栈
- [jaseg/python-mpv](https://github.com/jaseg/python-mpv) —— libmpv 的 Python 绑定
- [zhiyiYo/PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets) —— Fluent Design 组件库
- [mpv](https://mpv.io/) —— 媒体播放器

---

## 十一、打包

### 1. 构建 exe（PyInstaller）

```bash
pip install pyinstaller
pyinstaller LightCast.spec --noconfirm
```

产物：`dist/LightCast/LightCast.exe`（onedir 模式，含 `_internal/` 全部依赖，约 400MB）。

要点：
- `bin/libmpv-2.dll` 会随包打入 `_internal/bin/`，运行时自动注入 PATH 供 python-mpv 加载
- 安装版把配置/日志写入 `%APPDATA%\LightCast`，不污染安装目录
- spec 里排除了环境中的 PyQt5/PyQt6（与 PySide6 冲突）和整套 ML 栈
  （numpy/scipy 是 qfluentwidgets 的运行时依赖，会保留；torch/transformers 等
  由 scipy 条件导入带入，与本应用无关，全部排除）

### 2. 制作安装程序（Inno Setup 6）

1. 安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php)
2. 先执行第 1 步构建 exe
3. 用 ISCC 编译脚本：

```bash
ISCC.exe packaging\installer.iss
```

产物：`dist/LightCast-Setup-0.1.0.exe`（安装向导：开始菜单/桌面快捷方式、卸载程序、中文/英文双语界面）。
