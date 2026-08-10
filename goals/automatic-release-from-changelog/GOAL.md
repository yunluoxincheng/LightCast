Goal: 在 LightCast 中实现“合并到 master 后按 CHANGELOG 自动打标签并发布”。除非全部验收标准满足，否则不要宣称完成。

## 1. 项目背景

- 项目名称：轻投（LightCast）。
- 项目类型：Windows 10/11 桌面 DLNA 投屏接收软件。
- 技术栈：
  - 应用：Python 3.11、PySide6、qasync、aiohttp、libmpv。
  - 测试：pytest，GitHub Actions Windows runner。
  - 打包：PyInstaller、Inno Setup、7-Zip。
  - 发布：GitHub Actions、GitHub Release、lightweight `v*` tag。
  - 数据库/中间件：无。
- 现有能力：
  - `.github/workflows/release.yml` 在 PR 上运行测试，在 `v*` tag 上测试、构建、生成 SHA256SUMS 并发布 Release。
  - `workflow_dispatch` 可手动构建产物但不发布。
  - tag 版本会在构建时写入 `ydlna/__init__.py`，无需提交运行时版本号修改。
  - Release 正文由 `CHANGELOG.md` 对应版本小节提取。
- 当前发布约定：每个功能/修复 PR 必须在合并前增加新的 CHANGELOG 版本小节；合并即表示批准该版本发布。

## 2. 目标

扩展现有发布流水线，使任何进入 `master` 的已批准变更在测试和全部产物构建成功后，自动读取 `CHANGELOG.md` 顶部稳定版本、创建指向该 master 提交的 lightweight tag，并使用完整 CHANGELOG 小节发布 GitHub Release，不再依赖人工记得打 tag。

## 3. 范围

包含：

1. 让现有发布 workflow 同时响应 `master` push、`v*` tag push、PR 和手动触发。
2. 在 `master` push 场景严格解析 `CHANGELOG.md` 顶部 `X.Y.Z` 稳定版本。
3. 在测试和安装包/便携版/SHA256SUMS/Release notes 全部生成成功后才创建 tag。
4. 在同一 workflow run 中发布 Release，不依赖 tag push 再触发另一个 workflow。
5. 处理 tag/release 已存在、构建失败后重跑、版本未递增和并发发布。
6. 保留现有手动 tag 发布和 `workflow_dispatch` 手动构建能力。
7. 为版本提取和状态判定补充可在 pytest 中执行的自动化测试。
8. 更新 CHANGELOG 和发布文档。

不包含：

1. 不实现自动 major/minor/patch 计算；版本号仍由 PR 作者写入 CHANGELOG。
2. 不自动支持 `rc`、`beta`、`alpha` 等预发布版本；自动路径仅接受 `X.Y.Z`。
3. 不新增 GitHub App、PAT 或仓库 secret。
4. 不改变应用更新器、安装包格式、资产命名或 SHA-256 信任模型。
5. 不删除或移动任何已存在的 tag/Release。
6. 不实现 GitHub Environment 人工审批；合并到 `master` 本身就是发布批准。

## 4. 相关目录

- 主要 workflow：`.github/workflows/release.yml`
- 可新增的纯逻辑辅助脚本：`tools/`（优先 Python，不能引入新依赖）
- 自动化测试：`tests/`
- 发布说明：`CHANGELOG.md`
- 开发/发布文档：`docs/DEVELOPMENT.md`
- 版本同步目标：`ydlna/__init__.py`（仅构建工作区临时改写，不提交）
- 不应修改：播放器、DLNA、UI、更新器业务代码以及无关 workflow。

## 5. 业务规则

1. `master` 的每次 push 都是一个自动发布候选；PR merge 和极少数直接 push 采用相同规则。
2. 自动发布版本必须来自 CHANGELOG 第一个版本小节，标题格式必须严格为 `## [X.Y.Z] - YYYY-MM-DD`。
3. 自动路径只允许稳定 SemVer 三段数字，不能接受前导 `v`、缺段、额外后缀或任意自由文本。
4. CHANGELOG 对应版本小节必须存在且非空；禁止回退到“更新内容见 CHANGELOG.md”占位文案。
5. 合并到 master 即表示发布获批，不需要第二次人工确认。
6. 自动 tag 必须是 `vX.Y.Z`，必须指向触发该 run 的精确 `github.sha`，并保持 lightweight tag 格式。
7. 测试、PyInstaller、便携包、安装包、SHA256SUMS 和 Release notes 任一失败时不得创建新 tag。
8. tag 创建成功后必须在同一 run 中发布 Release；默认 `GITHUB_TOKEN` 创建的 tag 不作为启动第二条发布 run 的依据。
9. Release 标题保持 `轻投 LightCast vX.Y.Z`，正文必须等于该版本的完整 CHANGELOG 小节。
10. Release 资产名称和内容保持：
    - `LightCast-Setup-X.Y.Z.exe`
    - `LightCast-Portable-X.Y.Z.zip`
    - `SHA256SUMS.txt`
11. PR 事件仍只运行 test，build/release 必须 skipped。
12. `workflow_dispatch` 的非 tag 手动触发仍只构建并上传 Actions artifact，不创建 tag 或 Release。
13. 人工推送 `v*` tag 的现有发布入口必须继续工作。

## 6. 前端要求

不适用。该功能没有应用界面或用户交互页面；用户可见结果仅为 GitHub Actions 状态、tag、Release 正文和下载资产。

## 7. 后端/API 要求

不新增应用 HTTP API。

GitHub Actions 行为要求：

1. `release.yml` 增加 `push.branches: [master]`，同时保留 `push.tags: ["v*"]`、`pull_request` 和 `workflow_dispatch`。
2. 版本解析逻辑必须区分事件：
   - PR：只测试，不解析/发布。
   - master push：从 CHANGELOG 顶部解析稳定版。
   - tag push：从 tag 去掉前导 `v`，保留现有手动发布能力。
   - workflow_dispatch 非 tag：保留手动只构建行为。
3. master 自动路径应使用仓库 `GITHUB_TOKEN`，只授予所需的 `contents: write`；不使用长期凭据。
4. 创建 tag 和 `gh release create` 必须显式指定同一个 `vX.Y.Z`。
5. 建议把可测试的 CHANGELOG 解析/状态分类提取到 `tools/` 下的纯 Python 辅助模块，并让 workflow 调用同一实现，避免测试与生产逻辑分叉。

## 8. 数据库要求

不适用。本功能不新增数据库、表、索引或持久化应用数据；持久状态由 Git tag 和 GitHub Release 表示。

## 9. 状态机

状态：

1. `candidate`：收到 master push、tag push 或 workflow_dispatch。
2. `tests_passed`：完整 pytest 成功。
3. `version_validated`：版本与 CHANGELOG notes 已验证；发布 job 取得 concurrency 锁后，远端 tag/release 最新状态也已验证。
4. `building`：正在构建 exe、便携包、安装包、校验和和 notes。
5. `artifacts_ready`：全部发布产物已生成并验证。
6. `tag_created`：自动路径已创建 lightweight tag；手动 tag 路径视为已有 tag。
7. `released`：Release 和三项资产上传完成。
8. `already_released`：同一 tag 已指向当前提交且对应正式 Release 已存在，幂等成功。
9. `draft_recovery`：同一 tag 已指向当前提交，但上次资产上传中断留下 draft Release，正在覆盖补齐资产并正式发布。
10. `manual_artifact_ready`：非 tag workflow_dispatch 只上传 Actions artifact。
11. `failed`：任一步骤失败。

初始状态：`candidate`。

终止状态：`released`、`already_released`、`manual_artifact_ready`、`failed`。

允许转换：

- `candidate → tests_passed`
- `tests_passed → version_validated`
- `version_validated → already_released`
- `version_validated → draft_recovery`
- `version_validated → building`
- `building → artifacts_ready`
- `artifacts_ready → tag_created`（master 自动路径）
- `artifacts_ready → released`（已有且正确的 tag 恢复路径）
- `tag_created → released`
- `draft_recovery → released`
- `artifacts_ready → manual_artifact_ready`（非 tag workflow_dispatch）
- 任意非终止状态在校验或命令失败时 `→ failed`

禁止转换：

- `candidate/version_validated/building → tag_created`：产物未全部成功前禁止打 tag。
- `failed → 自动删除或移动 tag`：失败不得执行破坏性回滚。
- tag 指向其他 commit 时 `version_validated → building`：必须失败，不能覆盖 tag。
- `already_released/released → 再次创建 Release`：必须幂等 no-op。

转换触发和副作用：

- `artifacts_ready → tag_created`：workflow 使用 `GITHUB_TOKEN` 创建 `vX.Y.Z` 指向 `github.sha`。
- `tag_created/artifacts_ready → released`：`gh release create` 上传三项资产并使用 notes 文件。
- tag 已指向当前提交但 Release 不存在：属于恢复路径，允许重建产物后发布，不重新创建 tag。
- tag 已指向当前提交但 Release 为 draft：固定 Release 数字 ID，重建产物并在覆盖前再次确认同 tag 且仍为 draft，使用 `gh release upload --clobber` 覆盖三项预期资产，再修正标题/正文并执行 `gh release edit --draft=false --verify-tag` 正式发布。

## 10. 错误处理要求

1. CHANGELOG 顶部无合法版本：workflow 明确失败并说明预期格式。
2. 顶部版本含 rc/beta/其他后缀：自动路径失败，提示改为稳定版或使用人工发布流程。
3. 对应 CHANGELOG 小节缺失/为空：失败；不得使用占位 release notes。
4. `vX.Y.Z` 不存在：正常新版本路径。
5. tag 已存在且指向当前提交：
   - 正式 Release 已存在：幂等成功，不重复上传。
   - Release 不存在：进入恢复构建/发布路径。
   - Release 为 draft：进入 draft 恢复路径，覆盖预期资产后正式发布。
6. tag 已存在但指向其他提交：失败并提示必须提升 CHANGELOG 版本；绝不移动或覆盖 tag。
7. tag 创建遇到并发冲突：重新读取 tag；若指向当前提交则继续，否则失败。
8. 测试/下载 libmpv/打包/校验和/notes 生成失败：失败且不得创建 tag。
9. tag 创建后 Release 发布失败：保留 tag；重新运行同一 workflow 时必须能恢复发布。
10. Release 资产缺失：发布命令前失败，不能发布不完整 Release；draft 恢复同样必须先验证本地三项资产。
11. GitHub API/网络暂时失败：让 job 失败并保留可重跑性，不吞异常。

## 11. 并发与一致性

- 一致性边界：一个 release run 的版本、tag、SHA256SUMS、资产名和 Release notes 必须全部使用同一个解析出的 `X.Y.Z` 与同一个源提交 SHA。
- 幂等键：`tag name + target commit SHA`。
- 发布串行化：build/release job 使用固定 concurrency group（例如 `lightcast-release`）、`queue: max` 和 `cancel-in-progress: false`，使多个快速 master push 的发布阶段进入多任务等待队列、顺序执行且不互相取消；tag SHA、Release 缺失/draft/正式状态只能在 job 取得该锁后查询和决策。
- PR test 不应被发布 concurrency group 串行阻塞；concurrency 应放在 build/release job，而不是整个 workflow。
- tag 创建使用 Git/GitHub 的原子 ref 创建；不得先删除再创建。
- Release 创建和 draft 正式发布都必须使用 `--verify-tag`，且此前再次核验远端 tag 的解析后 commit SHA 等于 `github.sha`；禁止 GitHub CLI 隐式创建标签。
- 需要防御的竞态：
  1. 两个 run 同时判断 tag 不存在并尝试创建。
  2. 第一个 run 创建 tag 后、发布 Release 前失败，第二次重跑接管。
  3. 后续 master push 忘记提升 CHANGELOG，企图复用旧 tag。
  4. 人工 tag 发布与 master 自动发布同时针对同一版本。
- 冲突解决：创建失败后重新读取 tag；仅当目标 SHA 相同才继续，否则失败。

## 12. 权限与安全

- 不需要应用登录或角色系统。
- workflow 最小权限：测试 job `contents: read`；自动 tag/Release 所在 job `contents: write`。
- 不申请 `pull-requests: write`、`actions: write` 或其他无关权限。
- 不新增 PAT、GitHub App key 或 repository secret；使用短期 `GITHUB_TOKEN`。
- 不使用 `pull_request_target`，不在写权限上下文执行未经合并的 PR 代码。
- 只有受保护的 `master` push 才可进入自动发布路径。
- Release 正文来自仓库内已审查的 CHANGELOG，不拼接外部不可信输入。
- 日志不得输出 token、授权 header 或其他凭据。
- 官方行为依据：`GITHUB_TOKEN` 产生的普通 push 不会递归触发 workflow，因此自动路径必须在当前 run 中完成发布，而不是依赖新 tag 的 push run。

## 13. 工程约束

- 保持现有 PowerShell/Windows workflow 风格和 UTF-8 文档编码。
- 优先修改现有 `release.yml`，不要为了链式触发新增第二套重复构建 workflow。
- 不升级 Actions、Python 依赖、PyInstaller、Inno Setup 或应用依赖版本。
- 不引入第三方 Action 来解析版本或自动发 Release；使用仓库代码、GitHub CLI 和官方 actions。
- 保留当前资产命名、版本同步、SHA256SUMS 和 updater 信任边界。
- 所有 workflow 条件必须显式覆盖 PR、master push、tag push、workflow_dispatch。
- 修改前阅读 `CHANGELOG.md`、`docs/DEVELOPMENT.md` 和完整 `release.yml`。
- 实现 PR 必须先更新 CHANGELOG 的下一稳定版本；该 PR 合并后应由新 workflow 自动发布自身版本。
- 上线前置条件：确认自动化 PR 合并前的当前版本（目前为 0.1.23）已经人工打 tag 并成功发布，避免版本号跳过。

## 14. 执行流程

1. 同步最新 master，确认当前 tag、Release 和 CHANGELOG 顶部版本。
2. 若当前顶部版本尚未发布，先报告并要求/执行已获授权的人工发布，再开发自动化。
3. 创建独立 `codex/` 分支。
4. 阅读并画出当前 `release.yml` 各事件分支。
5. 实现可测试的版本/notes 解析与发布状态分类逻辑。
6. 扩展 workflow 的 master push 触发和事件条件。
7. 确保 test 成功后才进入 build；确保 artifacts ready 后才创建 tag。
8. 实现 tag/release 幂等、冲突和恢复语义。
9. 更新 CHANGELOG、CODE_REVIEW（如适用）和 `docs/DEVELOPMENT.md`。
10. 运行 pytest、语法/格式检查和本地解析测试。
11. 提交、推送并创建非 Draft PR，等待审查后才合并。
12. 合并后监控首个自动版本的 test、build、tag、Release、正文和三项资产直到完成。

## 15. 验收标准

必须全部满足：

1. PR 事件仍只运行 test，build skipped。
2. master 合并且 CHANGELOG 顶部为未发布 `X.Y.Z` 时，完整测试和构建成功后自动创建 `vX.Y.Z`。
3. 自动 tag 指向触发 master push 的精确 merge commit，且为 lightweight tag。
4. 自动流程在同一 run 内创建正式 Release，不依赖 tag push 启动第二个 run。
5. Release 标题、正文、安装包、便携包和 SHA256SUMS 均使用同一个版本。
6. Release 正文与 CHANGELOG 对应版本小节完整一致，无占位文案。
7. 测试或 tag 创建前的任一构建/验证步骤失败时远端不存在新 tag 和 Release；tag 创建后的资产上传失败允许留下同 SHA tag 与 draft Release，但重跑必须进入可恢复路径，不能误判完成。
8. tag 已指向当前提交且正式 Release 已存在时，重跑安全成功且不重复创建资产/Release。
9. tag 已指向当前提交但 Release 缺失时，重跑可以恢复发布。
10. tag 已指向当前提交但 Release 为 draft 时，重跑可以覆盖补齐三项资产并正式发布。
11. tag 已指向其他提交时 workflow 失败且不移动 tag。
12. master 新提交忘记提升 CHANGELOG 版本时 workflow 明确失败。
13. 多个发布 run 使用 `queue: max` 保留等待任务，不会因默认的 single pending 规则互相替换取消；取得锁后重新读取远端状态，前一任务已发布时后一任务幂等结束。
14. 手动 `v*` tag 发布仍正常。
15. 非 tag workflow_dispatch 仍只构建/上传 artifact，不发布。
16. 自动路径拒绝预发布版本格式，稳定 tag 对应非 draft prerelease 时也拒绝自动覆盖。
17. 不需要新增 secret，权限保持最小化。
18. 完整 pytest、workflow 相关测试和 `git diff --check` 通过。
19. 首次真实自动发布完成后，GitHub 上可核验 tag、Release 正文和三项资产。

## 16. 测试要求

1. 单元测试 CHANGELOG 顶部稳定版本提取。
2. 单元测试正文提取在下一版本标题前结束。
3. 测试缺失标题、格式错误、空 notes、预发布后缀被拒绝。
4. 测试版本/tag 状态分类：不存在、同 SHA+正式 Release、同 SHA+无 Release、同 SHA+draft、prerelease 和不同 SHA。
5. 测试资产上传中断留下 draft 后，下一次状态判定进入恢复而不是已发布；测试等待任务取得锁后使用最新正式状态幂等结束。
6. 测试超长/异常版本文本不会被接受或执行为 shell 内容。
7. 验证 PR workflow run：test success、build skipped。
8. 在 PR 中通过纯逻辑测试验证 master 自动分支；禁止在 PR 测试中真的创建 tag/Release。
9. 合并后的首次真实演练：
   - 观察 master push workflow；
   - 核对 test/build 成功；
   - 核对 tag SHA；
   - 核对 Release 不是 draft/prerelease；
   - 核对三项资产存在；
   - 核对 Release body 等于 CHANGELOG 小节。
10. 失败演练应使用纯逻辑测试或安全的 dry-run，不得创建垃圾 tag。
11. 若 workflow YAML 缺少本地 schema 工具，至少执行 `git diff --check`、pytest，并通过 GitHub PR run 验证语法可加载。

## 17. 禁止操作

- 禁止在测试/构建完成前创建 tag。
- 禁止 force push、移动、覆盖或删除已有 tag。
- 禁止在版本冲突时自动增加版本号。
- 禁止使用占位 Release notes。
- 禁止使用 PAT 或要求用户新增长期 secret。
- 禁止使用 `pull_request_target` 执行 PR 内容。
- 禁止让自动 tag push 递归触发第二次完整发布并产生重复 Release。
- 禁止取消正在进行的较早发布 run。
- 禁止修改无关应用代码、依赖或资产命名。
- 禁止吞掉 GitHub CLI、打包或上传错误。
- 禁止在没有真实验证 tag/Release/资产前宣称自动发布完成。

## 18. 最终输出要求

完成实现后必须输出：

1. 修改文件清单及每个文件的用途。
2. 最终 workflow 的事件矩阵（PR/master/tag/manual）。
3. 版本解析、tag 冲突、幂等与恢复语义。
4. 权限清单以及为何不需要 PAT/App secret。
5. 本地和 GitHub Actions 测试结果。
6. 自动发布产生的 tag、merge SHA、Release URL、标题和资产列表。
7. Release 正文与 CHANGELOG 一致性的核验结果。
8. 任何剩余风险或建议的后续增强。

## 19. 交付物

- `goals/automatic-release-from-changelog/GOAL.md`：本 Goal。
- 修改后的 `.github/workflows/release.yml`。
- 必要的 `tools/` 版本解析辅助脚本及对应 pytest。
- 更新后的 `CHANGELOG.md` 与 `docs/DEVELOPMENT.md`。
- 一个独立、可审查、非 Draft 的实现 PR。
- 合并后成功完成的首次自动 tag 与 GitHub Release。
