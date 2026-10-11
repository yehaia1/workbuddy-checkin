# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

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

### 文档

- README 全面重写，新增目录、特性表、免责声明、项目结构。
- 新增 `CHANGELOG.md`、`docs/architecture.md`、`docs/troubleshooting.md`。
- 新增 `config.example.json` 配置样例。

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
