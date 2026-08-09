# 代码审查报告

> 审查日期：2026-08-09
> 审查范围：`ydlna/` 全部 28 个模块（约 6300 行）、`tests/`、配置 / 构建 / CI / 文档
> 审查方式：只读审查，未改动任何代码
> 审查者：ZCode（含 4 个并行子审查 + 关键结论的人工交叉核验）

本报告按严重程度分级，每条标注**文件路径:行号**，并给出问题、影响、建议。修复进度用
`[ ]` / `[x]` 标记，建议按下面的「修复优先级」分批推进，每修一项就勾掉一项。

---

## 一、总体评价

工程整体质量**较高**：

- 架构清晰（应用编排 / DLNA / 播放器 / UI / 代理层分层明确）。
- 注释极其详尽——几乎每个设计决策都记录了「为什么」，对 Windows 平台经典坑
  （DLL 加载顺序、IOCP 收不到多播、模态 exec 卡死、Mica 闪屏）都有文档化的正确处理。
- `.gitignore` 配置正确，无敏感信息入库，无硬编码密钥。

但存在 **3 个真实严重问题** 和一批中低级隐患。核心风险集中在两条线：

1. **安全**：代理 SSRF + 自动更新无校验。
2. **工程化**：零测试。

---

## 二、严重问题（Critical）

### 🔴 C1. 自动更新无任何完整性校验，第三方镜像可篡改安装包（供应链 RCE）

- **文件**：`ydlna/updater.py:38-42, 190-227, 391-449`；`.github/workflows/release.yml`
- **状态**：`[x]` 新客户端已修复（2026-08-09）：Release 附 `SHA256SUMS.txt`；客户端只从 GitHub API 直连取校验和，下载（可经镜像）落地后强制 `hashlib.sha256` 比对，不匹配/缺失则拒绝安装。该保护无法追溯到已经安装的旧 updater；从旧版本迁移到首个安全版本时，必须从 GitHub Releases 手动下载安装一次，并在 Release Notes 中明确提示。
- **问题**：
  - `run_update_flow` 下载完 `LightCast-Setup-<ver>.exe` 后直接 `os.startfile(dest)` 执行，
    **无 SHA-256 / 签名校验**。
  - 默认 `use_mirror=True`，会把 GitHub URL 拼到三个**第三方代理**
    （`gh-proxy.com / ghfast.top / ghproxy.net`）前并行探测取最快——整条下载链路脱离 GitHub。
    这些代理对返回内容完全可控，可替换成木马安装包。
  - Release workflow 也未对产物签名或附带校验和文件。
- **影响**：供应链远程代码执行——镜像运营方或对其发起中间人的攻击者可在每次自动更新时
  下发恶意程序，用户点「安装」即以用户权限执行任意代码。
- **建议**：
  1. Release 附带 `SHA256SUMS.txt`（发布 workflow 计算并上传为 asset）。
  2. 客户端**只从 `api.github.com`（直连 HTTPS）取校验和**，下载（可经镜像）后用
     `hashlib.sha256` 强制比对，不匹配则拒绝执行并报错。
  3. 校验锚点必须落在 GitHub 官方端点，**校验和本身不能从镜像取**。
  4. 进阶：用 cosign / sigstore 给安装包签名，客户端验签。

### 🔴 C2. HLS / 直链代理对上游 URL 无 scheme/IP 过滤 = SSRF

- **文件**：`ydlna/player/hls_rewriter.py:304-329, 331-393, 472-553, 594-714`；
  `ydlna/dlna/renderer_bridge.py:152-200`
- **状态**：`[x]` 已修复（2026-08-09）：仅允许 http/https；所有上游请求关闭自动重定向并逐跳校验；`SSRFSafeConnector` 在 aiohttp 连接解析阶段过滤实际候选 IP，使校验结果与建连使用同一批地址；安全拒绝统一抛 `UrlBlockedError`，mpv 只接收本地代理 URL，不再存在原链 fallback。设置页「允许内网投屏源」默认开以兼容 DLNA 私网媒体，但回环、link-local、云元数据、未指定和多播地址始终拒绝。
- **问题**：投屏来的 URL（`SetAVTransportURI`）在 `_get` / `_forward_url` 里直接
  `session.get(url)`，**无任何校验**：
  - 无 scheme 白名单（仅靠 aiohttp 间接挡 `file://`，非显式）。
  - 无目标 IP 黑名单：`127.0.0.1`、`169.254.169.254`（云元数据）、`10/8`、`192.168/16`、
    `172.16/12`、`::1` 全部可达。
  - `allow_redirects=True` 默认开启，即便校验初始 URL，上游一个 `302 → 内网地址` 即可绕过。
  - 不仅最外层投屏 URL，m3u8 **内容里**的 `#EXT-X-KEY` / `#EXT-X-MAP` / 分片 URL 也都被转发。
  - **说明**：代理监听 `127.0.0.1` 本身是对的，不直接暴露外网；SSRF 风险在「上游抓取」——
    同 Wi-Fi 任何人都能借受害者机器当跳板，访问其本机服务、内网管理面板、路由器配置、云元数据凭据。
- **影响**：局域网攻击者借受害者机器读取其本机 / 内网服务，或探测内网。
- **建议**：
  - 对投屏 URL 和 m3u8 内所有 URL 做 scheme 白名单（仅 `http` / `https`）。
  - 解析 DNS 后做黑名单：拒绝 loopback（`127/8`、`::1`）、link-local（`169.254/16`、
    `fe80::/10`）、private（`10/8`、`172.16/12`、`192.168/16`、`fc00::/7`）、`0.0.0.0`。
  - 关闭重定向或对每跳重新校验（`allow_redirects=False` 手动跟进，或 `TraceConfig`
    的 `on_request_redirect` 钩子）。
  - 至少把「投屏 URL 指向私有 / 回环地址」作为可疑事件记录并默认拒绝。

### 🔴 C3. 测试目录为空，核心模块 0% 覆盖；CI 无任何质量门禁

- **文件**：`tests/`；`.github/workflows/release.yml`
- **状态**：`[x]` 安全关键路径已建立正式回归门禁（2026-08-09）：50 个 pytest 用例覆盖 SSRF 重定向/解析/fallback、私网开关、AES key 边界、Pillow 像素限制与更新 SHA-256；PR 与发布构建均先运行 Windows test job。全项目覆盖率、ruff/mypy/bandit 仍属后续工程化工作。
- **问题**：`ydlna/` 6300 行、28 模块，`tests/` 下 `git ls-files` 无任何条目，无 pytest / conftest。
  CI 是 tag 推送即构建即发布，**无 pytest / ruff / mypy / bandit**。最该测的 `updater.py`
  （下载并执行 exe）和 `hls_rewriter.py`（951 行、分支极多）完全裸奔。
- **影响**：任何回归（尤其更新器、HLS 代理的复杂分支）都无法自动发现；CHANGELOG 显示近期高频修
  控制栏 / 全屏 bug，这类问题最适合回归测试。
- **建议**：
  - 优先补纯函数测试（都不依赖 GUI / libmpv，可直接在 CI 跑）：
    - `updater.py`：`parse_version` / `is_newer` / `_md_to_html` / `rank_sources`（mock session）。
    - `hls_rewriter.py`：`_origin` / `_detect_image` / m3u8 重写规则（喂样例文本）。
    - `config.py`：load / save / merge（传临时路径）。
  - CI 加 test job（`pip install pytest && pytest`），在发布前阻断有回归的构建。
  - 后续给 GUI / mpv 模块用 mock 隔离补测试骨架。

---

## 三、高危问题（High）

### H1. 代理 DoS 面：无上限读取 / 解压炸弹 / 无限缓存 / 慢速攻击

- **文件**：`ydlna/player/hls_rewriter.py:771, 494, 825, 923, 631-637, 704-709, 410-411, 345, 261`
- **状态**：`[x]` 部分修复（2026-08-09）：m3u8/密钥/探测读取加硬上限；AES key 多读 1 字节并要求恰好 16 字节；PIL `MAX_IMAGE_PIXELS` 收紧为 4096² 且把 `DecompressionBombWarning` 提升为异常；`_jpeg_cache` 加 LRU；连接器限制为总 64 / 单 host 16。**未修**：`_forward_url` 流式转发的独立 `sock_read` 超时（见 M6，留后续）。
- **问题**：
  - **OOM**：m3u8（`:771`）、AES 密钥（`:494`）、探测（`:825/923`）三处 `await resp.read()`
    **无大小上限**（只有 hybrid / image / direct 走的 `_read_capped` 有 96MB 上限）。恶意上游可让进程 OOM。
  - **PIL 解压炸弹**：`Image.open`（`:631/704`）未设 `MAX_IMAGE_PIXELS`，几十 KB PNG 可解码成
    ≈40GB RGB 位图。
  - **`_jpeg_cache` 无 LRU**（`:410`）：长番漫画流缓存线性增长。
  - **慢速攻击**：`_forward_url`（`:345`）转发无超时；session（`:261`）无 `limit_per_host`，
    同一慢速 host 可挂几百条并发连接。
- **建议**：
  - m3u8 / 密钥 / 探测统一走带硬上限的读取（m3u8 给 1MB；密钥读 16 字节后停；探测用
    `resp.content.read(65536)` 并检查后续是否还有大量数据）。
  - 显式 `Image.MAX_IMAGE_PIXELS`（如 4096×4096 像素），并把 `DecompressionBombWarning` 也按错误处理。
  - `_jpeg_cache` 加 LRU 上限（如 16）。
  - session 用 `TCPConnector(limit=64, limit_per_host=16)`；转发请求用
    `ClientTimeout(total=None, connect=10, sock_read=30)`。

### H2. mpv 危险特性未完全关闭

- **文件**：`ydlna/player/mpv_player.py:126-143`
- **状态**：`[x]` 已修复（2026-08-09）：加 `config=False`、`load_scripts=False`。
- **问题**：已关 `ytdl / osc / input_cursor`（好），但缺 `config=False`（默认读 `mpv.conf`）、
  `load_scripts=False`。投屏 URL 来源是局域网外部设备，应最小化 mpv 主动能力。
- **建议**：显式 `config=False, load_scripts=False`；对 `play()` 的 url 做基本协议校验。

### H3. `server.py` base_uri patch 写错了属性名（死代码）

- **文件**：`ydlna/dlna/server.py:64-69`
- **状态**：`[x]` 已修复（2026-08-09）：`self._device._base_uri` → `self._device.base_uri`，并补刷 `self._device.host`。
- **问题**：patch 设的是 `self._device._base_uri`，但库 `UpnpServerDevice` 实际属性是
  `self.base_uri`（无下划线，已核对库源码 `async_upnp_client/server.py:418`）。这行实际是给
  device 新增了一个没人读的孤儿属性，patch 想达到的「刷新 device 的 base_uri」**根本没生效**。
  `self._device.host` 同样未被刷新。
  - **影响修正（已人工核验）**：device.xml 里的 `controlURL` / `SCPDURL` 是**相对路径**
    （库 920-922 行用 `service.control_url`），手机从 SSDP 的 LOCATION 拿 host 拼接——而 SSDP
    LOCATION 由我们自己的 `SsdpListener` 线程用真实 IP 独立生成，**与这个 patch 无关**。
    所以投屏功能仍能工作，这只是一段**半失效死代码**，不是「手机连不上」的致命 bug。
- **建议**：改为 `self._device.base_uri`（去下划线）并补 `host` 刷新；或干脆移除这段无效代码。
  建议用子类化 `UpnpServer` 重写 `async_start` 替代 monkey-patch，并加库版本断言。

### H4. SSDP listener 启停泄漏：线程不 join、socket 竞态

- **文件**：`ydlna/dlna/ssdp_listener.py:322-354`
- **状态**：`[ ]`
- **问题**：
  - `stop()` 只清标志发 byebye，**不 join 线程**，调用方把 `self._ssdp = None` 后旧线程作为
    daemon 残留，反复启停会累积僵尸线程。
  - byebye 发送（`_senders`）与线程内的 `_cleanup`（close socket）无锁，可能出现 close/send 竞态。
  - `async_start` 里 `self._ssdp.start()` 非 await，SSDP 启动失败（如 `_setup_socket` 失败）
    不可见——UI 仍显示「运行中」但实际搜不到设备。
- **建议**：`stop()` 后 `self.join(timeout=2)`；byebye 与 `_cleanup` 之间加锁；`async_start`
  在 `start()` 后短等并检查 `is_alive()`。

### H5. mpv 回调线程直接写 Python 实例状态 + shutdown 无同步

- **文件**：`ydlna/player/mpv_player.py:190, 208-281`
- **状态**：`[ ]`
- **问题**：事件回调在 mpv 独立线程里，除了 emit 信号还**直接写**
  `self._position / _duration / _state / _last_error` 等字段，主线程并发读；`shutdown()` 把
  `self._mpv = None` 但无 barrier / flag 与事件线程同步，存在崩溃窗口。GIL 下单字段赋值原子，
  但多字段组合读有读到中间态的逻辑竞态。
- **建议**：
  - 关键状态用 `threading.Lock` 保护，或所有状态写入都改为通过信号投递到主线程再落库。
  - `shutdown()` 先设 `_shutting_down=True` 标志，回调里检测到就 `return`，配合短延迟确保
    mpv 事件线程不再触发回调后才 `terminate()`。

### H6. asyncio task 未持有引用 + 退出未取消

- **文件**：`ydlna/app.py:147, 221`；`ydlna/ui/settings_interface.py:332`；`ydlna/player/hls_rewriter.py:563`
- **状态**：`[ ]`
- **问题**：`asyncio.create_task` / `ensure_future` 的返回值未保存。事件循环只对 task 持弱引用，
  可能被 GC 静默取消（官方文档明确警告）；退出时未取消会触发 `Task was destroyed but it is pending`
  警告。`hls_rewriter.py:563` 的预热任务同样无引用、`stop()` 不取消，旧 proxy 被任务引用住无法 GC
  （最长 60s 才释放）。
- **建议**：保存 task 到集合（如 `self._tasks`），任务完成回调里移除；`finally` 里
  `gather(*tasks, return_exceptions=True)` 取消；`_BaseProxy.stop()` 取消所有预热任务。

### H7. 局域网任意投屏（无鉴权）

- **文件**：`ydlna/dlna/server.py:111`；`ydlna/dlna/ssdp_listener.py`
- **状态**：`[ ]`
- **问题**：HTTP / SOAP 绑 `0.0.0.0` 无鉴权，任何同二层网络设备（咖啡店 / 酒店 Wi-Fi 的陌生人）
  都能直接投屏 / 调音量。属 DLNA 协议本质。
- **建议**：文档明示风险；提供「仅可信网络」开关（只在指定网卡 announce）；首次投屏弹窗确认。

---

## 四、中等问题（Medium）

### M1. 代理失败状态不一致

- **文件**：`ydlna/dlna/renderer_bridge.py:181-200`
- **状态**：`[x]` 已修复（2026-08-09）：`on_set_uri` 包 try/except，失败置 `ERROR_OCCURRED`；`_hls_proxy/_direct_proxy` 在 `__init__` 声明为 None。
- **问题**：`on_set_uri` 无 try / except，代理失败时 `AVTransportURI` 已更新但播放器没切，
  UI 显示新标题却播旧内容；旧代理可能泄漏（端口 / aiohttp server）。
- **建议**：`on_set_uri` 包 try / except，失败时回滚 `AVTransportURI` 或置
  `TransportState=ERROR_OCCURRED`；`_hls_proxy / _direct_proxy` 在 `__init__` 声明为 None。

### M2. 时间解析坑

- **文件**：`ydlna/dlna/renderer_bridge.py:34-45`
- **状态**：`[ ]`
- **问题**：`dlna_time_to_seconds` 把 `"00:00:00"` 当 None，导致 seek 到 0 秒 / 时长 0 内容异常。
- **建议**：只把 `NOT_IMPLEMENTED` / 空当 None，`00:00:00` 应返回 `0.0`。

### M3. warm 预热阻塞 DLNA action

- **文件**：`ydlna/player/hls_rewriter.py:885-889`
- **状态**：`[ ]`
- **问题**：setup 期串行预热 5 个分片，最坏 5 分钟才返回 SOAP 响应，控制点可能判超时。
- **建议**：setup 期只预热第 0 个（让 mpv 首片命中），其余交给 `_schedule_warm` 后台预取。

### M4. 缓存竞态（thundering herd）

- **文件**：`ydlna/player/hls_rewriter.py:484, 514, 598, 667`
- **状态**：`[ ]`
- **问题**：判空 → 抓取 → 写缓存无锁，同分片 / key 被并发重复抓取。
- **建议**：用 `asyncio.Lock` 或 single-flight 模式（每个 key 一个 `Future`，后续请求 await 同一 Future）。

### M5. 全局快捷键未按页面可见性门控

- **文件**：`ydlna/player/player_interface.py:478-496`
- **状态**：`[ ]`
- **问题**：空格 / 方向键 / 音量键在设置页也会触发播放控制（`Esc` / `F` 内部判了，其余没判）。
  用户在设置页按空格会意外暂停后台播放。
- **建议**：`eventFilter` 顶部、文本控件放行之后，加 `if not self._handler.isVisible(): return False`。

### M6. `_forward_url` 流式转发无超时、连接池无 per-host 限制

- **文件**：`ydlna/player/hls_rewriter.py:345, 261`
- **状态**：`[ ]`
- **问题**：转发请求不传 timeout，落到 session 默认 `total=300s`；session 无 `limit_per_host`，
    慢速上游可挂满连接。
- **建议**：见 H1 的 session / timeout 建议。

### M7. `int(request.match_info["index"])` 未校验 + 负索引静默错位

- **文件**：`ydlna/player/hls_rewriter.py:461, 467, 480, 511, 595`
- **状态**：`[ ]`
- **问题**：路由 `{index}.mp4` 默认匹配 `[^/]+`。`/seg/abc.mp4` → `int("abc")` 抛 500；`/seg/-1.mp4`
  → `self._segments[-1]` 静默取到最后一个分片（Python 负索引），返回错误内容。
- **建议**：路由正则 `{index:\d+}.mp4`；handler 内 `if index < 0 or index >= len(...): return 404`。

### M8. 隐藏页面 / overlay 定时器不停（省电开销）

- **文件**：`ydlna/player/player_interface.py:91-117, 254-266`
- **状态**：`[ ]`
- **问题**：`_Spinner` 30ms 定时器、`_anchor_timer` 150ms 在页面 / overlay 隐藏时仍跑，笔记本
    省电场景下持续唤醒主线程做无谓重绘。
- **建议**：`_Spinner` 加 `start()` / `stop()`，`BufferingOverlay` `showEvent` / `hideEvent` 调用；
  `_anchor_timer` 在页面 `hideEvent` 停、`showEvent` 启。

### M9. 版本号解析对预发布失效

- **文件**：`ydlna/updater.py:62-71`
- **状态**：`[ ]`
- **问题**：`parse_version("0.1.2-rc1")` 正则只匹配开头 `(\d+)\.(\d+)\.(\d+)`，返回 `(0,1,2)`，
    与正式版相等，rc 用户收不到正式版更新。
- **建议**：解析预发布后缀并参与比较，或用 `packaging.version.Version`。

### M10. Python 版本声明矛盾

- **文件**：`README.md:32`（3.10~3.12）vs `ydlna/updater.py:320`（用了 `asyncio.TaskGroup`，3.11+）
- **状态**：`[ ]`
- **问题**：代码用了 `asyncio.TaskGroup`（3.11 新增），**3.10 用户触发自动更新会崩**。
- **建议**：把 README 下限提到 3.11；CI 在声明的每个版本上跑测试。

### M11. 依赖无锁文件，下界对应有已修 CVE 的旧小版本

- **文件**：`requirements.txt`
- **状态**：`[ ]`
- **问题**：全 `>=` 开放，无锁文件，CI 与本地版本不一致；`Pillow>=10` / `aiohttp>=3.9` 下界
    对应有已修 CVE 的旧小版本。
- **建议**：用 `pip-compile` 生成锁文件用于 CI / 打包；或至少给每个依赖加上经验证的上界；
  定期 `pip-audit` / Dependabot。

### M12. `PySide6<6.12` 上限偏低

- **文件**：`requirements.txt:2`
- **状态**：`[ ]`
- **问题**：PySide6 已发布 6.9.x 系列，`<6.12` 会挡住未来约一年的兼容版本，对「源码运行」用户
    造成升级后装不上的困惑。未发现代码中对 6.12 的实际不兼容说明。
- **建议**：放宽到 `>=6.6,<7`，或写明为何卡 6.12 的具体不兼容项。

### M13. 几何恢复不校验

- **文件**：`ydlna/ui/main_window.py:86-94`
- **状态**：`[ ]`
- **问题**：直接信任保存的 `[x,y,w,h]`，拔副屏后窗口可能恢复到屏幕外「看不见」。
- **建议**：恢复前用 `QScreen.availableGeometry()` 校验窗口至少有部分在可用区内，否则丢弃。

### M14. 设备名 / 端口输入校验弱

- **文件**：`ydlna/ui/settings_interface.py:337-345`
- **状态**：`[ ]`
- **问题**：`friendly_name` 仅 strip（含 `<>` 会破坏 DLNA XML）；端口非法输入静默丢弃，且无
    「需重启服务」提示。
- **建议**：限制 friendly_name 长度（如 32 字符），过滤 XML 非法字符或保证 XML 转义；端口非法
  时 InfoBar 提示并告知需重启服务。

### M15. 托盘 tooltip 不复位

- **文件**：`ydlna/ui/tray.py`（`_on_state_changed`）
- **状态**：`[ ]`
- **问题**：播放结束后 tooltip 仍显示「正在播放: xxx」，只在 `_retranslate` 里按 idle 复位。
- **建议**：`_on_state_changed` 里 state==idle 时复位 tooltip。

### M16. `home_interface.show_info` 的 `is_warning` 参数失效（实打实 bug）

- **文件**：`ydlna/ui/home_interface.py:175-185`
- **状态**：`[ ]`
- **问题**：计算了 `kind = InfoBarIcon.WARNING if is_warning else InfoBarIcon.INFORMATION`，但
    **没传给 `InfoBar.show`**，警告与信息提示视觉无差别。
- **建议**：`InfoBar.show(..., icon=kind, ...)`。

### M17. CI 第三方 Action 未钉 SHA

- **文件**：`.github/workflows/release.yml`
- **状态**：`[ ]`
- **问题**：`actions/checkout@v5` 等用浮动标签，理论上可被 tag 重指向利用。
- **建议**：钉 commit SHA；release job 加环境保护 / 手动确认。

### M18. `toggle_service` 异常无人 await，UI 按钮卡中间态

- **文件**：`ydlna/app.py:134-149`
- **状态**：`[ ]`
- **问题**：`server.async_start()` / `async_stop` 若抛异常，task 内异常无人 await 变成
    `Task exception was never retrieved`，`set_service_running` 不执行。
- **建议**：给 `toggle_service` 加 try / except / finally，finally 里更新 UI 按钮状态。

### M19. `_rendering_control` 的 SelectPresets SCPD 与规范不符

- **文件**：`ydlna/dlna/rendering_control.py:136-143`
- **状态**：`[ ]`
- **问题**：in_args 只声明 `InstanceID`，规范要求 `InstanceID` + `CurrentPresetName`。
    函数名 `select_presets` 与 action 名 `SelectPresets` 单复数不一致。
- **建议**：补 `CurrentPresetName` 参数（类型 `A_ARG_TYPE_PresetName`，需新增该 state var）。

### M20. `RendererBridge.shutdown` 未断开信号、未清 service 引用

- **文件**：`ydlna/dlna/renderer_bridge.py:133, 324-325`
- **状态**：`[ ]`
- **问题**：`shutdown` 只 `_stop_polling()`，没断 `stateChanged` 连接，也没清 `_avt / _rc / _cm`
    引用。重启 server 不重启 player 时，`_on_player_state` 会写到已 stop 的旧 service 上。
- **建议**：`shutdown` 里 `disconnect` 信号，把 `_avt / _rc / _cm` 置 None。

---

## 五、低级问题（Low）

> 这些不影响功能和安全，但影响可维护性和整洁度，有空随手改。

### L1. 951 行单文件职责过多

- **文件**：`ydlna/player/hls_rewriter.py`
- **状态**：`[ ]`
- **问题**：混了 AVI 构造 / 内容探测 / HTTP 基类 / HLS 代理 / 直链代理 / 入口函数 5 类职责。
- **建议**：拆为 `hls/avi_builder.py` / `hls/probe.py` / `hls/_base.py` / `hls/hls_proxy.py` /
  `hls/direct_proxy.py`。

### L2. 5 处重复的「过滤客户端 header」代码块

- **文件**：`hls_rewriter.py:339-341, 485-487, 525-527, 613-615, 678-680`
- **状态**：`[ ]`
- **建议**：抽 `_client_headers(request, *, keep_range=False)` 公共方法。

### L3. 魔数散落

- **位置**：`control_bar.py`（`_hide_timer=3000`）、`player_interface.py`（`_anchor=150`、spinner
  `30/12`）、`ssdp_listener.py`（`CACHE-CONTROL max-age=1900`）、`renderer_bridge.py:295`
  （`asyncio.sleep(1.0)` 轮询周期）等。
- **状态**：`[ ]`
- **建议**：提取为模块级常量。

### L4. `_net.py` 用 8.8.8.8 探测默认路由

- **文件**：`ydlna/dlna/_net.py:77-87`
- **状态**：`[ ]`
- **问题**：中国网络下 8.8.8.8 常不可达，回退到 127.0.0.1；另硬编码 ICS 网段 `192.168.137.1`
  （`:70-71`）。
- **建议**：改用私有测试段地址（如 `192.0.2.1`，不真发包），或遍历 `list_local_ips` 选首个非回环。

### L5. 日志泄漏：媒体 URL / 本机 IP

- **文件**：`hls_rewriter.py`（多处 `log.info("...: %s", url)`）；`renderer_bridge.py:161, 175, 191`；
  `ssdp_listener.py:163-166, 253`；`server.py:147-150`
- **状态**：`[ ]`
- **问题**：媒体 URL 可能含 token / 基本认证凭据；本机 / 手机 IP 会写入 `lightcast.log`，
    用户分享日志排障时无意泄露。
- **建议**：加 `_redact(url)` 脱敏（剥离 `user:pass@`，把 `token/sig/auth/key/sign/expires` 打码）；
  IP 部分掩码。

### L6. 日志 M-SEARCH 无速率限制 / 去重

- **文件**：`ydlna/dlna/ssdp_listener.py:237-259`
- **状态**：`[ ]`
- **问题**：`ssdp:all` 时每个 ST 都打一条 info，设备密集网络会刷爆日志；SSDP 可被用作小规模
    UDP 反射源。
- **建议**：M-SEARCH 日志降 debug 或按来源去重；对单来源限速。

### L7. 国际化遗漏

- **位置**：`home_interface.py:196`（`"轻投"` fallback）、`app.py` 多处（config 默认值、
  `toggle_service`）。
- **状态**：`[ ]`
- **问题**：英文环境下 fallback 友好名会是中文。
- **建议**：fallback 用 `tr("app.default_name")` 或英文 `LightCast`。

### L8. 死代码 / 死参数

- **文件**：`avtransport.py:32`（`_seconds_to_str` 无人调用）；`connection_manager.py:65`
  （`bridge` 属性从未注入）；`ssdp_listener.py`（`_reply_msearch` 的 `raw` 参数未使用）。
- **状态**：`[ ]`
- **建议**：删除，或补注入（`renderer_bridge.set_services` 漏了 `cm.bridge = self`）。

### L9. `_Spinner` 定时器 + `Image.open` 未 close

- **文件**：`player_interface.py:91-117`；`hls_rewriter.py:631, 704`
- **状态**：`[ ]`
- **问题**：`_Spinner` 定时器隐藏时仍跑（见 M8）；`Image.open` 懒加载持有底层文件直到 GC。
- **建议**：`convert("RGB")` 后 `img.close()`。

### L10. 时间标签无零填充，UI 宽度跳动

- **文件**：`ydlna/player/control_bar.py:60-66`
- **状态**：`[ ]`
- **问题**：`f"{h}:{m:02d}:{s:02d}"`，长视频「1:00:00」→「10:00:00」宽度变化撑宽布局。
- **建议**：`setMinimumWidth` 调大并右对齐，或 h 也补零。

### L11. 音量滑块 `_on_volume` 可能双向回环

- **文件**：`ydlna/player/control_bar.py:199, 292-293`
- **状态**：`[ ]`
- **问题**：mpv volume 是 float、滑块是 int，边界值（如 80.5→80）可能抖动一次。
- **建议**：`_on_volume` 里 `blockSignals(True)` 再 setValue。

### L12. `_serve_cached` Range 解析不完整

- **文件**：`ydlna/player/hls_rewriter.py:716-741`
- **状态**：`[ ]`
- **问题**：不支持后缀范围 `bytes=-500`；`bytes=100-50` 会产生非法 206 响应。
- **建议**：校验 `s <= e` 否则 416；按规范处理后缀范围。

### L13. `TrayIcon` 重复持有 parent

- **文件**：`ydlna/ui/tray.py:25`
- **状态**：`[ ]`
- **问题**：`self._parent = parent` 与 `QSystemTrayIcon(parent)` 重复。
- **建议**：删 `self._parent`，用 `self.parent()`。

### L14. `RenderingControlService` 接受任意 Channel 改 Master

- **文件**：`ydlna/dlna/rendering_control.py:43-47, 83-91`
- **状态**：`[ ]`
- **问题**：SCPD 声明支持 13 个声道，但 `set_volume` 不检查 Channel，任何声道都改同一个
    `Volume` 状态变量。
- **建议**：`if Channel != "Master": return`。

### L15. monkey-patch UpnpServer 是修改库的全局类

- **文件**：`ydlna/dlna/server.py:39-75`
- **状态**：`[ ]`
- **问题**：库升级若改了 `async_start` / `_async_start_ssdp` / `_create_device` / `base_uri` 的
    内部结构，patch 会静默失效。
- **建议**：尽量用子类化重写 `async_start` 替代 monkey-patch；加库版本断言。

### L16. CI 无版本钉的 `innosetup`

- **文件**：`.github/workflows/release.yml:97`
- **状态**：`[ ]`
- **建议**：钉版本或缓存。

### L17. 局部导入风格不一致

- **位置**：`main_window.py:76, 215`、`player_interface.py:185` 等
- **状态**：`[ ]`
- **建议**：统一顶部导入；若为避免循环依赖，加注释说明。

---

## 六、做得好的地方（应保持）

- `constants.py:21-39` —— PATH 临时修改（而非 `add_dll_directory`）避免 libmpv 污染 Qt6 dll 解析，深思熟虑。
- `renderer_bridge.py:18` —— 用 **defusedxml** 解析 DIDL-Lite，挡住 XXE / 实体爆炸。
- `hls_rewriter.py:266` —— 代理只绑 `127.0.0.1`；`_read_capped` 96MB 上限正确处理 `CancelledError`；
  `_find_ts_offset` 要求 4 个连续 `0x47` 同步字节；主播放列表递归限深 3；密钥长度校验 16 字节；
  HTML 登录墙检测。
- `autostart.py` —— 写注册表路径 / 值名安全，无注入。
- `LightCast.spec` —— 高质量：`collect_all("qfluentwidgets")`、显式排除 PyQt5/6 和无关 ML 栈
  （避免 4.9GB 膨胀）、`upx=False`、libmpv 路径与代码一致。
- 打包后配置 / 日志重定向 `%APPDATA%\LightCast`；日志 1MB×3 轮转合理。
- 应用级按键过滤器解决焦点问题、单击 / 双击消歧定时器、系统触发关闭直接退出避免安装器死锁
  ——git 历史印证了这些打磨。

---

## 七、修复优先级

| 优先级 | 条目 | 一句话 |
|---|---|---|
| **立即** | C1 + H3 | 自动更新强制 SHA-256 校验（锚点走 GitHub 直连）；顺手修掉 base_uri patch 死代码 |
| **立即** | C2 + H1 | 代理加 scheme 白名单 + 私网 IP 黑名单 + 关重定向 + m3u8/key/probe 加大小上限 + PIL `MAX_IMAGE_PIXELS` |
| **近期** | C3 + H6 | 建立纯函数测试骨架（updater / hls_rewriter / config）+ 持有 task 引用并退出取消 |
| **近期** | H4 + H5 | SSDP 线程 join / 加锁；mpv 回调状态加锁 + shutdown barrier |
| **近期** | H7 | 文档明示公共网络风险，提供「仅可信网络 / 首次确认」开关 |
| **顺带** | 中 / 低 | 日志 URL 脱敏、统一 Python 版本声明、锁依赖、拆分大文件、修两个 UI 小 bug（M16 / M15）|

---

## 八、核验说明（对子审查结论的交叉核实）

本次审查由 4 个并行子审查（DLNA 网络栈 / HLS 代理 / 播放器 UI / 测试构建）完成。其中
**DLNA 审查的 H3（base_uri patch）与 HLS 审查存在分歧且最关键**，已人工核对
`async_upnp_client/server.py` 库源码：

- 确认 `UpnpServerDevice` 的属性是 `self.base_uri`（无下划线），patch 写的
  `self._device._base_uri` 确实是**无效属性赋值**（真实代码缺陷）。
- 但纠正了「导致 device.xml LOCATION 仍是 0.0.0.0、手机连不上」的**过度影响推断**：
  device.xml 内 `controlURL` 等是**相对路径**（库 920-922 行用 `service.control_url`），
  SSDP LOCATION 由我们独立的 `SsdpListener` 用真实 IP 生成，与该 patch 无关——故投屏功能仍正常，
  这只是一段半失效死代码而非致命 bug。

其余子审查结论均已交叉印证一致（SSRF、自动更新无校验、零测试、task 未持有引用、mpv 回调线程
安全等），如实呈现于上。
