# 轻投（LightCast）—— DLNA 投屏接收软件

把你的电脑变成一个 DLNA 投屏接收端：从手机把视频投到电脑上，用内嵌的 libmpv 播放器播放。

- 🎬 内嵌 libmpv：硬件解码，几乎所有格式
- 📺 投屏即缓冲：收到指令立即进入播放器页，体感秒开
- 🧩 流媒体兼容层：加密 HLS、漫画/图文番、PNG 伪装分片、防盗链、直链代理，换番不报错
- 🔊 音频输出设备选择、开机自启、系统托盘常驻
- 🔄 自动更新：启动时检查新版本，一键下载安装（可关闭）
- 🌐 中英双语，Win11 Fluent 风格界面
- 📡 播放进度实时回传给手机端

> 🎯 **当前以视频投屏为主**（看番 / 追剧类 App 兼容性较好，如 Lanerc）；音乐、图片与直播投屏正在规划中。
>
> 仅支持 **Windows 10/11（64 位）**。

---

## 获取方式

### 方式一：下载 Release（推荐）

从本仓库的 [Releases](https://github.com/yunluoxincheng/LightCast/releases) 页面下载最新版本：

- **`LightCast-Setup-<版本>.exe`** —— 安装程序（含开始菜单/桌面快捷方式、卸载程序）
- 或**便携版压缩包** —— 解压即用

下载后无需安装 Python 或 libmpv，直接运行即可。

### 方式二：源码运行

**环境要求**：Windows 10/11（64 位）、Python 3.10 ~ 3.12

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 获取 libmpv（播放器核心，约 110MB）
#    从 https://sourceforge.net/projects/mpv-player-windows/files/libmpv/
#    下载 mpv-dev-x86_64-*.7z，解压出 libmpv-2.dll 放到 bin/ 目录

# 3. 运行
python main.py
```

---

## 快速使用

1. 手机和电脑连**同一个 Wi-Fi / 局域网**（确认主页显示的是局域网 IP）。
2. 在手机任意支持 DLNA / 投屏的应用里（BubbleUPnP、VLC、系统相册「投射」、多数看番 / 追剧应用），选择设备 **`轻投`**。
3. 选择要播放的内容，电脑自动进入播放器页开始缓冲、播放，手机端实时显示进度。

> **Windows 防火墙**：首次启动请允许 Python / 轻投 联网（「专用」网络）。

---

## 常见问题

- **手机找不到设备**：确认同一局域网；检查 Windows 防火墙是否放行；路由器「AP 隔离」需关闭。
- **投屏报错**：界面会给出可读原因（防盗链 403 / 链接失效 / 密钥被拦截 / 内容异常 / 网络超时）；详细日志见 `lightcast.log`（打包版在 `%APPDATA%\LightCast\`）。
- **换一部番就报错**：加密 HLS、漫画番、PNG 伪装分片、防盗链等场景已由内置代理层处理，无需手动操作。

---

## 许可证

GPLv3（代码基于 PySide6-Fluent-Widgets 社区版；libmpv 为 GPLv2+）。架构参考 [Macast](https://github.com/xfangfang/Macast)。

> 贡献者 / 开发者请阅读 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)（架构、踩坑记录与打包发布流程）。
