# 排查手册

## 第一步永远是：跑自检

不开界面、不弹窗，最快拿到全部环境信息：

```bash
cd <exe所在目录>
WorkBuddyCheckin.exe --selftest
```

会在同目录生成 `selftest.json`，包含：

- 系统 DPI 与缩放比例
- 检测到的客户端安装路径、进程列表、是否在运行
- 模块搜索目录、`workbuddy_ipc` 导入失败原因
- IPC 是否连通、签到接口返回的 `code`

同时日志都写在 exe 旁边的 `checkin.log`（只有结果摘要，无敏感信息）。

---

## 点按钮闪出黑框

### 现象

点「定时签到」或其他按钮时，弹出一个 PowerShell 窗口。

### v1.5 已修复

原因和修法见 [architecture.md](architecture.md#子进程不弹控制台窗口)。简言之：
窗口程序自身没有控制台，拉起 `powershell.exe` 这类控制台程序时 Windows 会给它
新建一个控制台窗口；修法是所有子进程调用统一加
`creationflags=CREATE_NO_WINDOW` + `startupinfo(SW_HIDE)`。

如果你还想自己确认一遍（或者改了代码后想回归测试）：

```bash
python tools/no_popup_check.py
```

它会启动目标程序并**用 `EnumWindows` 枚举全系统可见顶层窗口**，
报告期间出现了几个控制台类窗口。`0` 表示安全。

### 自己写检查脚本时的坑

**不要只匹配 `ConsoleWindowClass`。** Win11 把控制台交给了 Windows Terminal 托管，
窗口类名变成了 `CASCADIA_HOSTING_WINDOW_CLASS`（还有 `OpenConsole.exe` 的
`PseudoConsoleWindow`）。只匹配一个类名会得出"没问题"的错误结论。

另外检查脚本本身要用 **`pythonw.exe`** 运行 —— 用 `python.exe` 的话它自带控制台，
子进程会继承控制台，**根本复现不出弹窗**。

---

## 定时不生效

按顺序查，前三步基本能定位。

### 1. 看任务在不在、下次什么时候跑

```powershell
Get-ScheduledTask -TaskName WorkBuddyCheckin | Format-List TaskName, State
Get-ScheduledTaskInfo -TaskName WorkBuddyCheckin
```

重点看：

- `NextRunTime` —— 下次执行时间。带抖动时它是**已经加过抖动的具体时刻**
  （例如 `09:07`），不是整点
- `LastTaskResult` —— 上次结果，`0` = 成功

### 2. 不开界面看一眼（v1.5+）

```bash
WorkBuddyCheckin.exe --sched-status
```

输出形如：

```
定时签到：已设置
  任务名    WorkBuddyCheckin
  状态      Ready
  触发时间  09:00
  下次运行  2026-10-12T09:12:46
  上次运行  2026-10-11T11:31:15（成功）
  执行命令  D:\...\dist\WorkBuddy签到.exe --run
```

结果同时会追加到 `checkin.log`（因为窗口程序没有控制台，`print` 没人看得见）。

### 3. 手动跑一次无界面入口

等价于计划任务做的事，**不会弹窗**：

```bash
WorkBuddyCheckin.exe --run
```

然后看 `checkin.log` 有没有新的结果行。

### 4. 结果码翻译

`LastTaskResult` 不全是退出码。`SCHED_S_*` 系列是**状态**，不是失败：

| 码 | 含义 | 算成功吗 |
|---|---|---|
| `0` | 成功 | ✅ |
| `2` | 未完成（客户端未就绪或未登录） | ❌ |
| `267009` | 正在运行 | ✅ |
| `267010` | 任务已被禁用 | ❌ |
| `267011` | **尚未运行过**（新建任务第一次触发前就是这个值） | ✅ |
| `267012` | 没有后续计划 | ❌ |
| `267013` | 任务被终止 | ❌ |
| `267014` | 就绪，尚未运行 | ✅ |

### 常见原因

| 症状 | 原因 | 处理 |
|---|---|---|
| 显示「任务指向的程序不是当前这个」 | exe 被挪走了 | 打开「定时签到」点一次「应用到系统」 |
| 到点完全没反应 | 触发条件要求"用户已登录"，锁屏或未登录时不触发 | 正常行为 |
| 错过了时间点 | 关机 / 休眠 | 已开 `StartWhenAvailable`，开机后会补跑 |
| 每次都是"未完成" | 到点时客户端没装 / 没登录 | 先跑 `--selftest` 看环境自检 |
| 提示"系统标记为异常" | 真实错误，看 `[20xx]` 的十六进制码 | 结合 `checkin.log` 定位 |
| 同时跑了两个任务 | 界面定时 + 旧的 `setup_task.bat` 冲突 | 删掉旧的 `_0900` / `_2100` 任务 |

---

## 检测不到客户端 / 说我没登录

自检分了四层判断（[为什么这么设计](architecture.md#环境检测为什么要分两层)），
所以先看横幅说的是哪一句：

| 提示 | 含义 | 处理 |
|---|---|---|
| 客户端已安装，但当前没有运行 | 找到路径了，只是没开 | 点横幅上的「打开客户端并签到」 |
| 未找到 WorkBuddy 客户端 | 四个来源都没找到 exe | 「设置 → WorkBuddy 客户端」手动指定，或点「自动检测」 |
| 在跑但通道没就绪 | 客户端刚启动，宿主通道还没建好 | 稍等再点「重新自检」 |
| 在跑但没登录 | IPC 真连了一次，确认未登录 | 点「打开客户端」去登录 |

> 光看账号快照文件（`account-snapshot.json`）**不可靠** —— 登出后文件可能残留。
> 所以程序会**真的连一次 IPC** 来确认登录态。这是唯一可靠的判据。

---

## 界面字太小

先确认系统缩放比例，再看程序有没有正确感知 DPI。
「设置 → 界面 → 显示缩放」可以额外放大 10% / 25% / 50%（重启生效）。

如果怀疑程序没感知到 DPI，跑一次 `--selftest`，看 `system_dpi` 和 `scale_percent`：

- `system_dpi` 应该是 96 的倍数倍（如 `120` = 125%、`240` = 250%）
- 如果显示 `96` 但系统缩放是 250%，说明 DPI 感知没生效

原理见 [architecture.md](architecture.md#高-dpi为什么字会变小)。

---

## 签到失败

`checkin.log` 里会记一条 JSON，关键字段是 `result`：

| `result` | 含义 |
|---|---|
| `CLAIMED` | 领取成功 |
| `ALREADY_CLAIMED` | 今日已签到（**正常**，不是错误） |
| `NOT_ACTIVE` | 活动未开始 / 已结束 |
| `NOT_LOGGED_IN` | 未登录 |
| `CLIENT_NOT_RUNNING` | 客户端没运行 |
| `IPC_ERROR` | 宿主通道调用失败 |

退出码约定：`0` 成功、`2` 环境问题（客户端缺失 / 未登录）、其他为异常。

> `result` 是 `ALREADY_CLAIMED` 时退出码依然是 `0`，计划任务不会标记失败。

---

## 推送没收到

1. 先点界面上的「**测试推送**」，它会向所有已配置渠道各发一条
2. 只填了 key 却收不到：
   - 企业微信：确认 key 是 webhook 链接 `?key=` 后面那段
   - 钉钉：确认是 `access_token=` 后面那段；若安全设置选了「自定义关键词」，
     关键词需包含「签到」二字（消息文案里已带）
3. 想确认程序拼出来的地址对不对：地址是按**官方域名**拼的，不接受自定义域名。
   看着像链接却抽不出参数时程序会**拒发**，避免消息被误推到第三方地址
4. 主界面配置摘要里的「推送」应显示已启用的渠道名（如「企业微信」）

---

## 打包相关

### 打包报错没有 pyinstaller

```bash
python -m pip install --target .build/pylibs pyinstaller
```

### 打出来的 exe 一启动就崩

按顺序怀疑：

1. **tkinter 没进包**。看体积：正常约 **11 MB**，只有约 **8 MB** 说明少了
   `_tkinter.pyd` 和 tcl/tk 运行时，是打包解释器不带 tkinter 导致的。
   v1.5 起 `build_exe.py` 会自动拦截并校验，正常情况下不会再出现。
2. **动态导入的依赖没进包**。症状是报 `ModuleNotFoundError: No module named 'hmac'`，
   并被误判成"客户端未登录"。用 `build_exe.py` 打包会自动带上这批 `--hidden-import`。

### 打包用的解释器

必须带 tkinter。可以用 `--python` 显式指定：

```bash
python build_exe.py --python C:\path\to\python.exe
```

> 有些环境的 Python 只有 `python3.exe` / `pythonw3.exe` 而没有 `python.exe`
> （例如通过非标准方式安装的精简版），此时 `py -3.13` 之类的写法不可用，
> 直接用实际存在的可执行文件路径即可。

---

## 日志与自检产物

| 文件 | 位置 | 内容 |
|---|---|---|
| `checkin.log` | exe 旁边 | 每次运行的结果摘要、无界面模式的完整输出 |
| `config.json` | exe 旁边 | 配置（**含机器人 key，不要分享**） |
| `selftest.json` | 运行 `--selftest` 时的当前目录 | 环境自检明细（**含本机路径与账号 uid，不要分享**） |

后两个文件已在 `.gitignore` 中排除。
