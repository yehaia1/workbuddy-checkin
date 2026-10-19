# 排查手册

按现象找，别按顺序读。所有问题的第一手证据都在 exe 旁边的 `checkin.log` 里。

---

## 第一步：先跑一次自检

```bash
WorkBuddy签到.exe --selftest                 # 结果写进 selftest.json
WorkBuddy签到.exe --diagnose                 # 授权 + 接口状态（不显示令牌内容）
```

界面上点「只查状态」也能顺带验证令牌是否可用。

---

## 提示「尚未授权」

本机没有 `auth_oauth.json`，或者文件不完整。

三种办法：

1. 「账号授权」→ **立即授权**（本机浏览器，最快）
2. 「账号授权」→ **推到手机授权**（机器没浏览器时用）
3. 服务器跑 `wb_auth_server.py`，把输出的 `WBAT1.…` 导入码粘到 **导入授权**

---

## 提示「授权已失效」

`refreshToken` 也过期了：约 30 天没跑过，或服务端提前撤销。重新授权一次即可。

> 还有一种隐蔽的情况：**刚在服务器授权完，又在本机导入了旧令牌**。
> 两边刷新会互相踢掉（`refreshToken` 轮换），服务端返回 `12153`。
> 令牌只能有一份，重新授权一次就好。

---

## 签到失败返回 401 / 403

不是网络问题，是令牌被拒。程序会自动刷新并重试一次，仍失败就说明
会话真的死了。跑 `--diagnose` 确认，然后重新授权。

---

## 网络不通 / 超时

- 检查代理：程序走系统默认的 `urllib` 环境，`HTTPS_PROXY` 会生效
- 确认能访问 `copilot.tencent.com` 和 `www.workbuddy.cn`
- 公司网络拦 TLS 的情况，用 `--diagnose` 会直接报 `OAUTH_NETWORK`

---

## 定时不生效

### 1. 看系统里到底有没有这个任务

```bash
WorkBuddy签到.exe --sched-status
```

windowed exe 没有控制台，`print` 出去没人看见，所以结果同时写进 `checkin.log`。
看到的内容长这样：

```
定时签到：已设置
  任务名    WorkBuddyCheckin
  状态      Ready
  触发时间  09:00
  下次运行  2026-10-20T09:07:41
  上次运行  2026-10-19T09:11:03（成功）
  执行命令  D:\workbuddy-checkin\WorkBuddy签到.exe --run
```

也可以直接看「每日定时」对话框里的系统状态行，或者打开系统自带的任务计划程序。

### 2. 对照常见原因

| 现象 | 原因 | 处理 |
|---|---|---|
| 状态行显示「未注册」 | 只改了配置，没点「应用到系统」 | 点一次「应用到系统」 |
| 状态行显示「路径失效」 | exe 挪了位置 | 重新「应用到系统」 |
| 上次结果「未完成（客户端未就绪或未登录）」，退出码 2 | 授权失效 | 重新授权 |
| 上次结果系统标记为异常（0x…） | 任务实例冲突 / 被拒绝 | 稍后重试；检查是否被安全软件拦 |
| 上次结果「尚未运行过」 | 新建任务，还没到点 | 不是故障，等它跑一次 |
| 到点了但电脑关机 / 休眠 | 已开「错过后尽快启动」 | 开机后会自动补跑一次 |

### 3. 手动跑一次，看是不是任务本身的问题

```bash
WorkBuddy签到.exe --run
```

和计划任务调的是同一个入口。这条命令能成、定时不成 → 问题在任务注册；
这条也不成 → 问题在授权或网络，先看 `checkin.log`。

---

## 点按钮闪出黑框

### 现象

点按钮时弹出一个 PowerShell 窗口，一闪而过。

### 原因

窗口程序自身没有控制台，拉起 `powershell.exe` 时 Windows 会给它新建一个。
修法见 [architecture.md](architecture.md#子进程不弹控制台窗口)：
所有子进程统一加 `CREATE_NO_WINDOW` + `startupinfo(SW_HIDE)`。

### 自己确认一遍

```bash
pythonw tools/no_popup_check.py --exe "dist\WorkBuddy签到.exe" -- --sched-status
```

它用 `EnumWindows` 枚举全系统可见顶层窗口，报告期间出现了几个控制台类窗口，
`0` 表示安全。

**自己写检查脚本时的两个坑**：

1. 必须用 `pythonw.exe` 跑检查脚本。用 `python.exe` 自己带控制台，
   子进程会继承它，根本复现不出弹窗。
2. 不要只匹配 `ConsoleWindowClass`。Win11 的控制台由 Windows Terminal 托管，
   类名是 `CASCADIA_HOSTING_WINDOW_CLASS`（还有 `PseudoConsoleWindow`）。

---

## 推送没收到

1. 「消息推送」里点 **发送测试**，界面会直接给出每个渠道的成败
2. 检查是不是**没勾渠道**：勾了才会推
3. 企业微信：key 是 webhook 链接里 `?key=` 后面那段，不要带整条链接
   （粘整条也能识别，但容易粘错）
4. 钉钉：机器人安全设置若选「自定义关键词」，关键词要包含「签到」
5. 两个渠道互相独立，一个挂了不影响另一个

> 消息文案里带 emoji。个别企业微信老版本会显示成方框，不影响送达。

---

## 服务端脚本相关问题

### 推送不出去

服务器上跑，检查这台机器能不能访问 `qyapi.weixin.qq.com` /
`oapi.dingtalk.com`。很多内网机器只放行白名单。

### 授权链接点开是空的 / 打不开

链接有效期有限（默认 10 分钟），过期就重新跑一次脚本。

### `--handoff` 模式 Windows 端取不到令牌

- Windows 端要能访问服务器端口（`telnet <ip> 8787` 试一下）
- 端口被占用时脚本会自动换一个，用控制台打印出来的那个地址
- secret 一次性有效：取过之后再取会返回 `410`

### 导入码报「无法解析」

多半是复制不全（换行被截断也行，但缺字符不行）。整段重新复制，
或让服务端用 `--save-token` 导出文件后走「从文件导入」。

---

## 界面字太小 / 太大

程序按系统 DPI 自动缩放，没有手动开关。如果明显不对：

1. 确认没有在 exe 属性 → 兼容性里勾「替代高 DPI 缩放行为」
2. 多屏且缩放比例不同的机器，把窗口拖到主屏再看
3. `--selftest` 里的 `system_dpi` 是程序读到的实际值，120 = 125%，144 = 150%

---

## 打包相关

### 提示没有 pyinstaller

```bash
python -m pip install --target .build/pylibs pyinstaller
```

### 打出来的 exe 一启动就崩

九成是打包解释器没有 tkinter。`build_exe.py` 会自动挑解释器并在打包后自检，
正常情况下不会发生；如果你手动跑了 `pyinstaller`，请改用
`python build_exe.py`。

### 源码态能用，exe 不能用

先跑 `WorkBuddy签到.exe --selftest`，把 `selftest.json` 和 `checkin.log`
一起看。最常见的两类：路径问题（配置读到了别处）和权限问题
（exe 放在 `Program Files` 下，写不了日志）。

---

## 日志与配置文件

| 文件 | 内容 | 能否删 |
|---|---|---|
| `config.json` | 推送渠道、key、定时设置 | **别删**，删了要重配 |
| `auth_oauth.json` | 明文令牌（access + refresh） | 删了要重新授权 |
| `checkin.log` | 执行结果摘要，无敏感信息 | 随便删 |
| `selftest.json` | `--selftest` 的输出 | 随便删 |

前两个都在 `.gitignore` 里，**别提交、别分享**。
