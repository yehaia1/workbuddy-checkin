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

- **界面不能常驻**。用户不会为了签到一直开着窗口，而且常驻会占内存
  （实测约 78 MB）。
- **调度必须交给系统**。自己写循环 sleep 的话，电脑重启、休眠、关机就全废了。
- **执行必须无界面**。计划任务调起的进程如果弹窗，会在用户毫无预期的时候打断他。

三者之间只有两条契约：计划任务里记录的**可执行文件路径 + `--run` 参数**，
以及 `config.json`。

### 单次签到流程

```
查状态 ──▶ 已签到？ ──是──▶ 直接返回 ALREADY_CLAIMED（幂等）
              │
              否
              ▼
          领积分 ──▶ code=10001 表示"今天已领过"（并发下的兜底）
              │
              ▼
          复核状态 ──▶ 确认今日已签到
              │
              ▼
          消息推送（按已配置渠道逐个发，互不影响）
```

整体是**幂等**的：重复执行不会重复领取，只会在日志里记一条"今日已签到"。

---

## 环境检测：为什么要分两层

"检测不到客户端"是最容易误报的地方，所以判断被刻意拆成两层：

| 层次 | 判据 | 为什么不能合并 |
|---|---|---|
| 装没装 | 配置路径 → 运行中进程的镜像路径 → 注册表 `Uninstall` / `App Paths` → 常见安装目录 | 客户端**没开**不等于**没装**，合并会把这两种情况混为一谈 |
| 在不在跑 | Win32 `CreateToolhelp32Snapshot` 枚举进程 | 只有进程在跑，宿主通道才可能存在 |
| 通道通不通 | `~/.workbuddy/wbipc/endpoint.json` 是否存在 | 通道文件不在，调接口必然失败 |
| 登录没登录 | 读账号快照 `primary.uid`，**再真连一次 IPC 调状态接口** | 登出后账号快照文件**可能残留**，只看文件会误判成"已登录" |

第 4 步是唯一可靠的一步。前三步都只是为了让报错信息更精确、让用户知道该点什么按钮。

> **为什么客户端通道必须先开着并登录**：签到接口的鉴权头由客户端**主进程**注入，
> 本程序不接触、不存储任何令牌。这条通道绕不过客户端进程。
>
> v1.7 起还有**账号直连**通道（`auth_mode: "oauth"`）：自行走设备授权换一份令牌，
> 纯 HTTP 签到，**不需要客户端在跑**。代价是令牌明文落在本机、调用身份变成 CLI。
> 两条通道的 `fetch(method, path)` 接口一致，业务代码共用一份（见 wb_oauth.py）。

---

## 内置模块发现：为什么不能写死路径

`workbuddy_ipc` 是客户端**自带**的模块（不是 pip 装的），运行时靠往 `sys.path`
插一个目录再 `import`。麻烦在于：**它的父目录在不同安装方式/版本下并不统一**。
实测同一台机器上就同时存在三种布局：

```
<安装目录>/resources/app.asar.unpacked/resources/plugins/
    workbuddy-builtin/skills/library/                          ← 主路径
    workbuddy-builtin/builtin-plugins/<插件>/skills/<技能>/     ← 插件式布局
<用户目录>/.workbuddy/plugins/cache/<厂商>/<插件>/<版本>/…/     ← 插件缓存
```

早期版本只探测前两类中的固定几条，一旦对不上就整体报
`IPC_MODULE_NOT_FOUND` —— 表现为"明明装了客户端也登录了，却说找不到模块"。
本机跑得好好的、换台机器就坏，根源就在这里。

### v1.6 的做法：约定路径 + 有界递归兜底

`module_roots()` 按可信度依次产出候选目录：

1. 配置里手工指定的 `ipc_dir`（最后一根救命稻草；**界面上不提供入口**，
   正常安装用不到，摆出来只会让使用者以为必须填——需要时手工改 config.json）
2. 环境变量（存在时）
3. 安装目录内的约定路径（命中即用，最快）
4. 用户插件缓存的多种 glob 布局
5. **有界递归扫描**：在安装目录 / 插件缓存 / `~/.workbuddy` 下逐层找
   `workbuddy_ipc` 的父目录（深度 ≤ 9，条目数封顶，跳过纯缓存目录）

实测代价：安装目录约 20 ms、插件缓存约 30 ms，整体 `module_roots()` 约 0.15 s。
因为一次运行里目录不会变，结果做了**进程级缓存**（`_MODULE_ROOTS_CACHE`，
key = 配置里的 `ipc_dir`），否则启动自检和 `load_ipc` 会各付一次扫描；
排查用的 `probe_module_roots()` 走 `refresh=True` 强制实扫，不吃缓存。

找到若干候选目录后仍然落空时，抛给用户的是一句干净的话
（"未找到内置模块 workbuddy_ipc。"），**候选目录数量、安装目录位置、
逐条导入失败原因只写进 `checkin.log` 与 `selftest.json`**。
早期版本把这些细节拼进报错正文，界面上等于用噪声挤掉了真正的建议。

### 两个踩过的坑

- **命中后不能提前收工**。同一个 base 下可能并存多种布局
  （`skills/workbuddy_ipc` 与 `skills/library/workbuddy_ipc` 同时存在），
  找到第一个就 `continue` 会把后者漏掉。
- **不能"跳过所有点号目录"**。`~/.workbuddy` 本身就是最关键的位置；
  必须用显式黑名单（`.git` / `.venv` / `node_modules` …）而不是前缀判断。

### 报错要能远程定位

失败时把 `候选目录 N 个；客户端安装目录 X` 拼进报错信息。这条信息决定了
远程排查是"再猜一轮"还是"一眼看出是哪个环节断了"。自检还会输出每条候选目录的
`exists` / `has_package`，见 `tools/module_discovery_check.py`。

---

## 后台运行：怎么做到不跳窗打断你

### 先试官方参数：没有

全量扫描了客户端的 `app.asar`，主进程的 `process.argv` 只解析 `--debug` 和 `--serve`。

网上能搜到的 `--hidden` / `--background`，在它的代码里**全是 CSS 类名**，
不是启动开关。所以**没有官方静默启动参数**。

### 所以改用"拉起后隐藏窗口"

客户端原生支持托盘 + 关闭到托盘，也就是说"**窗口隐藏、进程继续跑**"是它自己就支持的状态。
做法是：正常拉起客户端，然后用 Win32 `ShowWindow(SW_HIDE)` 把主窗口藏起来。

实测（`probe_hidden_launch.py --test-hide`）：

```
选中的主窗口：hwnd=2687368 title='WorkBuddy'
→ 隐藏… 成功
  隐藏后窗口 visible=False
  IPC 是否仍可用：OK          ← 关键：进程还在，宿主通道没断
→ 恢复窗口… 成功
```

**隐藏窗口不影响 IPC**，这条路成立。

### 代价：会闪现 1~5 秒

从"进程启动"到"隐藏成功"之间，窗口会闪现约 1~5 秒，可能抢一次焦点。
这是没有官方静默参数导致的，**绕不过去**。

---

## 高 DPI（为什么字会变小）

Windows 缩放到 125% / 150% / 250% 时，**DPI 感知必须在创建 Tk 之前声明**。

如果先建 `tk.Tk()` 再声明，时序是这样的：

1. Tk 初始化时进程还不感知 DPI → Windows 把屏幕虚拟化成 96 DPI
2. Tk 按 `96 / 72` 算完了所有字体
3. 之后进程才变成 DPI 感知 → 窗口不再被系统位图放大

结果就是**字体物理尺寸小 2.5 倍**（250% 缩放下）。本程序的做法：

1. `tk.Tk()` **之前**调用 `SetProcessDpiAwareness(1)`
2. 建完窗口后按实际 DPI 校准 `tk scaling`（管字体）
3. 边距、窗口尺寸这些**物理像素**参数统一乘一个 `PX` 系数（管间距）

另外两个高 DPI 的坑：

- tk 原生 `Checkbutton` 的指示器是**固定尺寸位图**，高 DPI 下会缩成一个小方块。
  设置窗口里的控件因此换成了 `ttk`（`clam` 主题，尺寸按字体度量算）。
- 250% 下 ttk(`clam`) 的 `Checkbutton` 指示器**画不出勾选态**，
  所以「定时签到」里的复选框改成了自绘开关（方框 + ✓ + `var.trace_add` 重绘）。

> 写截图 / 测量脚本时也要记得声明 DPI 感知，否则 `GetWindowRect` 返回的是被虚拟化的
> 逻辑坐标，量出来小 2.5 倍、`BitBlt` 会错位。

---

## 子进程不弹控制台窗口

### 现象

点界面上某些按钮时，会弹出一个 PowerShell 窗口。

### 原因

程序打包成**无控制台**的窗口程序后，自身没有控制台。
此时它去拉 `powershell.exe` / `tasklist.exe` / `taskkill.exe` 这些**控制台程序**，
Windows 找不到可继承的控制台，就会**给子进程新建一个控制台窗口**。

任务本身执行是正常的，纯粹是窗口不该出现。

### 修法

所有子进程调用统一加 `win_quiet.quiet_kwargs()`：

```python
subprocess.run(cmd, capture_output=True, timeout=30, **quiet_kwargs())
```

其中：

- `creationflags = CREATE_NO_WINDOW (0x08000000)` —— 从根上不分配控制台，
  管道读写一切照旧
- `startupinfo.dwFlags |= STARTF_USESHOWWINDOW; wShowWindow = SW_HIDE` —— 双保险

注意 `CREATE_NO_WINDOW` 与 `DETACHED_PROCESS` **互斥**，不能同时用。

### 怎么验证（别靠肉眼）

用 `EnumWindows` 枚举全系统可见顶层窗口，按类名判定：

| 类名 | 出现场景 |
|---|---|
| `ConsoleWindowClass` | 传统 conhost 控制台 |
| `CASCADIA_HOSTING_WINDOW_CLASS` | **Win11 交给 Windows Terminal 托管** |
| `PseudoConsoleWindow` | OpenConsole |

> **踩坑记录**：第一版探针只匹配 `ConsoleWindowClass`，结果对照组也是 0，
> 差点误判成"本来就没问题"。Win11 上必须把
> `CASCADIA_HOSTING_WINDOW_CLASS` 也算进去。

A/B 实测结果：

| 组 | 新出现的可见控制台窗口 |
|---|---|
| 裸调用 | **2 个**（`WindowsTerminal.exe` + `OpenConsole.exe`） |
| 加 `quiet_kwargs()` | **0 个** |

验证脚本（需要 `pythonw.exe` 运行以模拟无控制台环境）见
[troubleshooting.md](troubleshooting.md#点按钮闪出黑框)。

---

## 计划任务：为什么不用 schtasks.exe

改用 PowerShell 的 `ScheduledTasks` 模块（Win8+ 系统自带），原因有三个：

1. **命令行转义麻烦**，中文路径极易踩编码坑
2. **随机抖动（`RandomDelay`）几乎没法用命令行拼出来**
3. 一个任务挂多个触发器需要额外写 XML

实现方式是：把 PowerShell 脚本用 `-EncodedCommand` 以 **Base64 / UTF-16LE**
传进去，彻底绕开引号与编码问题；脚本把结果 `ConvertTo-Json` 输出，Python 侧解析。
这样即使失败也能拿到可读的错误原因。

### 权限设计

以「**当前用户 + `InteractiveToken` + `RunLevel Limited`**」注册：

- **不需要管理员权限，也不弹 UAC**
- 代价是触发条件为"用户已登录"——本场景正好合适

### 其它设置

- `StartWhenAvailable`：关机 / 休眠错过后，开机补跑一次
- `ExecutionTimeLimit` 1 小时：防止卡死的实例一直挂着
- 随机抖动是**计划任务原生能力**，不是脚本里 sleep：
  到点前由系统在窗口内挑一个时刻触发，所以"下次执行时间"显示的是已加过抖动的具体时刻

### 状态码翻译

`LastTaskResult` 不全是退出码，`SCHED_S_*` 系列是状态而不是失败，必须单独识别：

| 码 | 含义 | 是否算成功 |
|---|---|---|
| `0` | 成功 | ✅ |
| `2` | 未完成（客户端未就绪或未登录） | ❌ |
| `267009` | 正在运行 | ✅ |
| `267010` | 任务已被禁用 | ❌ |
| `267011` | **尚未运行过**（新建任务第一次触发前就是这个值） | ✅ |
| `267012` | 没有后续计划 | ❌ |
| `267013` | 任务被终止 | ❌ |
| `267014` | 就绪，尚未运行 | ✅ |

> 早期版本没做这张表，把 `267011` 误报成「系统标记为异常」。

---

## 打包：两个只有打完才知道的坑

1. **动态导入的依赖不会被自动收集**。`workbuddy_ipc` 是运行时从客户端安装目录
   动态 `import` 的，PyInstaller 静态分析看不到它，于是它依赖的
   `hmac` / `secrets` / `socket` / `ssl` 都不进包，exe 运行时报
   `ModuleNotFoundError: No module named 'hmac'`，还会被上层逻辑误判成"客户端未登录"。
   → 在 `build_exe.py` 里显式声明为 `--hidden-import`。

2. **打包用的解释器必须带 tkinter**。用不带 tkinter 的解释器打包，
   PyInstaller **不报任何错**，只是悄悄少收集 `_tkinter.pyd` 和 tcl/tk 运行时，
   打出来的 exe 一启动就 `ModuleNotFoundError: No module named 'tkinter'` 崩掉。
   **唯一线索是体积小掉约 3 MB**（正常约 11 MB，损坏版约 8 MB）。
   → `build_exe.py` 会主动挑一个带 tkinter 的解释器，并在打包后跑一次
   `--selftest` 校验，不通过直接报错、不放行。

### 无界面入口的输出重定向

`--windowed` 打包后，`sys.stdout` 可能是 `None`，也可能是 **GBK 文本流**。
两者都会让无界面执行"签到成功但退出码非 0"：

- `None` → `print()` 直接抛 `AttributeError`
- GBK 文本流 → 打印中文 / emoji 时抛 `UnicodeEncodeError`

计划任务只看退出码，于是会把成功的签到标记为失败。
所以 `run_headless()` 里**无条件**把 stdout / stderr 重定向到 UTF-8 的 `checkin.log`，
两个坑一起覆盖。
