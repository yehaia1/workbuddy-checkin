# 架构与实现细节

本文记录各项设计决策的**原因**，以及踩过的坑。想快速上手请看 [README](../README.md)。

---

## 总体架构

### 定时签到：三个角色分开

```
┌──────────────┐   写入    ┌────────────────────┐   到点触发   ┌──────────────────┐
│   图形界面    │ ────────▶ │  Windows 计划任务   │ ──────────▶ │  exe 无界面入口   │
│  （配置器）   │           │    （调度器）       │             │   （执行器）      │
└──────────────┘           └────────────────────┘             └──────────────────┘
   用完即退                     系统负责，零常驻                    只在触发那一刻存在
   0 MB 常驻                    0 MB 常驻                          跑完自动退出
```

这么分的原因：

- **界面不能常驻**。用户不会为了签到一直开着窗口，常驻还要占内存。
- **调度必须交给系统**。自己写循环 sleep，电脑重启、休眠、关机就全废了。
- **执行必须无界面**。计划任务调起的进程如果弹窗，会在用户毫无预期的时候打断他。

三者之间只有两条契约：计划任务里记录的**可执行文件路径 + `--run` 参数**，
以及 `config.json`。

### 模块职责

| 模块 | 职责 | 能否在服务器上跑 |
|---|---|---|
| `wb_config.py` | 配置默认值、读写、路径 | ✅ |
| `wb_notify.py` | 企业微信 / 钉钉推送 | ✅ |
| `wb_oauth.py` | 设备授权、令牌落盘、续期、导入码 | ✅ |
| `workbuddy_checkin.py` | 签到三步、文案、CLI | ✅ |
| `wb_auth_server.py` | 服务端授权 | ✅（本来就是给服务器用的） |
| `wb_task.py` | Windows 计划任务 | ❌ 仅 Windows |
| `checkin_gui.py` | tkinter 界面 | ❌ 仅 Windows 桌面 |

服务端脚本复用前四个模块，所以它们**不能 import 任何 Windows 专有东西**
（`tkinter` / `ctypes.windll`）。

---

## 单次签到流程

```
POST /v2/billing/meter/checkin-activity-status   查：今天签了吗
  ├─ active=false            → INACTIVE，结束
  ├─ today_checked_in=true   → ALREADY_CLAIMED，结束
  └─ 否则
POST /v2/billing/meter/daily-checkin             领（code=10001 = 已领过）
POST /v2/billing/meter/checkin-activity-status   复核：真签上了吗
```

三步都幂等，重复触发安全。领取报错时**先复核再判定失败**——服务端偶发 5xx
但实际已入账的情况真的存在，不复核会把成功报成失败。

---

## 授权：为什么是"设备授权"

客户端 5.6.2 起把本地登录态改成字段级 AES-GCM 加密（at-rest），
"读本地 token 直接调接口"的老路子已经死了。所以这里换一条：

```
POST /v2/plugin/auth/state?platform=CLI   → {state, authUrl}
GET  /v2/plugin/auth/token?state=<state>  → 轮询：
                                               code=0     拿到令牌
                                               code=11217 还在等（不是失败！）
                                               code=12153 会话失效，只能重新授权
```

两个端点都是**无状态 HTTP**，`state` 只是服务端一段会话 id，所以：
**谁拿着 state，谁就能取到令牌，跟发起请求的机器无关**。这正是服务端脚本
（`wb_auth_server.py`）能跨机器工作的原因。

### 令牌只有一份（最容易踩的坑）

`refreshToken` 每次刷新都会**轮换**。两台机器各存一份 → 谁先刷新，另一份立刻失效
（服务端 `12153`），表现为"授权莫名其妙失效"。所以：

- 服务端脚本默认**不落盘**，只把令牌包成一次性导入码交出去；
- Windows 端导入后独占，之后所有续期都发生在那台机器上；
- `--handoff` 模式用一次性随机 secret 把令牌直接交给客户端，取走即关端口。

### 续期策略

| 时机 | 动作 |
|---|---|
| 距过期 < 5 天 | 预刷新（服务端可能提前撤销，别卡最后一刻） |
| 调用遇到 401 / 403 | 刷新并**重试一次**，仍被拒才判定失效 |
| 刷新返回 12153 | 标记 `disabled` 并落盘，提示重新授权 |

刷新后必须**立刻原子写回**（`.tmp` + `os.replace`）。否则下一次运行读到的是旧
refreshToken，直接判死。

---

## 高 DPI（为什么字会变小）

Windows 缩放到 125% / 250% 时，进程必须在**创建 Tk 之前**声明 DPI 感知。
否则 Tk 初始化时按 96DPI 换算字体；等 Tk 建完再声明感知就晚了——窗口不再被
位图放大，字体却仍按 96DPI 算，250% 下会小 2.5 倍。

声明之后还要用实际 DPI 校准两样东西：

- `tk scaling`（管字号，单位是「每 point 多少像素」）
- `PX` 系数（管 padding / width 这些**物理像素**参数，它们不跟 tk scaling 走）

只做前者的话，250% 下字号对了，但间距会挤成一团。

---

## 子进程不弹控制台窗口

### 现象

窗口程序（`--windowed` 的 exe，或 `pythonw.exe` 跑的源码）自身没有控制台。
它去拉 `powershell.exe` 这类**控制台程序**时，Windows 会**给子进程新建一个
控制台窗口**——用户看到的就是"点一下按钮，冒出一个黑框闪一下"。

### 修法

`win_quiet.quiet_kwargs()` 给所有 `subprocess` 调用加上：

```python
creationflags = CREATE_NO_WINDOW      # 从根上不分配控制台
startupinfo.dwFlags |= STARTF_USESHOWWINDOW; wShowWindow = SW_HIDE   # 双保险
```

注意 `CREATE_NO_WINDOW` 与 `DETACHED_PROCESS` 互斥，不能同时用。

### 怎么验证（别靠肉眼）

用 `EnumWindows` 枚举可见顶层窗口，按类名挑控制台窗口。**不要只匹配
`ConsoleWindowClass`**：Win11 的控制台由 Windows Terminal 托管，类名是
`CASCADIA_HOSTING_WINDOW_CLASS`（还有 `PseudoConsoleWindow`），
只匹配老类名会得出"没问题"的错误结论。

现成的：

```bash
pythonw tools/no_popup_check.py --exe "dist\WorkBuddy签到.exe" -- --sched-status
```

---

## 计划任务：为什么不用 schtasks.exe

- 命令行转义麻烦，中文路径极易踩编码坑
- 「随机抖动」（`RandomDelay`）几乎没法拼
- 一个任务挂多个触发器要另外写 XML

改用 PowerShell 的 `ScheduledTasks` 模块（Win8+ 自带），通过 `-EncodedCommand`
传脚本（Base64/UTF-16LE），彻底绕开引号与编码问题；结果以 JSON 输出。

### 权限设计

以「当前用户 + 仅登录时运行（InteractiveToken）+ 普通权限（Limited）」注册，
**不弹 UAC、不需要管理员**。代价是触发前提为"用户已登录"——本场景正好合适。

### 查状态为什么要分两条路

| 方法 | 耗时 | 拿得到什么 |
|---|---|---|
| `query_fast()` 直读 `System32\Tasks\<任务名>`（UTF-16 XML） | ~0.5 ms | 在不在、几点跑、指向哪个程序、抖动多少 |
| `query()` 走 PowerShell | **~5 秒** | 上面这些 + 下次/上次运行时间、上次返回码 |

启动自检、界面刷新一律走 `query_fast()`；只有「每日定时」对话框和
`--sched-status` 才用 `query()`。把 5 秒的查询挂在启动流程上，就是
"开程序后横幅转半天"的元凶。

> XML 里 `StartBoundary` 是 **UTC**，要换算成本地时间再展示。

### 状态码翻译

计划任务的返回码通常就是被调进程的退出码（本工具：0 成功、2 授权/环境问题、
其它非零异常），但 `SCHED_S_*` 系列**不是失败**：

| 码 | 含义 |
|---|---|
| 267009 | 正在运行 |
| 267011 | 尚未运行过（新建任务第一次触发前就是这个值，别误报） |
| 267014 | 就绪，尚未运行 |

---

## 打包：一个只有打完才知道的坑

**打包用的解释器必须带 tkinter。** 用不带 tkinter 的解释器打包，PyInstaller
**不报任何错**，只是悄悄少收集 `_tkinter.pyd` 和 tcl/tk 运行时，打出来的 exe
一启动就 `ModuleNotFoundError: No module named 'tkinter'` 直接崩。
唯一线索是体积小掉约 3 MB（正常约 11 MB，坏版约 8 MB）。

`build_exe.py` 因此做三件事：挑带 tkinter 的解释器 → 打包 → 跑一次 `--selftest`
校验，不通过就不放行。

### 无界面入口的输出重定向

`--windowed` 打包后 `sys.stdout` / `sys.stderr` 可能是 `None`，任何 `print()`
都会抛 `AttributeError`；即使存在，也可能是 GBK 文本流，中文 / emoji 触发
`UnicodeEncodeError`。这两种都会导致**签到明明成功、退出码却是非 0**，
从而被计划任务标记成失败。

所以无界面入口在冻结态无条件把输出重定向到 UTF-8 的 `checkin.log`；
源码态且有控制台时不抢用户的终端。

---

## 回归检查

| 脚本 | 覆盖什么 | 依赖 |
|---|---|---|
| `tools/oauth_mode_check.py` | 轮询、字段兼容、令牌轮换、401 重试、12153、导入码、推送渠道筛选 | 本地桩服务器，不碰真接口 |
| `tools/no_popup_check.py` | 运行时是否弹出控制台窗口 | 需要 `pythonw` 运行，Windows |

```bash
python tools/oauth_mode_check.py       # 39 项断言
```
