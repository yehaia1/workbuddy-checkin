# 排查手册

按现象找，不用通读。所有问题的第一手证据都在**数据目录**的 `checkin.log` 里。

---

## 0. 先分清：你看的是哪一套数据？

现在有两种运行方式，各自有独立的配置与日志，**最容易误判的地方就在这里**：

| | 本地运行 | 独立运行 |
|---|---|---|
| 启动 | `python run.py` | `dist/WorkBuddy签到.exe` |
| 数据目录 | 仓库根目录 | `dist/` |
| 计划任务调用 | `pythonw run.py --run` | `WorkBuddy签到.exe --run` |

界面日志区第一行会打印"运行方式"和"程序目录"，先对一下：如果你在改根目录的
`config.json`，而实际跑的是 `dist/` 里的 exe，那当然"改了没反应"。

---

## 1. 先跑一次自检

```bash
# 独立运行
dist/WorkBuddy签到.exe --selftest
dist/WorkBuddy签到.exe --diagnose

# 本地运行
python run.py --selftest
python run.py --diagnose
```

`--selftest` 写 `selftest.json`，重点字段：

| 字段 | 期望 |
|---|---|
| `frozen` | 独立运行必须是 `true`；`false` 说明你跑的是源码 |
| `app_dir` | 独立运行应为 `dist`，本地运行为仓库根目录 |
| `config_path` / `token_file` | 是否落在你以认为的目录 |
| `token.present` / `expired` / `disabled` | 授权状态 |
| `schedule.exists` / `command` | 任务是否存在、指向哪个程序 |

---

## 2. 提示「尚未授权」

本机没有 `auth_oauth.json`，或文件不完整。此时界面上除「立即授权」外**全部按钮
都是灰的**——这是设计好的门禁，不是按钮坏了。

1. 点 **立即授权**，在本机浏览器登录一次即可
2. 这台机器没有浏览器 / 没人守着 → 走服务端流程：服务器跑
   `wb_auth_server.py`，再用命令行把令牌交给这台机器
   （`--import-token` 或 `--pull-token`，见 [server.md](server.md)）。
   **界面里没有导入入口**，只能在命令行做。

---

## 3. 提示「授权已失效」/ 收到「授权还剩 2 天」

`refreshToken` 也过期了：约 30 天没跑过，或服务端提前撤销。重新授权一次即可。

「还剩 N 天」的提醒是**每次签到后顺手检查**发出来的（阈值 `expiry_warn_days`，
默认 2 天）。它出现说明**自动续期没成功**——别等，直接重新授权。

> 隐蔽情况：**服务器那边还留着一份令牌在续期**（比如用了 `--save-token`），
> 两边刷新会互相踢掉（`refreshToken` 轮换），服务端返回 `12153`。
> 令牌只能有一份，删掉多余那份。

---

## 4. 签到失败返回 401 / 403

不是网络问题，是令牌被拒。程序会自动刷新并重试一次，仍失败说明会话真的死了。
跑 `--diagnose` 确认，然后重新授权。

---

## 5. 网络不通 / 超时

- 检查代理：走 `urllib` 默认环境，`HTTPS_PROXY` 会生效
- 确认能访问 `copilot.tencent.com` 与 `www.workbuddy.cn`
- 公司网络拦 TLS 时，`--diagnose` 会直接报 `OAUTH_NETWORK`

---

## 6. 定时不生效

### 6.1 看系统里到底有没有任务

```bash
WorkBuddy签到.exe --sched-status     # 独立运行
python run.py --sched-status         # 本地运行
```

windowed exe 没有控制台，`print` 没人看见，所以结果同时写进 `checkin.log`：

```
定时签到：已设置
  任务名    WorkBuddyCheckin
  状态      Ready
  触发时间  09:00
  下次运行  2026-10-20T09:07:41
  上次运行  2026-10-19T09:11:03（成功）
  执行命令  D:\workbuddy-checkin\WorkBuddy签到.exe --run
```

### 6.2 对照常见原因

| 现象 | 原因 | 处理 |
|---|---|---|
| 状态行「未注册」 | 只改了配置，没点「应用到系统」 | 点一次 |
| 状态行「路径失效」 | 程序挪了位置 | 重新「应用到系统」 |
| 上次结果「未授权 / 令牌失效」，退出码 2 | 授权没了 | 重新授权 |
| 上次结果系统标记异常（0x…） | 实例冲突 / 被安全软件拦 | 稍后重试，检查杀软 |
| 上次结果「尚未运行过」 | 新建任务还没到点 | 不是故障 |
| 到点但电脑关机 / 休眠 | 已开「错过后尽快启动」 | 开机后自动补跑 |

> 本地运行和独立运行注册的是**同一个任务名** `WorkBuddyCheckin`。
> 后应用的会覆盖前一个：在源码态点过「应用到系统」，之后又换成 exe，
> 必须重新应用一次，否则任务还在调旧的 `run.py`。

### 6.3 手动跑一次，区分任务问题还是程序问题

```bash
WorkBuddy签到.exe --run
```

和计划任务调的是同一个入口。这条能成、定时不成 → 问题在任务注册；
这条也不成 → 问题在授权或网络，先看 `checkin.log`。

---

## 7. 点按钮闪出黑框

### 现象

点按钮时弹出一个 PowerShell 窗口，一闪而过。

### 原因

窗口程序自身没有控制台，拉起 `powershell.exe` 时 Windows 会给它新建一个。
修法见 [architecture.md](architecture.md#8-子进程不弹控制台窗口)：
所有子进程统一加 `CREATE_NO_WINDOW` + `startupinfo(SW_HIDE)`。

### 自己确认

```bash
pythonw tools/no_popup_check.py --exe "dist/WorkBuddy签到.exe" -- --sched-status
```

它用 `EnumWindows` 枚举全系统可见顶层窗口，报告期间出现了几个控制台类窗口，
`0` 表示安全。

**自己写检查脚本时的两个坑**：

1. 必须用 `pythonw.exe` 跑检查脚本。用 `python.exe` 自带控制台，子进程会继承，
   根本复现不出弹窗。
2. 不要只匹配 `ConsoleWindowClass`。Win11 的控制台由 Windows Terminal 托管，
   类名是 `CASCADIA_HOSTING_WINDOW_CLASS`（还有 `PseudoConsoleWindow`）。

---

## 8. 推送没收到

1. 「消息推送」里点 **发送测试**，界面会给出每个渠道的成败
2. 检查是不是**没勾渠道**：勾了才会推，填了 key 不算
3. 企业微信：key 是 webhook 链接里 `?key=` 后面那段，不要带整条链接
4. 钉钉：机器人安全设置选「自定义关键词」时，关键词要含「签到」
5. 两个渠道互相独立，一个挂了不影响另一个

> 文案里带 emoji。个别企业微信老版本会显示成方框，不影响送达。

---

## 9. 服务端脚本

完整说明见 [server.md](server.md)，这里只列最常卡住的地方。

### 先别猜，跑体检

```bash
python src/wb_auth_server.py --config /etc/wbcheckin/config.json --check
```

它不联网，只告诉你每个渠道是「可用 / 勾了没填 key / 填了没勾 / 未启用」。
没有可用渠道时退出码 `2`。

### 推送不出去

服务器要能访问 `qyapi.weixin.qq.com` / `oapi.dingtalk.com`。很多内网只放行白名单。
单独试推送：`--push-only --url "https://example.com"`。

### 授权链接点开是空的 / 打不开

链接有效期有限（默认 10 分钟），过期就重新跑一次脚本，或把 `--timeout` 调大。

### `--handoff` 取不到令牌

- Windows 端要能访问服务器端口（先在 Windows 上 `curl http://<ip>:8787/status`）
- 端口被占用时脚本会自动换一个，用控制台打印出来的那个地址
- secret 一次性有效：取过之后再取会返回 `410`
- 超过 `--handoff-timeout`（默认 600 秒）没人取，脚本退出码 `4`

### 导入码报「无法解析」

多半是复制不全。整段重新复制；或让服务端用 `--save-token` 导出文件，再
`--import-token <文件路径>`。

### 服务端读的是哪份配置

默认是**数据目录**的 `config.json`。服务器上没有仓库结构时显式指定：

```bash
python src/wb_auth_server.py --config /etc/wbcheckin/config.json
```

还没有配置文件就先 `--init-config` 生成一份模板。

---

## 10. 界面字太小 / 太大

程序按系统 DPI 自动缩放，没有手动开关。如果明显不对：

1. 确认 exe 属性 → 兼容性里没有勾「替代高 DPI 缩放行为」
2. 多屏且缩放不同的机器，把窗口拖到主屏再看
3. `--selftest` 的 `system_dpi` 是实际读到的值：120 = 125%，144 = 150%

---

## 11. 打包

### 提示没有 pyinstaller

```bash
python -m pip install --target .build/pylibs pyinstaller
```

### 打出来的 exe 一启动就崩

九成是打包解释器没有 tkinter。正常请用：

```bash
python build/build_exe.py
```

它会挑解释器、打包、跑 `--selftest` 校验，不通过直接报错。
手工跑 PyInstaller 时**必须带 `--paths src`**，否则找不到 `src/` 里的模块；
完整命令见 [build.md](build.md#等价的-pyinstaller-命令)。

### 源码能跑，exe 不能

先看 `dist/selftest.json` 的 `app_dir` 和 `config_path` 是不是都指向 `dist/`。
最常见的两类：路径问题（配置读到了别处）、权限问题（exe 放在 `Program Files`
下，写不了日志）。

### 打包时 PermissionError

旧 exe 还在运行（或计划任务刚拉起），文件被占用。结束进程后重试；
仍不行就换一个 `--distpath` 打包，再复制回 `dist/`。

---

## 12. 日志与配置文件

| 文件 | 内容 | 能否删 |
|---|---|---|
| `config.json` | 推送渠道、key、定时设置 | **别删**，删了要重配 |
| `auth_oauth.json` | 明文令牌（access + refresh） | 删了要重新授权 |
| `checkin.log` | 执行结果摘要，无敏感信息 | 随便删 |
| `selftest.json` | `--selftest` 的输出 | 随便删 |

它们的位置：

| 运行方式 | 位置 |
|---|---|
| 本地运行 | 仓库根目录 |
| 独立运行 | exe 所在目录（通常是 `dist/`） |
