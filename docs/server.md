# 服务端授权指南

面向"签到跑在一台没人守的 Windows 上"的场景。本地版界面只有
「立即授权」（本机浏览器），**远程授权、跨机器交接令牌全部由这个脚本 +
命令行完成**，原因见 [architecture.md](architecture.md#6-授权为什么是设备授权)。

---

## 0. 它解决什么问题

Windows 那台机器可能没人守着、没有浏览器，或者你根本不想去点。于是：

```
服务器                          手机                       Windows
  │  ① 申请授权会话               │                            │
  │  ② 把链接推到群 ─────────────▶ │ 点开 → 登录 → 确认          │
  │  ③ 轮询到令牌                 │                            │
  │  ④ 交出令牌 ──────────────────┼──────────────────────────▶ │ 导入后独占续期
```

关键点只有一句：**令牌只能有一份**。`refreshToken` 每次刷新都会轮换，
两台机器各存一份，先刷新的那台会让另一台立刻失效（服务端返回 `12153`）。
所以脚本默认**不落盘**，拿到令牌就交出去，之后所有续期都在 Windows 上。

---

## 1. 部署前提

| 项 | 要求 |
|---|---|
| Python | 3.7+，**零第三方依赖**（不需要 pip install） |
| 能访问 | `copilot.tencent.com`（授权接口）、`qyapi.weixin.qq.com` / `oapi.dingtalk.com`（推送） |
| 不需要 | tkinter、GUI、Windows、`wb_task.py` |

只需要把这 5 个文件放到服务器的同一个目录（`src/` 下这几个即可）：

```
wb_auth_server.py      # 本脚本
workbuddy_checkin.py   # 提供授权文案
wb_oauth.py            # 设备授权协议
wb_notify.py           # 推送
wb_config.py           # 配置与路径
```

```bash
# 服务器上只需要这么放（路径随意，保持同级）
mkdir -p /opt/wbcheckin/src && cp wb_auth_server.py workbuddy_checkin.py \
     wb_oauth.py wb_notify.py wb_config.py /opt/wbcheckin/src/
```

下面所有命令都在**仓库根目录**执行；服务器上没有仓库结构时，把
`python src/wb_auth_server.py` 换成 `python /opt/wbcheckin/src/wb_auth_server.py` 即可。

---

## 2. 首次准备（三步，都不联网）

```bash
# ① 生成配置模板（默认写到数据目录；也可以指定路径）
python src/wb_auth_server.py --init-config
python src/wb_auth_server.py --init-config /etc/wbcheckin/config.json

# ② 编辑配置：填 key、勾选渠道
#    wecom_key      = 企业微信机器人 webhook 里 key= 后面那段
#    dingtalk_key   = 钉钉机器人链接里 access_token= 后面那段
#    push_channels  = ["wecom"] 或 ["dingtalk"] 或两个都写

# ③ 体检（不联网、不创建授权会话）
python src/wb_auth_server.py --check
```

`--check` 的输出长这样：

```
配置文件：/etc/wbcheckin/config.json
数据目录：/opt/wbcheckin

  企业微信：可用
  钉钉：未启用

✅ 可用渠道：企业微信
```

每个渠道只有四种状态：`可用` / `已勾选，但 key 缺失或不合法` /
`key 已填，但没勾选（不会推送）` / `未启用`。体检**不会打印 key 本身**。
没有可用渠道时退出码 `2`，适合放进部署脚本里当门禁。

> `--init-config` 不会覆盖已有文件；确实要重来加 `--force`
> （**会丢掉现有 key**）。

---

## 3. 两种交接方式

### 方式 A：导入码（手动一条命令）

```bash
python src/wb_auth_server.py
```

脚本会：

1. 申请授权会话，打印链接（同时推送到已勾选渠道）
2. 等人点开链接登录（默认最多 10 分钟，`--timeout` 可改）
3. 成功后打印一段 `WBAT1.…` 导入码

把这段码交给 Windows 端执行（一条命令，不需要界面）：

```powershell
WorkBuddy签到.exe --import-token "WBAT1.……"
python run.py    --import-token "WBAT1.……"     # 源码运行
```

**导入码等同于令牌本身**（只是 base64，不是加密），别贴群、别进工单。

### 方式 B：一次性端口自动取件（无人值守）

```bash
python src/wb_auth_server.py --handoff --port 8787
```

授权成功后脚本打印：

```
一次性领取地址已就绪：
  http://10.0.0.12:8787/token?k=<随机 secret>

在 Windows 端执行（exe 或源码任选其一）：
  WorkBuddy签到.exe --pull-token "http://10.0.0.12:8787/token" "<secret>"
```

Windows 端取走后，端口 2 秒内自动关闭，服务器不留任何令牌。
secret 是一次性的，重复领取返回 `410`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8787 | 被占用时自动换一个，以打印出来的为准 |
| `--bind` | 0.0.0.0 | 建议改成内网地址，减少暴露面 |
| `--handoff-timeout` | 600 | 等 Windows 来取件的秒数，超时退出码 `4` |

### 方式 C：只转发链接

本机（任何机器）已经生成了一条授权链接，想借服务器的推送渠道发出去：

```bash
python src/wb_auth_server.py --push-only --url "https://copilot.tencent.com/login?..."
```

不会创建授权会话，只发消息。

---

## 4. Windows 端怎么接

| 命令 | 用途 |
|---|---|
| `--import-token "<WBAT1.…>"` | 粘贴导入码 |
| `--import-token path\to\auth_oauth.json` | 导入一份令牌文件（`--save-token` 导出的那份） |
| `--pull-token <url> <secret>` | 从 `--handoff` 端口领取 |

导入后令牌落在 Windows 的数据目录（`exe` 旁边，或源码态的仓库根目录），
按 `docs/usage.md` 配一次定时即可。**界面上的「账号授权」只有本机浏览器一条路**，
导入只能走命令行 —— 这是刻意的：远程授权只属于服务端流程，界面保持最简。

> 导入后**不要**在服务器再留一份：两份 refreshToken 会互相踢掉。

---

## 5. 什么时候需要重跑

| 情况 | 是否要重跑脚本 |
|---|---|
| 只是日常签到 | 不用。令牌到期前 5 天会自动续期 |
| 收到「授权还剩 N 天」推送 | 要。说明自动续期没成功（`refreshToken` 快过期了） |
| 签到一直报 `OAUTH_SESSION_DEAD` / `12153` | 要 |
| 换了账号 | 要 |
| exe 换了机器 | 不用重跑授权，但令牌文件要手动带过去（或在那边走本机授权） |

到期提醒的阈值是配置项 `expiry_warn_days`（默认 2），它由 **Windows 端**在
每次签到后检查并推送；服务器上的脚本不参与这件事。

---

## 6. 部署形态

授权是**一次性交互动作**（等人点链接），不适合做成常驻服务。
直接在前台跑（ssh / screen / tmux 均可）：

```bash
# 交互式：手动取导入码
ssh you@server 'cd /opt/wbcheckin && python3 src/wb_auth_server.py --timeout 900'

# 无人值守：跑完等取件，取走即退出
ssh you@server 'cd /opt/wbcheckin && python3 src/wb_auth_server.py --handoff --bind 10.0.0.12'
```

想定期"体检 + 到期前提醒运维"，加一条 cron 就够（脚本本身不会自动续期）：

```cron
# 每周一 09:00 检查配置是否还有效
0 9 * * 1 cd /opt/wbcheckin && python3 src/wb_auth_server.py --check \
          || echo "wbcheckin 推送配置失效" | mail -s "wbcheckin" ops@example.com
```

---

## 7. 退出码

| 码 | 含义 | 怎么办 |
|---|---|---|
| `0` | 成功（导入码已打印 / 令牌已被取走） | — |
| `1` | `--push-only` 所有渠道都失败 | 查 key 与网络后重跑 `--check` |
| `2` | 配置/参数问题：没有可用渠道、`--init-config` 拒绝覆盖 | 按提示修配置 |
| `3` | 申请授权失败、授权响应异常（网络或协议） | 先确认能访问 `copilot.tencent.com` |
| `4` | `--handoff` 超时没人来取令牌 | 检查 Windows 能否访问该端口 |
| `130` | 被 Ctrl+C 中断 | 重新跑一次；令牌未交出 |

---

## 8. 安全注意

- **导入码 = 令牌**。任何能拿到它的人都等于拿到了你的登录态。
- 服务器上**不要**备份 `auth_oauth.json`（除非你明确用 `--save-token` 并且
  接受"这份令牌归服务器独占"）。
- `config.json` 里有机器人 key，权限收到 `600`：
  ```bash
  chmod 600 /etc/wbcheckin/config.json
  ```
- `--handoff` 的 secret 只出现在控制台，别贴到聊天窗口；端口用完即关，
  但**取件期间**它监听在 `--bind` 上，建议绑内网地址并配防火墙白名单。
- 群机器人 key 泄露等于任何人都能往群里发消息，定期轮换。

---

## 9. 排错

| 现象 | 大概率原因 | 处理 |
|---|---|---|
| `--check` 报"没有可用的推送渠道" | key 没填 / 填了整条链接抽取失败 / 没勾选 | 按体检提示改，只填 key |
| 脚本说"推送到…"但群里没消息 | 服务器出不了外网、key 失效、钉钉关键词不匹配 | 换 `--push-only --url <任意链接>` 单独试推送 |
| 链接点开报错 / 空白 | 链接已过期（默认 10 分钟） | 重跑脚本，或把 `--timeout` 调大 |
| `--handoff` 一直等不到取件 | 端口不通 / Windows 端命令抄错 | 在 Windows 上 `curl http://<ip>:8787/status` |
| 导入码报"无法解析" | 复制不全（换行、被截断） | 重新整段复制，或改用 `--save-token` 导文件 |
| 导入后签到仍报授权失效 | 服务器/别处还留着一份令牌在续期 | 删掉多余那份，只保留 Windows 这份 |
| 收到"授权还剩 2 天"推送 | 自动续期失败（`refreshToken` 快到期） | 重跑本脚本再导入一次 |

更多界面侧的问题见 [troubleshooting.md](troubleshooting.md)。
