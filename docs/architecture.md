# 架构与实现取舍

本文讲**为什么这样分、为什么这样做**。想直接上手看
[usage.md](usage.md)，打包看 [build.md](build.md)。

---

## 1. 两个产品，一份逻辑

```
    src/common/   wb_config · wb_oauth · wb_notify · workbuddy_checkin
         ▲                        ▲
         │                        │
    src/desktop/             src/server/
    checkin_gui · wb_task    wb_server
    win_quiet                     │
         │                        │  build/make_server.py（import 闭包 + zipapp）
         │ build/entry.py         ▼
         ▼                  dist/server/wb_server.pyz   ← 单文件，拷到哪都能跑
  dist/WorkBuddyCheckin.exe

    数据目录：exe → 用户缓存；pyz → 脚本目录；仓库里跑 → 仓库根
```

| 产品 | 形态 | 调度 | 授权方式 |
|---|---|---|---|
| Windows 版 | `WorkBuddyCheckin.exe`（GUI） | Windows 计划任务 | 手机 / 本机浏览器 |
| 服务端版 | `wb_server.pyz`（无界面单文件） | cron | 推链接到手机 |

两者共用 `src/common/` 下的 `workbuddy_checkin.py` / `wb_oauth.py` /
`wb_notify.py` / `wb_config.py`，差别只在入口和调度方式。
**不要在同一账号上同时跑两边**（见第 6 节）。

---

## 2. 分层：逻辑、入口、构建彻底分开

| 层 | 目录 | 允许做的事 | 不允许做的事 |
|---|---|---|---|
| 共用逻辑 | `src/common/` | 读写配置、调接口、签到、推送 | import `desktop/`、`server/`、`build/` |
| exe 版 | `src/desktop/` | 画界面、写计划任务、抑制弹窗 | 被 `common/` 或 `server/` import |
| 服务端 | `src/server/` | 只做 CLI 入口、编排授权与签到 | import `desktop/` 里的任何东西 |
| 构建 | `build/` | 选解释器、调 PyInstaller / zipapp、校验产物 | 写业务逻辑 |
| 回归 | `tools/` | 驱动上面的代码做检查 | 参与发布 |

`src/` 里为什么是"按版本分"而不是"按技术分"：服务端部署时只该拿自己那一份。
`make_server.py` 按 import 闭包收模块，闭包里一出现 `checkin_gui` / `wb_task` /
`win_quiet` 就直接构建失败——这条边界是**可执行**的，不靠自觉。

这样分之后，"改 bug"基本只动 `common/`，"改界面"只动 `desktop/`，
"改打包参数"只动 `build/`，不会出现「打包脚本里藏了一段签到逻辑」这种谁都找不到的东西。

---

## 3. 数据目录：三种运行方式，三个位置

`wb_config` 是唯一判断点，入口调用 `prepare()` 定下来：

| 运行方式 | 数据目录 | 谁来定 |
|---|---|---|
| 独立运行（exe） | `%LOCALAPPDATA%\WorkBuddyCheckin` | `build/entry.py` |
| 服务端（单文件） | `wb_server.pyz` 所在目录 / `--config` 的上级 | `wb_server.main()` |
| 源码运行（开发） | 仓库根目录 | 入口脚本 `main()` 里的 `prepare()` |

### 为什么 exe 不写在自己旁边

v2.x 是写在 exe 旁边的，代价很具体：

- exe 常被放在桌面、U 盘、只读目录、Program Files —— 写配置可能被安全软件拦；
- exe 换个位置，用户看到的就是"配置丢了、要重新授权"；
- 把 exe 发给别人时，`config.json`（含机器人 key）和 `auth_oauth.json`
  （明文令牌）会一起被带走。

写进当前用户的缓存目录后，exe 就是个纯程序：放哪都能跑，拷给别人也不带数据。
代价是"配置不在我眼皮底下"，所以界面的日志第一行会直接打印数据目录路径。

### 为什么服务端要跟着脚本走

服务器上没有"当前用户缓存"这种概念（cron 用户、容器、只读镜像都不同），
最可靠的做法是**配置和脚本在同一层**：整个目录拷走就能换机器，
`--config` 换个地方也能用，不依赖任何全局路径。

### 升级迁移

v3.0 首次运行 exe 时，会把旧版留在 exe 旁边的 `config.json` /
`auth_oauth.json` **复制**到缓存目录：

- 只在目标不存在时复制（不覆盖新配置）；
- 源文件保留，让用户自己确认后再删；
- 数据目录 == 程序目录（服务端、源码运行）时不做迁移。

---

## 4. 模块职责

| 模块 | 位置 | 职责 | 能在服务器跑 |
|---|---|---|---|
| `wb_config.py` | `common/` | 配置默认值、读写、数据目录、版本号 | ✅ |
| `wb_notify.py` | `common/` | 企业微信 / 钉钉推送 | ✅ |
| `wb_oauth.py` | `common/` | 设备授权、令牌落盘、续期、导入码 | ✅ |
| `workbuddy_checkin.py` | `common/` | 签到三步、文案、到期提醒、CLI | ✅ |
| `wb_server.py` | `server/` | 服务端入口：检查授权 → 授权（退避重试）→ 签到 → 推送 | ✅（本来就是给服务器用的） |
| `wb_task.py` | `desktop/` | Windows 计划任务 | ❌ 仅 Windows |
| `win_quiet.py` | `desktop/` | 子进程不弹黑框 | ❌ 仅 Windows |
| `checkin_gui.py` | `desktop/` | tkinter 界面 + 无界面分流 | ❌ 仅桌面 |

服务端那一列打 ✅ 的必须保持**零 Windows 依赖**（不 import `tkinter` /
`ctypes.windll` / `wb_task`），否则 Linux 上跑不起来。回归里有专门的断言。

---

## 5. 调度：交付给系统，自己绝不常驻

### Windows 版

```
┌──────────────┐   写入    ┌────────────────────┐   到点触发   ┌──────────────────┐
│   图形界面    │ ────────▶ │  Windows 计划任务   │ ──────────▶ │  exe --run        │
│  （配置器）   │           │    （调度器）       │             │  （执行器）       │
└──────────────┘           └────────────────────┘             └──────────────────┘
   用完即退                     系统负责，零常驻                    跑完自动退出
```

- **界面不能常驻**：没人会为了签到一直开窗口；
- **调度必须交给系统**：自己 sleep，重启 / 休眠 / 关机就全废；
- **执行必须无界面**：计划任务弹窗会在用户毫无预期时打断他。

计划任务里只有两条契约：**exe 的绝对路径 + `--run`**。
`wb_task.launcher()` 是唯一决定这件事的地方——冻结态永远是 exe 自己，
源码态才是 `pythonw src/common/workbuddy_checkin.py --run`（只用于开发自测）。

绝对路径的代价是"exe 一挪就失效"，所以 v3.0 加了**启动自愈**：
`ensure_current()` 比对任务里的命令与当前程序，不一致就按当前配置重新注册。
源码运行**不做**这一步——否则开发机的注册动作会把用户装好的 exe 任务抢走。

### 服务端版

cron 直接调 `python3 wb_server.pyz`，脚本自己决定要不要先授权。
调度器不理解"授权"这件事，所有状态都在脚本里，所以换 cron 表达式、换机器
都不影响行为。

---

## 6. 单次签到

```
POST /v2/billing/meter/checkin-activity-status   查：今天签了吗
  ├─ active=false            → INACTIVE，结束
  ├─ today_checked_in=true   → ALREADY_CLAIMED，结束
  └─ 否则
POST /v2/billing/meter/daily-checkin             领（code=10001 = 已领过）
POST /v2/billing/meter/checkin-activity-status   复核：真签上了吗
```

三步都幂等，重复触发安全。领取报错时**先复核再判定失败**——服务端偶发 5xx
但实际已入账的情况确实存在，不复核会把成功报成失败。

---

## 7. 授权：为什么是设备授权

客户端 5.6.2 起把本地登录态改成字段级 AES-GCM 加密（at-rest），
"读本地 token 直接调接口"的老路子已经死了。所以换一条：

```
POST /v2/plugin/auth/state?platform=CLI   → {state, authUrl}
GET  /v2/plugin/auth/token?state=<state>  → 轮询：
                                               code=0     拿到令牌
                                               code=11217 还在等（不是失败！）
                                               code=12153 会话失效，只能重新授权
```

两个端点都是**无状态 HTTP**，`state` 只是服务端一段会话 id：**谁拿着 state，
谁就能取到令牌，跟发起请求的机器无关**。

但 `state` 变成「已完成」这一步，是**登录页自己**做的：登录成功后页面回调
`GET /console/auth/login?platform=CLI&state=...`，带着页面自己的登录 cookie。
所以"谁完成登录"很关键——把 authUrl 转发到手机上，登录态落在手机浏览器里，
服务端轮询只会一直返回 `11217`（实测）。

服务端版于是反过来：**自己开一个浏览器**（`wb_qrlogin`）打开 authUrl，
把那页里的微信二维码取出来推给用户扫。登录发生在服务端可控的浏览器里，
令牌就落到了服务器上。这是 v3.2.0 的分界线。

### 服务端的授权循环

```
检查授权（detect_env 深检）
  ├─ OK          → 直接签到（不启动浏览器）
  ├─ 网络/接口异常 → 退出码 3，不折腾授权
  └─ 无 / 坏 / 失效
       轮 1：申请 → 起无头浏览器取二维码链接 → 推 → 等 180s
       轮 2：隔 60s → 重新申请 → 新的二维码 → 推 → 等 180s
       轮 3：隔 120s → 同上
       拿到令牌立刻关浏览器；全失败 → 推失败通知 → 退出码 2
```

推的是**二维码图片链接**（`open.weixin.qq.com/connect/qrcode/<code>`），
不是 PNG 也不经过任何第三方图床：那张图本来就是公网资源，企业微信和钉钉
都只需要发文字，手机点开长按识别即可。这样就不必在服务器上开端口，
也回避了"钉钉机器人不支持图片消息"这个差异。

几个细节值得记：

- **每轮重新申请会话**：`state` 一次性，超时后继续轮询也是白等；
- **每轮重读 config.json**：用户就是在这 3 分钟里补 key 的，第二轮才推得出去；
- **指数退避而不是死循环**：cron 每天一次，无限重试只会把机器挂在那里；
- **浏览器必须活到扫码完成**：`QRSession` 在 `finally` 里关闭，成功/超时/异常
  三条路径都收，避免一轮一个残留 Chromium。

### 令牌只有一份

`refreshToken` 每次刷新都会**轮换**。两台机器各存一份 → 谁先刷新，另一份立刻
失效（`12153`），表现为"授权莫名其妙失效"。

v2.x 试图用"服务端拿令牌、只交给 Windows"来回避这个问题；v3.0 承认那只是
把复杂度搬到了交接环节，于是改成更直接的办法：**两个版本各自授权、各存一份，
但同一账号只用一处**。要换机器就停掉旧的、在新机器上重新授权。

`--import-token` 仍保留，给"某台机器浏览器走不通"这种个别情况兜底；
用了它就要保证原机器不再运行。

### 界面只保留一个授权入口

界面上只有一个「立即授权」，但它**不要求在本机浏览器完成**：

```
点「立即授权」→ 申请会话 → 窗口里出现链接（可复制）
                        → 手机/本机/别的电脑打开，登录确认
                        → 本机轮询到 code=0 → 令牌落盘、界面刷新
```

默认**不自动弹本机浏览器**（`login_oauth(open_browser=False)`）：多数人的
WorkBuddy 账号就在手机上，把链接拿过去比在电脑上再登录一次更顺；也顺带
绕开了企业策略拦浏览器、远程桌面没有默认浏览器这类情况。「在本机打开」
按钮仍然留着给习惯在电脑上操作的人。

为什么只有一个按钮：三个按钮并排时，用户分不清"推到手机"和"导入"的区别，
也不知道该先配推送还是先授权。

### 未授权就把界面锁住

`App.refresh_auth()` 是唯一判断点，算出 `authorized` 后交给 `_sync_controls()`：

```
authorized = 令牌存在 且 未损坏 且 未 disabled 且 未过期
enabled    = authorized and not busy
```

`enabled` 决定签到按钮、② 每日定时、③ 消息推送三处是否可点；
「立即授权」永远可点——否则就成了"要授权才能授权"的死锁。

按钮状态**只在 `_sync_controls()` 里改**。以前是各任务收尾时自己
`configure(state="normal")`，结果签到结束会把按钮全开，哪怕授权此刻已经失效。

### 到期提醒

令牌 28 天有效、提前 5 天自动续期，所以正常情况下不该出现"快过期"。
一旦出现，说明续期失败（`refreshToken` 也快到期），只能重新授权。

`check_expiry_notice()` 挂在**每次签到之后**（`run_once` 的收尾处）：

| 剩余天数 | 行为 |
|---|---|
| > `expiry_warn_days`（默认 2） | 返回 `None`，什么都不做 |
| ≤ 阈值 | 推一条提醒，`result["expiry_notice"]` 里留证据 |
| 已过期 | 文案直接说"授权已过期" |

放在签到之后而不是单独定时：签到每天本来就要跑一次，顺手看一眼不增加任何
调度；重复触发也只是每天多一条消息，不需要额外的去重状态文件。

### 续期策略

| 时机 | 动作 |
|---|---|
| 距过期 < 5 天 | 预刷新（服务端可能提前撤销，别卡最后一刻） |
| 调用遇到 401 / 403 | 刷新并**重试一次**，仍被拒才判失效 |
| 刷新返回 12153 | 标记 `disabled` 并落盘，提示重新授权 |

刷新后必须**立刻原子写回**（`.tmp` + `os.replace`）。否则下一次运行读到旧
refreshToken，直接判死。

---

## 8. 高 DPI

Windows 缩放 125% / 250% 时，进程必须在**创建 Tk 之前**声明 DPI 感知。
否则 Tk 初始化时按 96DPI 换算字体；建完窗口再声明就晚了——窗口不再被位图
放大，字体却仍按 96DPI 算，250% 下会小 2.5 倍。

声明之后还要校准两样：

- `tk scaling`：管字号（每 point 多少像素）
- `PX` 系数：管 padding / width 这类**物理像素**参数，它们不跟 tk scaling 走

只做前者的话，250% 下字号对了，但间距挤成一团。

---

## 9. 子进程不弹控制台窗口

窗口程序（`--windowed` 的 exe，或 `pythonw.exe` 跑的源码）自身没有控制台。
它去拉 `powershell.exe` 这类控制台程序时，Windows 会给子进程**新建一个控制台**。

`win_quiet.quiet_kwargs()` 给所有 `subprocess` 调用加：

```python
creationflags = CREATE_NO_WINDOW
startupinfo.dwFlags |= STARTF_USESHOWWINDOW
startupinfo.wShowWindow = SW_HIDE      # 双保险
```

`CREATE_NO_WINDOW` 与 `DETACHED_PROCESS` 互斥，不能同时用。

### 验证不要靠肉眼

用 `EnumWindows` 枚举可见顶层窗口，按类名挑控制台窗口。**不要只匹配
`ConsoleWindowClass`**：Win11 的控制台由 Windows Terminal 托管，类名是
`CASCADIA_HOSTING_WINDOW_CLASS`（还有 `PseudoConsoleWindow`），只匹配老类名
会得出"没问题"的错误结论。

```bash
pythonw tools/no_popup_check.py --exe "dist/WorkBuddyCheckin.exe" -- --sched-status
```

---

## 10. 无控制台程序的输出

`--windowed` 打包后 `sys.stdout` / `sys.stderr` 可能是 `None`，任何 `print()`
都会抛异常；即使存在，也可能是 GBK 文本流，中文 / emoji 触发
`UnicodeEncodeError`。这两种都会导致**签到成功、退出码非 0**，从而被计划任务
记成失败。

现在的处理分两种情况：

- 流存在（终端手敲、`--json` 接管道）→ 就地改成 UTF-8 + replace，保留输出；
- 流不存在（计划任务拉起 exe）→ 重定向到 UTF-8 的 `checkin.log`。

以前无条件重定向，`exe --json | jq` 一个字节都拿不到。

---

## 11. 计划任务：为什么不用 schtasks.exe

- 命令行转义麻烦，中文路径极易踩编码坑
- 「随机抖动」（`RandomDelay`）几乎没法拼
- 一个任务挂多个触发器要另外写 XML

改用 PowerShell 的 `ScheduledTasks` 模块（Win8+ 自带），通过 `-EncodedCommand`
传脚本（Base64 / UTF-16LE），绕开引号与编码问题；结果以 JSON 输出。

### 权限设计

以「当前用户 + 仅登录时运行（InteractiveToken）+ 普通权限（Limited）」注册，
**不弹 UAC、不需要管理员**。代价是触发前提为"用户已登录"——本场景正好合适。

### 查状态为什么分两条路

| 方法 | 耗时 | 拿得到什么 |
|---|---|---|
| `query_fast()` 直读 `System32\Tasks\<任务名>`（UTF-16 XML） | ~0.5 ms | 在不在、几点跑、指向哪个程序、抖动多少 |
| `query()` 走 PowerShell | **~5 秒** | 上面这些 + 下次/上次运行时间、上次返回码 |

启动自检、界面刷新、**路径自愈**一律走 `query_fast()`；只有「每日定时」对话框
的第二段补充查询和 `--sched-status` 才用 `query()`。把 5 秒查询挂在启动流程上，
就是"开程序后横幅转半天"的元凶。

> XML 里 `StartBoundary` 是 **UTC**，要换算成本地时间再展示。
> 运行记录（下次/上次/返回码）不在 XML 里，快速版用 `partial=True` 标记，
> 展示方据此跳过这些字段，不要显示成"上次 未知"。

### 状态码翻译

计划任务的返回码通常就是被调进程的退出码，但 `SCHED_S_*` 系列**不是失败**：

| 码 | 含义 |
|---|---|
| 267009 | 正在运行 |
| 267011 | 尚未运行过（新建任务第一次触发前就是这个值，别误报） |
| 267014 | 就绪，尚未运行 |

---

## 12. 版本策略

版本号单一来源：`wb_config.VERSION`（见 [build.md](build.md)）。
exe 的版本资源在打包时生成，界面、日志、`--version`、文件属性四处一致。

为什么这次要从 2.2 跳到 3.0：数据目录换了位置（exe 从"旁边"到"缓存目录"）、
服务端从"授权工具"变成"日常入口"、产物不再有中文名——对用户都是可见的变化。

---

## 13. 回归检查

| 脚本 | 覆盖什么 | 依赖 |
|---|---|---|
| `tools/oauth_mode_check.py` | 轮询、字段兼容、令牌轮换、401 重试、12153、导入码、到期提醒、推送渠道筛选、版本与数据目录、迁移、服务端参数 | 本地桩服务器，不碰真接口 |
| `tools/no_popup_check.py` | 运行时是否弹出控制台窗口 | 需要 `pythonw` 运行，Windows |

```bash
python tools/oauth_mode_check.py        # 67 项断言，结果写 .build/oauth_mode_check.json
python build/build_exe.py               # 打包并自检
pythonw tools/no_popup_check.py --exe "dist/WorkBuddyCheckin.exe" -- --sched-status
```
