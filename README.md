# 轻投（LightCast）—— DLNA 投屏接收软件

把你的电脑变成一个 DLNA MediaRenderer（投屏接收端）。从手机（或任意 DLNA 控制点）把视频、音乐、图片投到电脑上，用内置的现代化播放器播放。

- 🎬 内嵌 libmpv 播放器：硬件解码、MKV/H.265/FLAC 等几乎所有格式
- 📺 **投屏即缓冲**：收到投屏指令立即进入播放器页并显示缓冲动画，体感秒开
- 🧩 **深挖的流媒体兼容层**：HLS 加密流（AES-128）、漫画/图文番图片流、PNG 伪装分片、防盗链 Referer、直链代理，换番不报错（详见「流媒体兼容性」）
- 🎨 Win11 Fluent Design 风格界面（PySide6 + PySide6-Fluent-Widgets）
- 🌐 中英双语运行时切换
- 🔊 音频输出设备选择（默认 / 扬声器 / 耳机，插拔自动刷新）
- ⚡ 开机自启、系统托盘常驻、关闭主窗口不退出
- 📡 完整实现 UPnP AVTransport / RenderingControl / ConnectionManager，播放进度实时回传给手机端
- 🪟 架构清晰：PySide6（UI）+ qasync（事件循环融合）+ async-upnp-client（DLNA 协议）

> 架构参考了 [xfangfang/Macast](https://github.com/xfangfang/Macast)，UI 用 Qt + Fluent 组件重写，DLNA 协议改用 async-upnp-client 的服务端实现，SSDP 设备发现层为线程化原生实现（修复 Windows 多播发现失败问题）。

---

## 一、环境要求

- **Windows 10/11**（64 位）
- **Python 3.10 ~ 3.12**（在 3.11.9 上开发测试）
- **libmpv**（`libmpv-2.dll`，见下文获取方式）

> 也可以直接用打包好的版本（见「十、打包」）。

---

## 二、安装

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

核心依赖：`PySide6`、`PySide6-Fluent-Widgets`、`qasync`、`python-mpv`、`async-upnp-client`、`python-didl-lite`、`aiohttp`、`Pillow`、`netifaces`。

### 2. 获取 libmpv（关键，播放器依赖它）

轻投通过 `python-mpv` 调用 libmpv 渲染视频，**必须手动下载 `libmpv-2.dll` 放到 `bin/` 目录**（dll 体积大，不纳入仓库）。

**步骤：**

1. 访问 mpv libmpv dev 包下载页：
   https://sourceforge.net/projects/mpv-player-windows/files/libmpv/
2. 下载最新的 **`mpv-dev-x86_64-<日期>.7z`**（标准版，**不要**下 `v3` 后缀的精简版，那个不含独立 dll）
3. 用 7-Zip 解压，取出里面的 **`libmpv-2.dll`**（约 110 MB）
4. 放到项目的 **`bin/libmpv-2.dll`**：

```
LightCast/
├── bin/
│   └── libmpv-2.dll   ← 放这里
├── main.py
├── ydlna/
└── ...
```

> 文件名必须是 `libmpv-2.dll`、`mpv-2.dll` 或 `mpv-1.dll` 之一（`python-mpv` 的搜索约定）。
> 首次启动时若检测不到 dll，轻投会弹窗提示。

---

## 三、运行

```bash
python main.py
```

启动后：

1. 主窗口显示，左侧导航：**主页 / 播放器 / 设置**
2. DLNA 投屏服务默认**自动开启**（可在设置里关闭）
3. 主页显示本机设备名和局域网 IP（确认手机和电脑在同一网络）
4. **确保 Windows 防火墙允许 Python 入站连接**（首次启动会弹窗，选「允许」）

---

## 四、如何投屏

1. 确保手机和电脑连**同一个 Wi-Fi / 局域网**。
2. 在手机上打开支持 DLNA 的应用：
   - **通用**：[BubbleUPnP](https://play.google.com/store/apps/details?id=com.bubblesoft.android.bubbleupnp)（推荐，Android）
   - **看番 / 追剧**：多数视频 App 自带「投屏 / DLNA」入口
   - **视频 / 音乐**：VLC、MX Player、系统相册/文件的「投射」
   - **iOS**：建议用第三方 DLNA 应用（系统 AirPlay 不兼容 DLNA）
3. 在应用的设备列表里，选择名为 **`轻投`**（或你在设置里改的名字）的设备。
4. 选择要播放的内容，电脑**立即**进入播放器页开始缓冲，解码完成后自动播放。
5. 播放器页支持：进度定位、播放/暂停/停止、音量、±10s、倍速、全屏（键盘：`空格` 播放/暂停、`←→` 快进后退、`↑↓` 音量、`F`/`Esc` 全屏）。

手机端会实时显示播放进度（轻投每秒回传进度）。

---

## 五、设置说明

进入「设置」页：

| 设置 | 说明 | 是否需要重启 |
|---|---|---|
| 界面语言 | 简体中文 / English，**即时切换** | 否 |
| 主题 | 浅色 / 深色 / 跟随系统，即时生效 | 否 |
| 开机自动运行轻投 | 登录 Windows 后自动在后台启动 | 否 |
| 音频输出设备 | 声音从哪个设备播放（默认 / 扬声器 / 耳机…，插拔自动刷新） | 否 |
| 启动时自动开启投屏服务 | 打开轻投后自动开始接收投屏 | 否 |
| 设备名称 | 手机端搜索设备时显示的名称 | **是** |
| 服务端口 | HTTP 端口，`0` = 自动分配，一般无需修改 | **是** |

需要重启的设置改完后，关闭并重新打开轻投即可。

---

## 六、流媒体兼容性

投屏源五花八门，轻投内置了一个**本地流媒体代理层**（`ydlna/player/hls_rewriter.py`），把各种「不按规范来」的流在交给播放器前先修好：

| 场景 | 处理方式 |
|---|---|
| 分片用 `.jpg` 等非标准扩展名伪装（新版 ffmpeg 白名单直接拒绝） | 分片统一改名 `.mp4/.ts/.avi` 再提供 |
| AES-128 加密 HLS（`#EXT-X-KEY`） | 密钥 URI 重写到本地代理并转发（16 字节校验，识别被防盗链拦截的假密钥） |
| fMP4 初始化段（`#EXT-X-MAP`） | 一并重写到本地端点 |
| 漫画 / 图文番（分片是真实 PNG/JPEG 图片） | 图片惰性转 MJPEG/AVI 再播放 |
| 「PNG 封面 + TS 视频」混合分片（番剧站省流量套路） | 探测并剥离封面，按 TS 提供（含预取，秒开） |
| 防盗链 403（CDN 校验 Referer / UA） | 所有上游请求自动附加 Referer + 浏览器 UA，复用 cookie，5xx/网络错误自动重试 |
| 302 重定向 / 主播放列表（只有变体） | 按最终地址解析；自动跟进第一个变体 |
| 直播流（无 `#EXT-X-ENDLIST`） | 不静态代理，交 mpv 原生播放（列表自动刷新） |
| 非 m3u8 直链（mp4/ts/图片） | 同样走代理：防盗链 + 重试 + 内容模式兼容 |
| 源站返回 HTML 错误页（登录墙伪装 200） | 识别并给出友好提示，不喂给播放器 |

播放失败时，界面会按错误细节给出**可读的原因**（防盗链 403 / 链接失效 / 密钥被拦截 / 内容异常 / 网络超时），并附带技术细节；完整日志见 `lightcast.log`。

---

## 七、项目结构

```
LightCast/
├── main.py                     # 入口（注意导入顺序：PySide6 → qasync → ydlna.player）
├── requirements.txt
├── LightCast.spec              # PyInstaller 打包配置
├── config.json                 # 运行配置（自动生成）
├── bin/libmpv-2.dll            # libmpv（用户自行放入，不纳入仓库）
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
    │   ├── ssdp_listener.py    # 线程化原生 SSDP 监听（Windows 多播发现，替代 asyncio 实现）
    │   └── _net.py             # 网卡枚举 / 子网判断
    ├── player/                 # 播放器层
    │   ├── mpv_player.py       # python-mpv 封装 + Qt 信号桥（含缓冲状态）
    │   ├── mpv_widget.py       # 嵌入 libmpv 的 QWidget（原生窗口句柄）
    │   ├── hls_rewriter.py     # 流媒体代理层（HLS 重写 + 直链代理，见「六」）
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

---

## 八、技术要点（开发文档）

实现过程中踩过的关键坑，记录于此供后续维护参考。

### 8.1 必须用 `qasync`，不能只用 `asyncio`

DLNA 服务（async-upnp-client）是 asyncio 库，mpv 的播放事件回调在独立线程，UI 是 Qt 主线程。三者必须共享同一个事件循环。

`qasync` 提供的 `QEventLoop` 把 asyncio loop 嫁接到 Qt 事件循环上。**入口必须用 `qasync.run()` 或 `QEventLoop`**，不能用标准 `asyncio.run()`——否则 mpv 事件线程 emit 的 Qt 信号无法跨线程投递到主线程（信号到达不了槽，UI 不刷新）。参考 `main.py` 的 `_bootstrap()`。

### 8.2 导入顺序：先 PySide6，后 libmpv（Windows 专属坑）

Windows 上，`import mpv` 会通过 `ctypes` 加载 `libmpv-2.dll`（110 MB 的巨型 dll）。**如果先加载 libmpv 再导入 PySide6，会触发 `ImportError: DLL load failed while importing QtWidgets`**——libmpv 的依赖 dll 会污染进程的 DLL 解析表，干扰 Qt6 的延迟加载 dll。

**正确顺序**（见 `main.py`）：

```python
from PySide6.QtWidgets import QApplication   # 1. 先完全加载 PySide6
import qasync                                # 2. qasync
from ydlna.player.mpv_player import ...      # 3. 最后加载 libmpv
```

`ydlna/player/mpv_player.py` 在 import 时临时把 `bin/` 加入 `PATH`（**不**用 `os.add_dll_directory`，那个会持久影响 Qt 的 dll 搜索），让 `ctypes.util.find_library` 找到 libmpv，import 完后恢复 PATH。

### 8.3 DLNA service 文件不能用 `from __future__ import annotations`

`async_upnp_client.server` 的 `@callable_action` 装饰器在运行时比较方法参数的**类型注解对象**（如 `<class 'int'>`）与状态变量声明的 `data_type`。PEP 563（`from __future__ import annotations`）会把注解变成字符串（`'int'`），导致 `assert state_var.data_type_mapping["type"] == annotation` 失败。所以 `avtransport.py` / `rendering_control.py` / `connection_manager.py` **不能**用 future annotations。

### 8.4 `device.services` 是 dict 不是 list

`async_upnp_client` 的 `UpnpDevice.services` 是 `dict[service_type_URN, UpnpService]`。遍历找 service 时要遍历 `.values()`。见 `server.py` 的 `_find_service`。

### 8.5 mpv 回调线程安全

`python-mpv` 的 `property_observer` / `event_callback` 在 `MPVEventHandlerThread` 线程触发。直接 emit Qt 信号是安全的（跨线程 queued，由 qasync loop 处理），但**不要在回调里直接操作 Qt 控件**。所有 UI 更新通过 `PlayerSignals` 的信号 marshal 回主线程。

### 8.6 SSDP 设备发现：不能用 asyncio 的 UDP 多播

Windows 的 IOCP（`ProactorEventLoop`）对**多播 UDP 接收**有长期缺陷——socket 建好、membership 也加了，就是收不到手机发的 M-SEARCH。**解决方案**（`ssdp_listener.py`）：独立线程 + 原生阻塞 socket（照 Macast 验证过的模式），按网卡逐个加入多播组，收到 M-SEARCH 后选「与请求方同子网」的网卡 IP 拼 LOCATION 单播回复，并周期性主动广播 `ssdp:alive` 加速发现。HTTP/SOAP/GENA 部分仍用 async_upnp_client（可靠）。

### 8.7 模态对话框会卡死应用（打包版）

qfluentwidgets 的 `MessageBox.exec()`（嵌套事件循环 + acrylic 特效）在打包版中点击按钮会冻结应用。**用 Qt 原生 `QMessageBox` + 非阻塞 `open()` + `finished` 回调**替代，从架构上杜绝嵌套事件循环。见 `main_window.py` 的 `_ask_minimize_to_tray`。

### 8.8 设置页会撑大窗口最小高度

设置页内容纵向堆叠（约 1000px）会撑大窗口 `minimumSizeHint`，导致窗口无法缩小。**内容包进 `ScrollArea` 滚动区**，窗口最小高度即解放。改窗口默认尺寸时记得升 `window_geometry_vN` 配置标记，否则旧几何会覆盖新默认。

---

## 九、故障排查

### 启动报「缺少 libmpv」
按「二、安装 → 2. 获取 libmpv」把 `libmpv-2.dll` 放进 `bin/`。

### 启动报 `DLL load failed while importing QtWidgets`
用 `python main.py` 启动（它处理了导入顺序）；不要在其他脚本里先 `import ydlna.player.mpv_player` 再 `import PySide6.QtWidgets`。

### 手机找不到设备
1. 确认手机和电脑在**同一个 Wi-Fi / 局域网**。
2. 确认主页显示的本机 IP 是局域网地址（如 `192.168.x.x`），不是 `127.0.0.1`。
3. **Windows 防火墙**：首次启动会询问是否允许联网，选「允许」。若误点了拒绝，去「Windows Defender 防火墙 → 允许应用通过防火墙」勾选 Python 的「专用」网络。
4. 路由器开启了「AP 隔离 / 客户端隔离」会阻断设备发现，需关闭。
5. 多网卡环境（虚拟网卡 / ICS 共享）：日志中应出现「SsdpListener 监听 0.0.0.0:1900，已加入 N 个网卡的多播组」。若手机搜索时没有「收到 M-SEARCH」日志，是防火墙/网卡问题。

### 投屏后电脑不播放 / 换一部番报错
1. 投屏瞬间播放器页会显示「正在缓冲…」；如果一直转圈或报错，查看 `lightcast.log`。
2. 日志关键线索：
   - `代理上游失败 / 上游返回 HTTP 403` → 防盗链，代理已自动附加 Referer/UA 并重试，仍失败则是链接过期
   - `密钥长度异常` → 加密流密钥被源站拦截
   - `上游返回 HTML 页面` → 登录墙 / 防盗链错误页
   - `检测到混合分片 / 检测到图像流` → 兼容层已生效
3. 播放失败提示会给出可读原因（403 防盗链 / 404 链接失效 / 密钥被拦截 / 内容异常 / 网络超时）。

### 声音不对 / 没有声音
「设置 → 音频输出设备」选择正确的输出设备（插拔耳机后列表自动刷新）；确认系统音量未被静音。

### 直播流卡顿 / 提前结束
直播流走 mpv 原生播放，已内置 `max_reload=1000` 防列表刷新提前退出；网络波动时播放器页会重新出现缓冲动画。

### 进度不刷新
轻投每秒回传进度。某些控制点不订阅 GENA 事件则看不到实时进度（但点查询按钮仍能看到）。

---

## 十、打包

### 1. 构建 exe（PyInstaller）

```bash
pip install pyinstaller
pyinstaller LightCast.spec --noconfirm
```

产物：`dist/LightCast/LightCast.exe`（onedir 模式，含 `_internal/` 全部依赖，约 400MB）。

要点：
- `bin/libmpv-2.dll` 会随包打入 `_internal/bin/`，运行时自动注入 PATH 供 python-mpv 加载
- 安装版把配置/日志写入 `%APPDATA%\LightCast`，不污染安装目录
- spec 里排除了环境中的 PyQt5/PyQt6（与 PySide6 冲突）和整套 ML 栈（numpy/scipy 是 qfluentwidgets 的运行时依赖，会保留；torch/transformers 等由 scipy 条件导入带入，与本应用无关，全部排除）
- 打包前请退出正在运行的轻投（exe 被占用会导致构建失败）

### 2. 制作安装程序（Inno Setup 6）

1. 安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php)
2. 先执行第 1 步构建 exe
3. 用 ISCC 编译脚本：

```bash
ISCC.exe packaging\installer.iss
```

产物：`dist/LightCast-Setup-0.1.0.exe`（安装向导：开始菜单/桌面快捷方式、卸载程序、中文/英文双语界面）。

---

## 十一、许可证

- **本项目代码**：GPLv3（因使用 PySide6-Fluent-Widgets，其社区版为 GPLv3）
- **libmpv**：GPLv2+（随程序分发需遵守其许可）
- **参考实现**：[Macast](https://github.com/xfangfang/Macast)（GPLv3）

## 十二、致谢

- [xfangfang/Macast](https://github.com/xfangfang/Macast) —— DLNA 投屏接收的参考实现
- [StevenLooman/async_upnp_client](https://github.com/StevenLooman/async_upnp_client) —— DLNA/UPnP 协议栈
- [jaseg/python-mpv](https://github.com/jaseg/python-mpv) —— libmpv 的 Python 绑定
- [zhiyiYo/PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets) —— Fluent Design 组件库
- [mpv](https://mpv.io/) —— 媒体播放器
