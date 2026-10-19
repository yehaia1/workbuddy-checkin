# WorkBuddy 每日签到

**账号直连**版签到工具：授权一次，之后纯 HTTP 请求签到，不依赖 WorkBuddy 客户端。

界面只有三块：**账号授权 / 每日定时 / 消息推送**。定时交给 Windows 计划任务，
配好就能关掉程序，系统里零常驻。

![主界面](dist/ui_main.png)

---

## 两种运行方式，从一开始就分开

同一份逻辑，跑法有两种，**代码入口、数据目录、依赖都不一样**：

| | 本地运行 | 独立运行 |
|---|---|---|
| 入口 | `python run.py` | `dist/WorkBuddy签到.exe` |
| 目标机器 | 需要 Python 3.8+（带 tkinter） | 什么都不用装 |
| 配置 / 日志 | 仓库根目录 | exe 旁边 |
| 改代码 | 立即生效 | 需要重新打包 |
| 适合 | 开发、调试、自己用 | 发给别人、装到别的机器 |

两者**不共用配置文件**：根目录的 `config.json` 属于本地运行，`dist/config.json`
属于 exe。这是刻意的——调试时改坏配置，不会影响已经部署好的那一份。

> 打包入口是 `build/entry.py`，源码在 `src/`，构建脚本在 `build/`。
> 逻辑代码里没有任何"构建"相关的东西，构建脚本里也没有业务逻辑。

---

## 快速开始

### 独立运行（推荐给最终用户）

1. 下载 `WorkBuddy签到.exe`，放到一个不会被随手删掉的固定目录
2. 双击运行 → 首次会在 exe 旁边生成 `config.json`
3. 「账号授权」点 **立即授权**，浏览器登录一次
4. 想自动签到 → 「每日定时」设时间 → **应用到系统**，然后程序可以关了

> 计划任务里记的是 exe 的绝对路径，**挪动位置后要重新点一次「应用到系统」**。

### 本地运行（开发 / 自用）

```bash
git clone https://github.com/yehaia1/workbuddy-checkin.git
cd workbuddy-checkin
python run.py
```

只要求 **Python 3.8+ 且带 tkinter**（官方 Windows 安装包默认包含），
**零第三方依赖**，不需要 `pip install`。

---

## 界面说明

| 区域 | 内容 |
|---|---|
| 顶部横幅 | 环境状态一句话；授权缺失 / 失效 / 快到期 / 网络不通会直接给按钮 |
| 四张卡片 | 今日状态 / 连续签到 / 本期累计 / 活动截止 |
| 按钮区 | 立即签到、只查状态（**未授权时不可点**） |
| ① 账号授权 | 授权状态、到期时间 + 立即授权 |
| ② 每日定时 | 是否启用、每天几点、下次执行 + 设置定时 |
| ③ 消息推送 | 推到哪几个渠道 + 设置推送 / 发送测试 |
| 运行日志 | 全过程记录，右上角打开日志文件 |

**未授权时界面会锁住**：除了「立即授权」，其余按钮全部变灰——避免点一下就报错，
也避免误以为已经配好了。授权成功后自动恢复。

| 每日定时 | 消息推送 |
|---|---|
| ![定时](dist/ui_schedule.png) | ![推送](dist/ui_notify.png) |

---

## 账号授权

签到接口要登录态。本工具走 **CodeBuddy CLI 的设备授权流程**换一份令牌：

1. 申请授权会话 → 拿到一条登录链接
2. 在本机浏览器里登录并确认
3. 轮询到结果 → 令牌落到 `auth_oauth.json`

**本地版只有这一条路**（界面上的「立即授权」）。有效期约 28 天，到期前 5 天
自动续期；遇到 401 会刷新后重试一次。

### 到期提醒

每次签到结束后会检查令牌剩余天数：**≤ 2 天就往推送渠道发一条提醒**，
界面上横幅也会变黄。收到提醒说明自动续期没成功，点一次「立即授权」即可。
阈值是 `config.json` 里的 `expiry_warn_days`，设 `0` 关闭。

### 远程授权属于服务端流程

机器没人守、没有浏览器时，在**服务器**上跑 `wb_auth_server.py`：生成链接 →
推到企业微信/钉钉 → 手机点开登录 → 令牌用命令行交给 Windows。界面里没有
"推到手机"，也没有"导入"——那部分只走命令行，见 [docs/server.md](docs/server.md)。

> **令牌只有一份。** `refreshToken` 每次刷新都会轮换，两台机器各存一份会互相踢掉
> （服务端返回 `12153`，表现为"授权莫名失效"）。所以服务端默认不留令牌，
> 交出去之后由 Windows 独占续期。

详见 [docs/usage.md](docs/usage.md#1-账号授权)。

---

## 每日定时

- 时间可设多个，支持 `±N` 分钟随机抖动（避免整点集中请求）
- 写进 Windows 计划任务，任务名 `WorkBuddyCheckin`
- 触发时执行 `exe --run`（无界面），跑完自动退出
- 应用定时后程序默认自动关闭，系统里不留常驻进程

时间/抖动请**用界面改**：只编辑 `config.json` 不会同步到系统计划任务。

---

## 消息推送

企业微信 / 钉钉群机器人，**只填 key，不填完整链接**（地址由程序拼接）：

| 渠道 | 填什么 |
|---|---|
| 企业微信 | webhook 地址里 `key=` 后面那段 |
| 钉钉 | 地址里 `access_token=` 后面那段 |

在「设置推送」里勾选要用的渠道；**没勾 = 不推**，填了 key 也不会自己开。

---

## 服务端授权（人不在电脑前）

```bash
# ① 一次性准备（不联网）
python src/wb_auth_server.py --init-config   # 生成 config.json 模板
python src/wb_auth_server.py --check         # 体检渠道与 key

# ② 生成链接 → 推到手机 → 等人扫码 → 输出导入码
python src/wb_auth_server.py

# ③ 无人值守：授权后开一次性端口，Windows 端自己来取令牌
python src/wb_auth_server.py --handoff --port 8787

# ④ 只把一条已有链接转发到渠道
python src/wb_auth_server.py --push-only --url "https://..."
```

零依赖，Linux / macOS / Windows 都能跑。默认**不落盘**，只有显式 `--save-token`
才在服务器保留令牌副本——那时就别再往别处导入。

完整流程（含 Windows 端命令、systemd/cron、退出码、排错）见
[docs/server.md](docs/server.md)。

---

## 命令行

本地运行用 `python run.py <参数>`；独立运行用 `WorkBuddy签到.exe <参数>`，
参数完全一致：

| 参数 | 作用 |
|---|---|
| `--run` | 无界面签到一次（计划任务用的就是它） |
| `--check-only` | 只查状态，不领 |
| `--selftest` | 环境自检，结果写 `selftest.json` |
| `--diagnose` | 授权 + 接口连通性（不打印令牌） |
| `--sched-status` | 查看计划任务状态（写日志） |
| `--test-notify` | 发一条测试消息 |
| `--auth` | 本机浏览器授权（界面同一入口） |
| `--import-token <码或文件>` | **服务端流程**：导入服务端给的令牌 |
| `--pull-token <url> <secret>` | **服务端流程**：从服务端一次性端口领取 |
| `--json` | 输出单行 JSON（可与上面组合） |

---

## 项目结构

```
workbuddy-checkin/
├── run.py                  # 本地运行入口（源码态，唯一入口）
├── src/                    # 逻辑源码 —— 不含任何构建代码
│   ├── checkin_gui.py      #   tkinter 界面 + 无界面分流
│   ├── workbuddy_checkin.py#   签到三步、授权、CLI
│   ├── wb_oauth.py         #   设备授权、令牌落盘、续期、导入码
│   ├── wb_task.py          #   Windows 计划任务
│   ├── wb_notify.py        #   企业微信 / 钉钉推送
│   ├── wb_config.py        #   配置与路径（服务端共用）
│   ├── win_quiet.py        #   子进程不弹黑框
│   └── wb_auth_server.py   #   服务端授权脚本
├── build/                  # 构建与打包 —— 不含任何业务逻辑
│   ├── entry.py            #   独立运行入口（PyInstaller 只认它）
│   └── build_exe.py        #   打包脚本（选解释器 → 打包 → 自检）
├── tools/                  # 回归检查
│   ├── oauth_mode_check.py #   授权/续期/导入码/推送（本地桩服务器）
│   └── no_popup_check.py   #   运行时是否弹控制台黑框
├── docs/                   # 使用 / 构建 / 架构 / 排障
└── dist/                   # 发布产物（exe、截图）
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/usage.md](docs/usage.md) | 使用手册：授权、到期提醒、定时、推送、配置项、退出码 |
| [docs/server.md](docs/server.md) | 服务端授权：部署、两种交接方式、Windows 端命令、排错 |
| [docs/build.md](docs/build.md) | 构建与打包、发布前检查清单 |
| [docs/architecture.md](docs/architecture.md) | 分层、两种运行模式、协议与实现取舍 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 按现象排查 |

---

## 常见问题

**Q：点按钮闪出一个黑色命令窗口？**
窗口程序自身没有控制台，去调 PowerShell 会新建一个。本工具所有系统调用都隐藏窗口；
有疑问用 `pythonw tools/no_popup_check.py -- --sched-status` 验证。

**Q：提示「尚未授权」/「授权已失效」？**
前者是本机没有令牌，点授权即可；后者是 `refreshToken` 也过期了（约 30 天没跑或
被服务端撤销），重新授权。刚在服务器授权完又导入旧令牌也会这样——令牌只能有一份。

**Q：定时到点没签到？**
看「每日定时」那行的系统状态，或执行 `--sched-status`（结果也写进 `checkin.log`）。
逐步排查见 [docs/troubleshooting.md](docs/troubleshooting.md#6-定时不生效)。

**Q：exe 挪了目录后定时失效？**
任务里记的是绝对路径。打开程序，重新点一次「应用到系统」。

**Q：源码能跑，exe 起不来？**
多半是打包解释器没有 tkinter。用 `python build/build_exe.py`（会自动挑解释器并自检），
不要手工敲 PyInstaller 命令。

---

## 免责声明

- 本项目是**非官方的个人自动化工具**，与 WorkBuddy 官方无关。
- 令牌以明文存在本机 `auth_oauth.json`（已在 `.gitignore` 中排除），调用身份是
  CLI 而非桌面端，服务端看到的风控特征不同。
- 官方对「脚本自动签到」持否定态度，检测到可能回收超额积分甚至限制账号功能。
  是否使用请自行确认符合相关服务条款，**风险自负**。
- `config.json` 可能包含机器人 key，**不要提交、不要分享**。

---

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。
