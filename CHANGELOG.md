# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [3.0.0] - 2026-10-19

**Windows 版变成真正的"拷到哪都能跑"的单文件程序；服务端版从"授权工具"
变成"日常入口"，一条命令管完授权与签到。**

### 变更（对用户可见）

- **数据目录从 exe 旁边挪到用户缓存目录** `%LOCALAPPDATA%\WorkBuddyCheckin`
  - exe 放桌面、U 盘、只读目录都能正常读写配置
  - 把 exe 发给别人不会连 `config.json`（机器人 key）和 `auth_oauth.json`
    （明文令牌）一起带走
  - 首次运行会把旧版留在 exe 旁边的配置与令牌**复制**过来（不删源文件，
    不覆盖已有配置）
  - 界面日志首行显示"程序位置"与"数据目录（用户缓存目录）"
- **计划任务指向 exe 自身**（`WorkBuddyCheckin.exe --run`），并在启动时自愈：
  exe 换目录、从旧版升级后任务指向失效，打开一次程序即自动改指当前路径。
  源码运行不参与自愈，避免开发动作抢走已部署的任务。
- **服务端脚本 `wb_auth_server.py` → `wb_server.py`**，默认行为改为完整日常流程：
  检查授权 → 需要时推授权链接并等 180 秒 → 失败按 60/120 秒指数退避重试，
  最多 3 轮 → 授权成功后立刻签到 → 推送结果。3 轮全失败推失败通知、退出码 `2`。
- **配置跟着脚本走**：服务端默认读脚本同目录的 `config.json`，日志与令牌
  也写在同一层，整个目录拷走即可换机器。
- **产物只出英文名** `dist/WorkBuddyCheckin.exe`，不再生成中文名副本；
  dist 里旧的中文名 exe 会在打包时清掉。
- 新增版本管理：`wb_config.VERSION` 为唯一来源，`--version` 输出
  版本 / 运行方式 / 数据目录，打包时写入 exe 文件属性。

### 新增

- 配置项 `auth_wait_seconds`（180）、`auth_attempts`（3）、
  `auth_backoff_seconds`（60），可用 `--wait` / `--attempts` / `--backoff` 覆盖
- `wb_server.py --auth-only` / `--checkin-only` / `--version`
- `--check` 增加授权状态一行（未授权时说明下次运行会自动发起授权）
- `docs/server.md` 按"cron 日常入口"重写：退避时间线、cron 示例、干净环境验证
- 回归从 52 项扩到 **77 项**：新增版本与数据目录、迁移、服务端参数、
  服务端打包闭包与平铺目录断言

### 新增

- **服务端打包脚本 `build/make_server_bundle.py`**：服务端部署不再需要
  "把整个 `src/` 拷过去"。脚本从 `wb_server.py` 出发按 import 闭包（AST
  静态解析模块级导入）自动收集，产出 `dist/server/`：
  `wb_server.py`、`workbuddy_checkin.py`、`wb_oauth.py`、`wb_notify.py`、
  `wb_config.py` + `config.example.json` + `README.txt` + `_bundle.json`。
  `checkin_gui.py`（tkinter 界面）、`wb_task.py`（Windows 计划任务）、
  `win_quiet.py`（弹窗抑制）不会被带上；万一以后被 import 进来，脚本直接报错。
  只清理自己上次放进去的文件，不碰 `config.json` / `auth_oauth.json` /
  `checkin.log`，打完自动跑一次 `--version` 冒烟。

### 删除

- 服务端一次性领取端口（`--handoff` / `--port` / `--bind` /
  `--handoff-timeout`）与客户端 `--pull-token`
  —— 两个版本各自授权、各存一份，不再需要跨机器交接令牌
- 服务端 `--save-token` / `--timeout`（由 `auth_wait_seconds` 与 `--wait` 取代）
- 中文名 exe 产物与相关文案

### 修正

- 服务端单独调用内部函数时，令牌可能写到当前工作目录而不是配置目录
  （现在每个入口都先绑定数据目录）
- **服务端平铺目录的数据目录不再是"父目录"**：`default_data_dir()` 之前假定
  "源码运行 = 仓库"，把 `src/` 的上一级当数据目录；服务端把 5 个脚本平铺在
  `/opt/wbcheckin` 时，配置会被写到 `/opt/`。现在用 `is_repo_layout()` 判据
  （`run.py` / `.git` / `build/`）区分，平铺目录的数据目录就是脚本目录，
  `--version` 显示「脚本运行」以示区别
- 界面"每日定时"的路径校验改看"命令 + 参数"，之前只比程序路径

## [2.2] - 2026-10-19

**界面收口（未授权即锁死）、到期提醒、服务端用法补全，顺手清掉一批遗留文件。**

### 新增

- **授权到期提醒**：每次签到结束后检查令牌剩余天数，`≤ expiry_warn_days`
  （默认 2 天）就向已配置渠道推一条提醒；已过期时文案直接说"授权已过期"。
  定时任务（`--run`）同样生效，不额外增加任何调度。
- 配置项 `expiry_warn_days`（默认 `2`，设 `0` 关闭；界面不暴露）
- 界面横幅：令牌快到期时变黄并给「重新授权」按钮，「账号授权」行同步标注
- 服务端脚本新增两个**不联网**的运维入口：
  - `--init-config [PATH]`：生成配置模板（`--force` 才覆盖）
  - `--check`：体检渠道与 key（`可用` / `勾了没填` / `填了没勾` / `未启用`），
    无可用渠道时退出码 `2`
- 服务端 `--help` 补上典型流程；输出改为 ①②③④ 分阶段；Ctrl+C 优雅退出（`130`）
- `docs/server.md`：服务端授权完整指南（部署文件清单、两种交接方式、
  Windows 端命令、cron、退出码、安全注意、排错表）

### 变更

- **未授权门禁**：打开界面时如果检测到没有授权（无令牌 / 损坏 / 会话失效 /
  已过期），除了「立即授权」以外**所有按钮禁用**（签到、只查状态、设置定时、
  设置推送、发送测试），授权成功后自动恢复。避免"点了才报错"和"以为配好了"。
- 按钮可用性收敛到唯一出口 `App._sync_controls()`（`已授权 且 不忙`），
  不再由各任务收尾时自己 `configure(state=...)`
- 命令行帮助里 `--import-token` / `--pull-token` 明确标注为「服务端流程」

### 删除

- **界面上的远程授权入口**：「推到手机授权」「导入授权」两个按钮及对应对话框
  （远程授权只属于服务端流程，界面只保留本机浏览器授权）
- `--auth-push` 参数、`AuthDialog(push=...)`、`ImportDialog`、
  `core.login_oauth(push=...)` 及其推送逻辑
- `.build/` 下 v1 时期遗留的 40 个探针脚本与临时输出（只保留 `shot.py`）
- 根目录测试产物 `selftest.json` / `no_popup_check.json` / `oauth_mode_check.json`；
  回归脚本输出统一改到 `.build/`
- `dist/ui_import.png`（导入对话框已删除）

### 文档

- README 按新行为更新：门禁说明、到期提醒、远程授权指向 `docs/server.md`
- `docs/usage.md` 授权章节重写（只讲本机授权 + 服务端流程指路），
  新增「到期提醒」小节与 `expiry_warn_days` 配置项
- `docs/architecture.md` 新增「界面只保留本机浏览器授权」「未授权就把界面锁住」
  「到期提醒」三节
- `docs/troubleshooting.md`：未授权 / 授权失效两节按新行为重写，
  服务端章节改为先跑 `--check`
- `docs/build.md` 检查清单补「未授权时按钮全灰」「两套配置都要断推送」

### 验证

- `tools/oauth_mode_check.py`：**52/52 通过**（新增到期提醒 6 项、服务端体检 3 项）
- 源码态 / 冻结态 `--selftest` 均通过；未授权时按钮全灰（截图确认）
- `--sched-status` 零控制台弹窗
- 断推送后 `--run` 退出码 0

---

## [2.1] - 2026-10-19

**目录与入口重组：逻辑源码、构建脚本、本地运行、独立运行四件事彻底分开。**

### 变更（不兼容的路径调整）

- **目录分层**
  - `src/`：全部业务逻辑（GUI、签到、授权、推送、计划任务、服务端脚本）
  - `build/`：全部构建代码（`build_exe.py` + `entry.py`）
  - `tools/`：回归检查，不参与发布
  - 仓库根目录只留 `run.py` 与文档
- **两个运行入口**
  - `run.py`：本地源码运行，唯一入口。负责挂 `src/` 到 `sys.path`，
    并在缺 tkinter 时给出明确提示
  - `build/entry.py`：独立运行入口，只给 PyInstaller 用。冻结态不再做
    路径引导，避免把 `_MEIPASS` 里的包结构搞乱
- **数据目录按运行方式分流**
  - 本地运行：仓库根目录
  - 独立运行：exe 所在目录
  - 两套 `config.json` / `checkin.log` / `auth_oauth.json` 互不影响
  - 界面日志区新增一行「运行方式」，排查时先确认在看哪一套
- 版本号 2.0 → 2.1。

### 构建

- `build/build_exe.py` 显式传 `--paths src`，入口改为 `build/entry.py`
- 打包加 `--clean`，避免上一次的中间产物导致 `PermissionError`
- 自检不再只看 `selftest.json` 是否存在，还要求 `frozen == true`；
  顺带打印授权状态与推送渠道
- 打包成功后自动复制出 `dist/WorkBuddy签到.exe`，不再手工改名
- `.gitignore` 不再忽略 `build/`（它是打包脚本目录）；中间产物仍在 `.build/`

### 文档

- README 按新结构重写，开头先讲清「本地运行 vs 独立运行」
- `docs/` 重组为四份：`usage.md`（使用）、`build.md`（构建）、
  `architecture.md`（架构取舍）、`troubleshooting.md`（排障）
- 删除过期的界面缩放、客户端通道、旧路径说明；命令行示例统一改为
  `python run.py …` / `WorkBuddy签到.exe …`
- 排障手册新增「先分清两套数据目录」与「本地/独立任务互相覆盖」两节

### 回归

- `tools/oauth_mode_check.py`：41 项全部通过（路径改为 `src/`）
- `dist/WorkBuddy签到.exe --selftest`：`frozen=true`、`app_dir=dist`
- `--sched-status` 无控制台弹窗

## [2.0] - 2026-10-19

**不兼容的大改版：只保留「账号直连」一条通道，其它全部删掉。**

### 移除

- **客户端通道（WBIPC）整体删除**：不再借用客户端注入的鉴权，也不再
  自动拉起 / 隐藏窗口 / 签到后关闭客户端。账号直连本来就不需要客户端，
  两套通道并存只会让界面和配置翻倍。
- 随之删除的文件：`wb_host.py`（进程/窗口/登录态辅助）、
  `probe_hidden_launch.py`（后台拉起探针）、
  `tools/module_discovery_check.py`（内置模块发现回归检查）。
- 配置项 `auth_mode`、`workbuddy_exe`、`ipc_dir`、`launch_workbuddy`、
  `launch_timeout`、`hide_window`、`hide_window_timeout`、
  `close_after_checkin`、`close_timeout`、`close_force`、
  `auto_env_check`、`ui_scale`、`wecom_webhook`、`dingtalk_webhook`。
  旧配置文件里残留的这些键在下次保存时自动剔除（值不丢，只删键）。
- 界面里的「工作流自检」式诊断：`detect_env` 只剩授权与接口两项。

### 新增

- **`wb_auth_server.py`：服务端授权脚本**。在服务器上生成授权链接 →
  推送到企业微信 / 钉钉 → 用户手机点开登录 → 脚本拿回令牌。
  支持三种交付方式：
  - 默认输出一次性导入码（`WBAT1.…`，base64），在 Windows 端粘贴导入；
  - `--handoff` 开一次性端口，Windows 端 `--pull-token` 自动领取，全程无人工；
  - `--push-only` 只把已有链接转发到渠道。
  默认**不在服务器落盘**——`refreshToken` 会轮换，令牌必须只有一份。
- **「推到手机授权」**（`--auth-push`）：本机生成链接、推到手机、本机轮询。
  没有浏览器 / 人不在机器前也能完成授权，全程不启动客户端。
- **导入授权**（`--import-token`）：粘贴导入码或选令牌文件，界面与命令行都支持。
- 配置项 `push_channels`：推送类型从"填了 key 就算开"改成**显式勾选**，
  界面上的三块（授权 / 定时 / 推送）与配置一一对应。
- `wb_config.py`：配置与路径统一到一处，GUI 与服务端脚本共用。
- `wb_notify.py`：推送独立成模块，零依赖、跨平台，含域名白名单校验。
- `--diagnose` 现在输出一份完整证据快照（令牌摘要、billing 基址、接口返回、
  计划任务状态），排查时不用再翻代码。

### 变更

- 界面**重做**：只保留「账号授权」「每日定时」「消息推送」三块 + 状态卡片 +
  运行日志。设置窗口、登录方式单选、客户端路径、超时设置、界面缩放全部移除。
- 无界面入口在**源码态且有控制台**时不再抢走 stdout（以前 `--diagnose | head`
  什么也看不到，输出全被重定向进了日志）。
- `--login-oauth` 更名为 `--auth`，语义不变。

### 回归

- `tools/oauth_mode_check.py` 重写：39 项断言，覆盖轮询 / 字段兼容 /
  令牌轮换 / 401 重试 / 12153 / 导入码往返 / 坏码拒绝 / 推送渠道筛选 /
  服务端脚本可导入。

## [1.7] - 2026-10-19

### 新增

- **账号直连通道（`auth_mode: "oauth"`）**：不启动客户端也能签到。
  走 CodeBuddy CLI 的设备授权流程换取令牌，之后纯 HTTP 调签到接口。
  这是为"不想让客户端常驻/不想被拉起"的机器准备的第三条路——
  此前只有两条：借客户端注入鉴权（要求客户端在跑），
  或读本地登录态（客户端 5.6.2 起把令牌做了字段级加密，这条路已死）。
- `wb_oauth.py`：授权、令牌落盘、自动续期独立成模块。
  与 WBIPC 的 `Session` **同构**（都只暴露 `fetch(method, path)`），
  所以签到三步、推送、日志这些业务逻辑一行没改，两种通道共用。
- 设置窗口加「登录方式」单选，选直连时出现「立即授权」按钮与授权状态；
  主界面横幅支持直连模式的四种状态（未授权 / 失效 / 网络不通 / 正常），
  并在配置摘要里显示令牌剩余天数。
- 命令行 `--login-oauth`：等价于界面上的「立即授权」。
- `tools/oauth_mode_check.py`：用本地桩服务器把整条链路跑一遍
  （24 项断言），不碰真实接口、不需要真实账号。

### 令牌续期策略

- 过期前 **5 天**预刷新（`REFRESH_MARGIN`），避免服务端提前撤销导致空跑；
- 调用遇 401 / 403 时，刷新后**重试一次**；仍被拒才判定授权失效；
- `refreshToken` 每次刷新都会轮换，刷新后立即原子写回（`.tmp` + `os.replace`）；
- 只有 refresh 也失效（服务端 `12153` / `Offline user session not found`）
  才标记 `disabled` 并提示重新授权，不当成网络错误含糊过去。

### 修复

- **授权轮询把"等待中"当成失败**：接口在用户还没点完登录时返回 `code=11217`，
  这个码既不是成功也不是错误，必须继续轮询。回归用例专门盯住这条。
- **授权接口与刷新接口的字段命名不一致**（`access_token` vs `accessToken`），
  只认一种就会取到空令牌。现在两边都兼容。

### 安全

- 令牌文件 `auth_oauth.json` 已加入 `.gitignore`，写入时做 `chmod 600`（POSIX）；
  界面上只显示账号与剩余天数，**任何位置都不显示令牌内容**。
- 默认模式仍是 `wbipc`——直连需要显式开启。直连把令牌明文落在本机、
  调用身份也从桌面端变成 CLI，安全性弱于客户端通道，README 与免责声明都已写明。

## [1.6] - 2026-10-11

### 修复

- **换一台电脑就报 `IPC_MODULE_NOT_FOUND`（"缺少内置模块"）**，
  哪怕客户端已安装并登录。根因是客户端自带的 `workbuddy_ipc` 模块，
  其父目录在不同安装方式/版本下**布局并不统一**——
  安装目录里是 `…/workbuddy-builtin/skills/library`，
  也可能在 `…/workbuddy-builtin/builtin-plugins/<插件>/skills/<技能>/`，
  用户目录的插件缓存又是另一套结构。旧版只探测几条写死的相对路径，
  对不上就整体失效，且报错信息不含任何定位线索（表现为"本机好好的，
  换台机器就坏"）。
  现在改为**约定路径 + 有界递归兜底**：在安装目录、插件缓存、`~/.workbuddy`
  下逐层搜索模块父目录（深度 ≤ 9、条目数封顶、跳过纯缓存目录）；
  实测 `module_roots()` 约 0.15 s，对签到耗时无实质影响。
  同时把发现过程（候选目录数量、客户端安装目录、逐条导入失败原因）记进
  `checkin.log` 与 `selftest.json`，便于远程定位。
- **「缺少内置模块」被误报成「客户端未登录」**。`detect_env` 原先把
  `IPC_MODULE_NOT_FOUND` 与 `NOT_IN_WORKBUDDY` 归到同一分支，界面上显示
  "客户端未登录"，把排查方向带偏。现在两者分开，并在横幅上直接给出处理入口。

### 新增

- 配置项 `ipc_dir`：手工指定内置模块所在目录，作为自动探测的兜底。
  **没有界面入口**（正常安装用不到，见下文「界面」一节）；需要时手工改
  `config.json`，留空即恢复自动探测。
- 自检输出新增 `module_roots` 逐条体检（`exists` / `has_package`）、
  `install_root`、`ipc_search_trace`。
- `tools/module_discovery_check.py`：回归检查——在临时目录合成四种真实布局
  （标准安装 / skills 直挂 / 深层未知嵌套 / 插件缓存式）验证发现逻辑，
  并校验"全找不到"时的定位线索没有丢、界面报错里也没混进内部细节。

### 性能

- **打开程序后要等 5 秒多才能用**。启动自检里那次计划任务查询走的是 PowerShell
  （`Get-ScheduledTask`），每次都要重新加载 ScheduledTasks 模块，本机实测单次
  **5.0 秒**——这段时间按钮是灰的，看起来就像"卡住了"。现在改为**直读任务定义
  文件** `%SystemRoot%\System32\Tasks\<任务名>`（一份人人可读的 XML），
  实测 **0.5 毫秒**，快约一万倍。"在不在、指向哪个程序、几点跑"这些信息 XML 里
  全都有，只有运行记录（下次运行时间、上次结果）不在，于是：
  - 启动自检不再取运行记录，主界面摘要相应显示为「· 已注册」；
  - 「定时签到」对话框改为**两段式**：先用快速查询秒显任务是否存在、指向对不对，
    再用 PowerShell 补上运行记录，窗口打开即可见内容、不必干等。
  同一处还修掉了「应用定时」会卡 10 秒的问题：清理 v1.0 遗留任务时要对两个
  任务名各查一次存在性，用 PowerShell 就是 2 × 5 秒，现在同样是读 XML。
- **启动检测整体从约 5.7 秒降到约 0.5 秒**（`load_config` + 定位客户端 +
  内置模块发现 + 深度自检 + 计划任务查询）。
- **重复扫盘**：`module_roots()` 结果加进程级缓存（key = 配置里的 `ipc_dir`），
  否则启动自检与 `load_ipc` 会各付一次全盘扫描（约 0.16 s）；
  排查用的 `probe_module_roots()` 走强制实扫，不受缓存影响。
- **窗口启动时反复重绘**：`_fit_window()` 算出的尺寸与当前一致时不再重设
  `geometry()`。环境自检会连着刷几轮内容，窗口因此不再闪。

### 界面

- **移除「设置 → 内置模块目录」输入框**。它是"自动探测失败时的最后一根稻草"，
  正常安装根本用不到，摆出来只会让使用者以为必须填。配置项 `ipc_dir` 仍然有效，
  且保存设置时会原样保留已有值，不会被清空。
- 「缺少内置模块」的横幅不再引导使用者去设置里填目录，改为提示"重装或升级
  客户端后重新自检"——该模块由客户端自带，程序已翻遍常见位置，剩下最可能的原因
  就是客户端本身不完整。界面上的报错同时精简为一句话。

### 修复（发现过程中）

- 递归发现的两个自身缺陷：**命中后提前收工**会漏掉同一 base 下的其它布局；
  **"跳过所有点号目录"**会跳过最关键的 `~/.workbuddy`。已分别改为
  「命中后继续下探」与「显式黑名单」。

## [1.5] - 2026-10-11

### 修复

- **点击「定时签到」会弹出 PowerShell 窗口**。根因是窗口程序自身没有控制台，
  拉起 `powershell.exe` / `tasklist.exe` / `taskkill.exe` 这类控制台程序时，
  Windows 找不到可继承的控制台就会给子进程**新建一个控制台窗口**。
  新增 `win_quiet.py`，所有子进程调用统一使用
  `creationflags=CREATE_NO_WINDOW` + `startupinfo(SW_HIDE)`，共覆盖 4 处调用点。
- **打包脚本可能产出无法启动的 exe**。此前 `build_exe.py` 直接使用
  `sys.executable` 打包，若该解释器不带 tkinter，PyInstaller **不报任何错**，
  只是悄悄少收集 `_tkinter.pyd` 与 tcl/tk 运行时，打出来的 exe 一启动就
  `ModuleNotFoundError: No module named 'tkinter'`。现在会主动挑选带 tkinter 的解释器
  （也可 `--python <路径>` 指定），并在打包后自动跑 `--selftest` 校验，不通过则拒绝产出。

### 新增

- `--sched-status` 无界面入口：不开界面即可查看计划任务状态（结果同时写入 `checkin.log`）。
- `build_exe.py --python <路径>`：手动指定打包解释器。
- 打包后自动校验（`verify_bundle`）。

### 移除

- **移除 bat 定时方案**：删除 `run_checkin.bat` 与 `setup_task.bat`。
  该方案先探测本机 Python 再调起脚本，**依赖目标机器装 Python**；
  且用 `schtasks` 固定整点触发、**没有随机抖动**。这两点与
  「目标机器免 Python + 时间抖动」的设计目标直接冲突，因此整体删除，
  **定时签到自此只有界面一种入口**（界面 → Windows 计划任务 → `exe --run`）。
  装过旧版的机器上若还残留 `WorkBuddyCheckin_0900` / `_2100`，
  在「定时签到」里点一次「应用到系统」会自动清理（`wb_task.cleanup_legacy()`）。

### 文档

- README 全面重写，新增目录、特性表、免责声明、项目结构。
- 新增 `CHANGELOG.md`、`docs/architecture.md`、`docs/troubleshooting.md`。
- 不再提供 `config.example.json` 配置样例：全部配置项已在 README 的「配置项」
  表格里列全，单独维护一份样例反而容易与代码脱节。
- **面向使用者的文档改为只描述界面操作**：README 与 troubleshooting 中原有的命令行用法
  （`--selftest` / `--sched-status` / `--run`、以及查任务状态的命令）全部替换为
  「打开程序 → 点某个按钮」的界面步骤，`selftest.json` 相关内容一并移除。
- **移除 bat 方案相关的遗留说明**：README 里的 `setup_task.bat` / `run_checkin.bat`
  FAQ 条目、`docs/architecture.md` 的「为什么移除了 bat 方案」整章、
  troubleshooting 里的残留任务一行。
- 面向维护者的内容保持不变：从源码运行、从源码构建 / 打包、项目结构、
  `tools/no_popup_check.py` 回归检查。

### 安全

- `config.json`（可能含机器人 key）与 `selftest.json`（含本机路径、进程号、账号 uid）
  加入 `.gitignore`，不再进入版本库。

## [1.4] - 2026-10-11

### 新增

- **定时签到**：界面里设置运行时间与随机抖动，写入 Windows 计划任务。
  架构为「界面配置器 → 计划任务调度器 → exe 无界面执行器」，
  界面用完即退，系统里零常驻内存。
- `wb_task.py`：计划任务管理模块（注册 / 查询 / 删除 / 状态翻译），
  基于 PowerShell `ScheduledTasks` 模块，以「当前用户 + 仅登录时运行 + 普通权限」
  注册，不需要管理员权限、不弹 UAC。
- 随机抖动使用计划任务**原生 `RandomDelay`**，并开启 `StartWhenAvailable`
  以支持错过后补跑。
- 无界面入口：`checkin_gui.py` 增加 `--run` / `--check-only` / `--diagnose` /
  `--test-notify` 分流，走 `run_headless()`，绝不创建任何窗口。
- 配置项 `schedule_enabled` / `schedule_times` / `schedule_jitter` / `exit_after_apply`。

### 修复

- **无界面执行时中文日志导致退出码非 0**。`--windowed` 打包后 `sys.stdout`
  可能是 `None` 或 GBK 文本流，打印中文 / emoji 会抛 `UnicodeEncodeError`，
  导致"签到明明成功、退出码却是 1"，被计划任务标记为失败。
  改为**无条件**把 stdout/stderr 重定向到 UTF-8 的 `checkin.log`。
- 计划任务状态码 `267011`（尚未运行过）此前被误报为「系统标记为异常」，
  新增 `SCHED_STATUS` 状态表修正。
- **界面误报「已清理旧的计划任务」**。`cleanup_legacy()` 原本是先 `remove` 再判断
  是否存在，而 `remove` 是幂等的（任务不存在也返回成功），于是每次点「应用到系统」
  都会输出一句"已清理 `_0900` / `_2100`"，其实什么都没清。改为**先查存在、再删除**，
  只把真正删掉的任务计入返回值。
- 命令行的 `--check-only` 原本也会触发推送，与界面行为不一致，改为不推送。

### 已知问题

- 250% DPI 下 ttk(`clam`) 的 `Checkbutton` 指示器画不出勾选态，
  已改为自绘开关（方框 + ✓ + `var.trace_add` 重绘）。

## [1.3] - 2026-10-11

### 新增

- **消息推送支持企业微信机器人与钉钉机器人**，配置里**只填 key**，
  程序按官方域名自动拼完整地址。
- 多渠道路由（`PUSH_CHANNELS`），单渠道失败不影响其它渠道。
- `--test-notify` 向所有已配置渠道各发一条测试消息。

### 变更

- 设置窗口的推送分组改为「只填 key」形式；保存时会清空旧的完整链接字段，避免两处配置打架。
- 主界面配置摘要显示已启用的渠道名（如「企业微信＋钉钉」）。

### 安全

- 容错解析只接受裸 key / `key=xxx` 片段 / 整条链接三种写法；
  看着像链接却抽不出参数时**拒绝发送**，避免消息被误推到第三方地址。

## [1.2] - 2026-10-11

### 修复

- **高缩放下字特别小**。根因是 `SetProcessDpiAwareness` 调用时机错误：
  在 `tk.Tk()` 之后声明，Tk 已按 96 DPI 算完字体，而窗口又不再被系统位图放大，
  250% 缩放下字体物理尺寸小 2.5 倍。改为**先声明 DPI 感知，再创建 Tk**，
  建窗后按实际 DPI 校准 `tk scaling`，物理像素参数统一乘缩放系数。
- **装了客户端却提示"未检测到"**。两个独立 bug 叠加：
  1. 冻结态 `workbuddy_ipc` 动态导入失败 —— PyInstaller 静态分析看不到它，
     其依赖的 `hmac` / `secrets` / `socket` / `ssl` 未被收集，报
     `ModuleNotFoundError: No module named 'hmac'` 后被归类成"未登录"。
     新增 `build_exe.py` 显式声明这些 `--hidden-import`。
  2. 检测语义不完整 —— 旧逻辑只查"进程在不在跑"，客户端没开就直接说"未检测到客户端"。
     现拆分为 `installed` / `process_running` 两层判断，新增 `CLIENT_NOT_RUNNING` 状态。
- 修正 `module_roots()` 的路径拼接（少了一层 `library`）。
- 补全 Win32 调用的 `argtypes` / `restype`（`HANDLE` 是指针宽度，不声明会被截断）。
- 窗口尺寸改为内容更新后重新自适应，避免长文案把右侧内容撑破。

### 新增

- **设置窗口**：推送配置、客户端路径（自动检测 + 浏览）、运行行为开关、
  超时设置、界面缩放，保存即写回 `config.json`。
- `--selftest` 无界面自检，输出 `selftest.json`（系统 DPI、客户端路径、
  进程列表、模块搜索目录、IPC 是否连通），是排查环境问题最快的手段。
- 配置项 `ui_scale`、`auto_env_check`。

## [1.1] - 2026-10-11

### 新增

- **图形界面**（tkinter，零第三方依赖），复用核心逻辑。
- 启动时**环境自检**：客户端装没装 / 在不在跑 / 宿主通道通不通 / 登录没登录，
  异常时在顶部挂醒目横幅并给出处理按钮。
- **后台静默运行**：客户端无官方静默启动参数，改为正常拉起后用 Win32
  `ShowWindow(SW_HIDE)` 隐藏主窗口；实测**隐藏后 IPC 依然可用**。
- `wb_host.py`：Windows 侧辅助（进程枚举、窗口查找与隐藏、注册表安装路径探测、登录态读取）。
- `probe_hidden_launch.py`：后台拉起可行性探针。
- 打包为单文件 exe（`dist/WorkBuddyCheckin.exe`）。

### 已知问题

- 从"进程启动"到"隐藏成功"之间窗口会闪现约 1~5 秒，可能抢一次焦点。
  这是客户端没有官方静默参数导致的，无法完全避免。

## [1.0] - 2026-10-10

### 新增

- 命令行版签到脚本，实现签到三步流程（查状态 → 领积分 → 复核状态），幂等可重复执行。
- `run_checkin.bat` 命令行入口与 `setup_task.bat` 计划任务注册（每天 09:00 / 21:00）。

---

[1.5]: https://github.com/yehaia1/workbuddy-checkin/compare/v1.4...v1.5
[1.4]: https://github.com/yehaia1/workbuddy-checkin/compare/v1.3...v1.4
[1.3]: https://github.com/yehaia1/workbuddy-checkin/compare/v1.2...v1.3
[1.2]: https://github.com/yehaia1/workbuddy-checkin/compare/v1.1...v1.2
[1.1]: https://github.com/yehaia1/workbuddy-checkin/compare/v1.0...v1.1
[1.0]: https://github.com/yehaia1/workbuddy-checkin/releases/tag/v1.0
