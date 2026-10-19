# 排查手册

按现象找，不用通读。所有问题的第一手证据都在**数据目录**的 `checkin.log` 里。

---

## 0. 先确认：你看的是哪一份数据？

三种运行方式各写各的目录，**最容易误判的地方就在这里**：

| 运行方式 | 启动 | 数据目录 |
|---|---|---|
| Windows 版（exe） | `WorkBuddyCheckin.exe` | `%LOCALAPPDATA%\WorkBuddyCheckin` |
| 服务端版（单文件） | `python3 wb_server.pyz` | 脚本所在目录 |
| 源码运行（开发） | `python src/desktop/checkin_gui.py` | 仓库根目录 |

界面日志第一行、`--version`、`--selftest` 都会打印数据目录。先对一下：
如果你在改根目录的 `config.json`，而实际跑的是 exe，那当然"改了没反应"。

---

## 1. 先跑一次自检

```bash
WorkBuddyCheckin.exe --selftest       # 结果写 数据目录\selftest.json
WorkBuddyCheckin.exe --diagnose       # 授权 + 接口连通性（不含令牌）
WorkBuddyCheckin.exe --version        # 版本 / 运行方式 / 数据目录
```

`selftest.json` 重点字段：

| 字段 | 期望 |
|---|---|
| `frozen` | exe 必须是 `true`；`false` 说明你跑的是源码 |
| `data_dir` / `data_origin` | 应为缓存目录 / `用户缓存目录` |
| `install_dir` | exe 所在目录（不写数据，只用来确认程序在哪） |
| `token.present` / `expired` / `disabled` | 授权状态 |
| `schedule.exists` / `command` | 任务是否存在、指向哪个程序 |

---

## 2. 提示「尚未授权」

数据目录里没有 `auth_oauth.json`，或文件不完整。此时界面上除「立即授权」外
**全部按钮都是灰的**——这是设计好的门禁，不是按钮坏了。

1. 点 **立即授权**，把链接复制到手机打开、登录确认即可
2. 浏览器走不通时（企业策略 / 代理拦截）：在别处命令行授权后，
   `WorkBuddyCheckin.exe --import-token "<导入码>"` 导入
   （界面没有导入入口，只在命令行）

---

## 3. 提示「授权已失效」/ 收到「授权还剩 2 天」

`refreshToken` 也过期了：约 30 天没跑过，或被提前撤销。重新授权一次即可。

「还剩 N 天」是**每次签到后顺手检查**发出来的（阈值 `expiry_warn_days`，
默认 2 天）。它出现说明**自动续期没成功**——别等，直接重新授权。

> 隐蔽情况：同一账号在**两台机器**上都在跑（比如 Windows 版和服务端版同时定时）。
> `refreshToken` 每次刷新都会轮换，两边互相踢，服务端返回 `12153`。
> 只留一处运行，另一处的定时删掉。

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
WorkBuddyCheckin.exe --sched-status
```

windowed exe 没有控制台，`print` 没人看见，所以结果同时写进 `checkin.log`：

```
定时签到：已设置
  任务名    WorkBuddyCheckin
  状态      Ready
  触发时间  09:00
  下次运行  2026-10-20T09:07:41
  上次运行  2026-10-19T09:11:03（成功）
  执行命令  D:\dist\WorkBuddyCheckin.exe --run
  指向当前  是
```

### 6.2 对照常见原因

| 现象 | 原因 | 处理 |
|---|---|---|
| 状态行「未注册」 | 只改了配置，没点「应用到系统」 | 点一次 |
| 状态行「路径失效」 | exe 挪了位置 | 打开程序会自动修；或重新「应用到系统」 |
| 「指向当前 否」 | 任务还指向旧路径（如旧版入口脚本） | 同上 |
| 上次结果「未完成（客户端未就绪或未登录）」，退出码 2 | 授权没了 | 重新授权 |
| 上次结果系统标记异常（0x…） | 实例冲突 / 被安全软件拦 | 稍后重试，检查杀软 |
| 上次结果「尚未运行过」 | 新建任务还没到点 | 不是故障 |
| 到点但电脑关机 / 休眠 | 已开「错过后尽快启动」 | 开机后自动补跑 |

> 任务里存的是 **exe 的绝对路径**。exe 换目录后，打开一次程序即可自动修正
> （v3.0 起）。源码运行**不会**改这个任务，避免开发动作影响已部署的机器。

### 6.3 手动跑一次，区分任务问题还是程序问题

```bash
WorkBuddyCheckin.exe --run
```

和计划任务调的是同一个入口。这条能成、定时不成 → 问题在任务注册；
这条也不成 → 问题在授权或网络，先看 `checkin.log`。

---

## 7. 点按钮闪出黑框

### 现象

点按钮时弹出一个 PowerShell 窗口，一闪而过。

### 原因

窗口程序自身没有控制台，拉起 `powershell.exe` 时 Windows 会给它新建一个。
修法见 [architecture.md](architecture.md#9-子进程不弹控制台窗口)：
所有子进程统一加 `CREATE_NO_WINDOW` + `startupinfo(SW_HIDE)`。

### 自己确认

```bash
pythonw tools/no_popup_check.py --exe "dist/WorkBuddyCheckin.exe" -- --sched-status
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
3. 企业微信：key 是 webhook 链接里 `?key=` 后面那段（贴整条链接也能自动抽）
4. 钉钉：机器人安全设置选「自定义关键词」时，关键词要含「签到」
5. 两个渠道互相独立，一个挂了不影响另一个

> 文案里带 emoji。个别企业微信老版本会显示成方框，不影响送达。

---

## 9. 服务端（cron）

完整说明见 [server.md](server.md)，这里只列最常卡住的地方。

### 先别猜，跑体检

```bash
python3 wb_server.pyz --check
```

不联网，只告诉你授权状态和每个渠道是「可用 / 勾了没填 key / 填了没勾 / 未启用」。
没有可用渠道时退出码 `2`。

### 卡在授权

- 默认每轮等 180 秒，最多 3 轮，轮间退避 60 / 120 秒；
- 3 轮都没点 → 推失败通知，退出码 `2`；
- 想留更长时间：`--wait 300` 或改 `auth_wait_seconds`；
- 想先单独把授权做完：`--auth-only`。

### 收不到二维码 / 扫了没反应

服务端推给你的是**二维码图片链接**（`open.weixin.qq.com/connect/qrcode/...`）：

- 手机上点开 → 长按图片 →「识别图中二维码」；电脑上点开则用手机扫屏幕；
- 链接约 5 分钟失效（`state` 只在等待窗口内有效）。过期就重跑，会重新申请；
- 扫完还是不动时，看控制台打印的轮询码：
  `code=11217` 说明还没扫成功，`12153` 说明会话已失效（本轮作废）。

`--check` 里「二维码授权：不可用」= 库或浏览器缺一个：

```bash
pip install playwright
python -m playwright install --with-deps chromium    # Linux
```

**只装库不够**：库在、浏览器没下，启动时会报
`Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome`。
脚本按 chrome → msedge → 系统 chromium/chrome → playwright 自带 chromium
的顺序找，有一处能起就行；也可以用环境变量直接指过去：

```bash
export WB_BROWSER_PATH=/usr/bin/chromium
export PLAYWRIGHT_BROWSERS_PATH=/home/xxx/.cache/ms-playwright
```

装完再跑 `--check` 应显示「可用（复用系统浏览器：…）」或「可用（playwright 自带：chromium-xxxx）」。

**装不上就直接退出**：缺 playwright、缺浏览器、浏览器起不来时，脚本列出
每种来源失败的原因 + 该敲的命令，立刻以退出码 `2` 结束（约 1 秒），
**不推链接、不等待、不重试**。因为没有二维码就扫不了码，等下去也永远到不了
`code=0`，白占十几分钟。cron 里看到 `2` 就是环境没准备好。

> `cron 里起不来、手工跑正常`：cron 多半是 root，而浏览器装在你自己用户下。
> `~/.cache/ms-playwright` 是按用户分的，两边要一致，或指
> `PLAYWRIGHT_BROWSERS_PATH`。

实在跑不起浏览器时，可以用 `--no-qr` 退回推授权页链接
（只有桌面浏览器打开能完成登录）。

### 推送不出去

服务器要能访问 `qyapi.weixin.qq.com` / `oapi.dingtalk.com`。很多内网只放行白名单。
单独试推送：`--push-only --url "https://example.com"`。

### cron 里不跑，手工跑正常

- 没有 `cd` 到脚本目录，或用了相对路径
- cron 的 `PATH` 很短，`python3` 找不到 → 写绝对路径
- 用干净环境验证：

```bash
env -i HOME=/root PATH=/usr/local/bin:/usr/bin:/bin \
  sh -c 'cd /opt/wbcheckin && python3 wb_server.pyz --check'
```

### 服务端读的是哪份配置

默认是**脚本同目录**的 `config.json`。放别处：

```bash
python3 wb_server.pyz --config /etc/wbcheckin/config.json
```

此时日志和令牌也跟着写到 `/etc/wbcheckin/`。

### 导入码报「无法解析」

多半是复制不全。整段重新复制，或直接用文件：
`WorkBuddyCheckin.exe --import-token D:\path\auth_oauth.json`。

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

它会挑解释器、写入版本资源、打包、跑 `--selftest` 校验，不通过直接报错。
手工跑 PyInstaller 时**必须带 `--paths src`** 并指向 `build/entry.py`，
完整命令见 [build.md](build.md)。

### 源码能跑，exe 不能

先看 `selftest.json` 的 `data_dir` 和 `config_path`。最常见的两类：
路径问题（`frozen` 不是 `true`，跑的还是源码）、缓存目录没权限
（极少见，`%LOCALAPPDATA%` 一般都可写）。

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

位置见第 0 节。从 v2.x 升级后，旧的 exe 旁边可能还留着一份
`config.json` / `auth_oauth.json`——已复制到缓存目录，确认没问题后可以删。
