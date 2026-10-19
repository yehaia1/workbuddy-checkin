# 架构与实现取舍

本文讲**为什么这样分、为什么这样做**。想直接上手看
[usage.md](usage.md)，打包看 [build.md](build.md)。

---

## 1. 分层：逻辑、入口、构建彻底分开

```
                    ┌──────────────────────────┐
   本地运行 ───────▶│  run.py（唯一源码入口）   │
                    └────────────┬─────────────┘
                                 │ sys.path += src/
                                 ▼
                    ┌──────────────────────────┐        ┌──────────────┐
   独立运行 ───────▶│  src/  业务逻辑 + 界面    │◀───────│  build/      │
   （exe）          │  checkin_gui / wb_*      │  打包   │  entry.py    │
                    └──────────────────────────┘        │  build_exe   │
                                 │                       └──────────────┘
                                 ▼
                    config.json / auth_oauth.json / checkin.log
```

| 层 | 目录 | 允许做的事 | 不允许做的事 |
|---|---|---|---|
| 业务逻辑 | `src/` | 读写配置、调接口、画界面 | import `build/` 里的任何东西 |
| 本地入口 | `run.py` | 挂 `src/` 到 sys.path、调 `main()` | 写业务逻辑 |
| 构建 | `build/` | 选解释器、调 PyInstaller、校验产物 | 写业务逻辑 |
| 回归 | `tools/` | 驱动上面的代码做检查 | 参与发布 |

这样分之后，"改 bug"永远只动 `src/`，"改打包参数"永远只动 `build/`，
不会出现「打包脚本里藏了一段签到逻辑」这种谁都找不到的东西。

---

## 2. 本地运行 vs 独立运行

同一份逻辑，两种运行方式，差别不只是"有没有 exe"：

| 维度 | 本地运行 | 独立运行 |
|---|---|---|
| 入口 | `run.py` | `build/entry.py` → exe |
| `sys.frozen` | 假 | 真 |
| 数据目录 | 仓库根目录（`src/` 的上一级） | exe 所在目录 |
| 控制台 | 有，`print` 直接可见 | 无，输出进日志 |
| 依赖 | Python + tkinter | 无 |
| 升级 | 改代码即生效 | 重新打包 |

### 为什么数据目录必须分开

`wb_config.app_dir()` 是唯一判断点：

```python
if getattr(sys, "frozen", False):
    return Path(sys.executable).parent      # exe 旁边
return Path(__file__).parent.parent         # 仓库根目录
```

- 冻结态：`__file__` 在 PyInstaller 的临时解包目录 `_MEIPASS` 里，写进去的
  `config.json` 会随进程退出一起消失，所以必须用 exe 目录；
- 源码态：如果直接用 `__file__` 的目录，配置就会落在 `src/` 里，和 `.py` 混在
  一起，也容易误以为"exe 的配置丢了"。所以显式上跳一级到仓库根目录。

结果是根目录和 `dist/` 各有一套 `config.json` / `checkin.log` /
`auth_oauth.json`，调试时改坏一套不影响另一套。**排查问题先确认在看哪一套。**

### 为什么入口要两个

`run.py` 和 `build/entry.py` 都只有几行，看着重复，但它们的环境假设相反：

- `run.py` 假设源码在旁边，负责把 `src/` 挂进搜索路径，并检查 tkinter；
- `build/entry.py` 假设模块已经被打进 `_MEIPASS`，**只在源码态**才做路径引导。

合成一个入口的写法通常是 `if frozen: ... else: ...`，然后这个判断会慢慢渗进
业务代码。现在业务代码对运行方式只有一个依赖：`wb_config.run_mode()`。

---

## 3. 定时签到：三个角色分开

```
┌──────────────┐   写入    ┌────────────────────┐   到点触发   ┌──────────────────┐
│   图形界面    │ ────────▶ │  Windows 计划任务   │ ──────────▶ │  无界面入口       │
│  （配置器）   │           │    （调度器）       │             │  （执行器）       │
└──────────────┘           └────────────────────┘             └──────────────────┘
   用完即退                     系统负责，零常驻                    只在触发那一刻存在
   0 MB 常驻                    0 MB 常驻                          跑完自动退出
```

- **界面不能常驻**：没人会为了签到一直开窗口；
- **调度必须交给系统**：自己 sleep，重启 / 休眠 / 关机就全废；
- **执行必须无界面**：计划任务弹窗会在用户毫无预期时打断他。

三者之间只有两条契约：计划任务里的**程序路径 + `--run`**，以及 `config.json`。

---

## 4. 模块职责

| 模块 | 职责 | 能在服务器跑 |
|---|---|---|
| `wb_config.py` | 配置默认值、读写、数据目录 | ✅ |
| `wb_notify.py` | 企业微信 / 钉钉推送 | ✅ |
| `wb_oauth.py` | 设备授权、令牌落盘、续期、导入码 | ✅ |
| `workbuddy_checkin.py` | 签到三步、文案、CLI | ✅ |
| `wb_auth_server.py` | 服务端授权 | ✅（本来就是给服务器用的） |
| `wb_task.py` | Windows 计划任务 | ❌ 仅 Windows |
| `win_quiet.py` | 子进程不弹黑框 | ❌ 仅 Windows |
| `checkin_gui.py` | tkinter 界面 + 无界面分流 | ❌ 仅桌面 |

前四个 + 服务端脚本必须保持**零 Windows 依赖**（不 import `tkinter` /
`ctypes.windll`），否则服务端授权脚本没法在 Linux 上跑。

---

## 5. 单次签到

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

## 6. 授权：为什么是设备授权

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
谁就能取到令牌，跟发起请求的机器无关**。这是服务端脚本能跨机器工作的根本原因。

### 令牌只有一份

`refreshToken` 每次刷新都会**轮换**。两台机器各存一份 → 谁先刷新，另一份立刻
失效（`12153`），表现为"授权莫名其妙失效"。所以：

- 服务端脚本默认**不落盘**，只输出一次性导入码；
- Windows 端导入后独占，之后所有续期都发生在那一台；
- `--handoff` 用一次性随机 secret 把令牌直接交给客户端，取走即关端口。

### 续期策略

| 时机 | 动作 |
|---|---|
| 距过期 < 5 天 | 预刷新（服务端可能提前撤销，别卡最后一刻） |
| 调用遇到 401 / 403 | 刷新并**重试一次**，仍被拒才判失效 |
| 刷新返回 12153 | 标记 `disabled` 并落盘，提示重新授权 |

刷新后必须**立刻原子写回**（`.tmp` + `os.replace`）。否则下一次运行读到旧
refreshToken，直接判死。

---

## 7. 高 DPI

Windows 缩放 125% / 250% 时，进程必须在**创建 Tk 之前**声明 DPI 感知。
否则 Tk 初始化时按 96DPI 换算字体；建完窗口再声明就晚了——窗口不再被位图
放大，字体却仍按 96DPI 算，250% 下会小 2.5 倍。

声明之后还要校准两样：

- `tk scaling`：管字号（每 point 多少像素）
- `PX` 系数：管 padding / width 这类**物理像素**参数，它们不跟 tk scaling 走

只做前者的话，250% 下字号对了，但间距挤成一团。

---

## 8. 子进程不弹控制台窗口

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
pythonw tools/no_popup_check.py --exe "dist/WorkBuddy签到.exe" -- --sched-status
```

---

## 9. 无控制台程序的输出

`--windowed` 打包后 `sys.stdout` / `sys.stderr` 可能是 `None`，任何 `print()`
都会抛异常；即使存在，也可能是 GBK 文本流，中文 / emoji 触发
`UnicodeEncodeError`。这两种都会导致**签到成功、退出码非 0**，从而被计划任务
记成失败。

现在的处理分两种情况：

- 流存在（终端手敲、`--json` 接管道）→ 就地改成 UTF-8 + replace，保留输出；
- 流不存在（计划任务拉起 exe）→ 重定向到 UTF-8 的 `checkin.log`。

以前无条件重定向，`exe --json | jq` 一个字节都拿不到。

---

## 10. 计划任务：为什么不用 schtasks.exe

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

启动自检、界面刷新一律走 `query_fast()`；只有「每日定时」对话框的第二段补充
查询和 `--sched-status` 才用 `query()`。把 5 秒查询挂在启动流程上，就是
"开程序后横幅转半天"的元凶。

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

## 11. 回归检查

| 脚本 | 覆盖什么 | 依赖 |
|---|---|---|
| `tools/oauth_mode_check.py` | 轮询、字段兼容、令牌轮换、401 重试、12153、导入码、推送渠道筛选 | 本地桩服务器，不碰真接口 |
| `tools/no_popup_check.py` | 运行时是否弹出控制台窗口 | 需要 `pythonw` 运行，Windows |

```bash
python tools/oauth_mode_check.py        # 41 项断言
python build/build_exe.py               # 打包并自检
pythonw tools/no_popup_check.py --exe "dist/WorkBuddy签到.exe" -- --sched-status
```
