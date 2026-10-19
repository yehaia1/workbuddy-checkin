# 服务端版（cron 定时脚本）

`src/wb_server.py`：无界面、无浏览器，一条命令跑完「授权（必要时）→ 签到 → 推送」，
交给 cron 定时调用即可。

和 Windows 版是**两套部署，选一个用**。两边同时定时会因为 `refreshToken`
轮换而互相踢掉（服务端返回 `12153`）。

---

## 1. 部署

服务器上要的只是几个 `.py` 和一个 `config.json`，**全部在 `src/` 里**：

```
wb_server.py          入口
workbuddy_checkin.py  签到流程
wb_oauth.py           设备授权 / 续期
wb_notify.py          推送
wb_config.py          配置与路径
config.json           配置（自己生成）
```

```bash
# 1) 拷目录（也可以只拷上面这几个文件）
scp -r src/ server:/opt/wbcheckin

# 2) 生成配置模板
cd /opt/wbcheckin
python3 wb_server.py --init-config

# 3) 填 key（企业微信 key 或钉钉 access_token），勾选渠道
vi config.json

# 4) 体检：不联网，只看配置和授权状态
python3 wb_server.py --check
```

要求：Python 3.8+，**零第三方依赖**，Linux / Windows 都能跑
（不 import tkinter，也不碰 Windows 计划任务）。

**配置跟着脚本走**：默认读脚本同目录的 `config.json`，日志
`checkin.log`、令牌 `auth_oauth.json` 也写在同一层。整个目录拷走就能换机器。

---

## 2. 每次运行做什么

```
python3 wb_server.py
```

```
① 检查授权
   有效      → 直接进入 ②
   未授权 / 损坏 / 会话失效 → 进入授权流程
   网络不通  → 退出码 3（重试授权没有意义）

   授权流程（最多 3 轮，指数退避）
     轮 1：申请会话 → 推链接到渠道 → 等 180 秒
     轮 2：（隔 60 秒）重新申请 → 推链接 → 等 180 秒
     轮 3：（隔 120 秒）重新申请 → 推链接 → 等 180 秒
   最坏耗时约 12 分钟

② 执行签到：查 → 领 → 复核

③ 推送结果到同一批渠道（含授权到期提醒）
```

关键点：

- **每轮都重新读 `config.json`**。等待的 3 分钟里，你完全可以回去把机器人
  key 补上、把渠道勾上——下一轮就推得出去了。
- 每轮都重新申请授权会话（`state` 一次性，用完就换新的）。
- 3 轮都没成：推一条失败通知，退出码 `2`，本次跳过签到。

时间参数（配置文件，也可命令行覆盖）：

| 键 | 默认 | 命令行 | 说明 |
|---|---|---|---|
| `auth_wait_seconds` | `180` | `--wait` | 每轮等多久 |
| `auth_attempts` | `3` | `--attempts` | 最多几轮 |
| `auth_backoff_seconds` | `60` | `--backoff` | 退避基数，按 1x/2x 增长 |

---

## 3. cron

```cron
# 每天 09:30，输出追加到 checkin.log
30 9 * * * cd /opt/wbcheckin && python3 wb_server.py >> checkin.log 2>&1
```

几点建议：

- **一定 `cd` 到脚本目录**，或写全路径 `/opt/wbcheckin/wb_server.py`；
- 用 `>> checkin.log 2>&1` 留下输出，出问题时不用猜；
- 退出码非 0 说明这次没签上，可以接报警（如 `|| echo fail | mail ...`）；
- 想错开高峰，让 cron 在 09:30 触发、脚本内部不需要再做随机延迟。

验证 cron 环境（path、权限、时区）：

```bash
env -i HOME=/root PATH=/usr/local/bin:/usr/bin:/bin \
  sh -c 'cd /opt/wbcheckin && python3 wb_server.py --check'
```

---

## 4. 常用命令

| 命令 | 作用 |
|---|---|
| `python3 wb_server.py` | 日常：该授权就授权，然后签到 |
| `--check` | 体检配置与授权状态（不联网、不签到） |
| `--init-config [PATH]` | 生成配置模板（`--force` 才覆盖） |
| `--auth-only` | 只授权，不签到（首次部署时先跑这个） |
| `--checkin-only` | 只签到，不检查授权 |
| `--channel wecom` | 强制只用一个渠道 |
| `--push-only --url <链接>` | 只把一条链接转发到渠道 |
| `--version` | 版本 |
| `--json` | 结果输出单行 JSON |

退出码：

| 码 | 含义 |
|---|---|
| `0` | 成功（含"今天已签到"） |
| `1` | 推送失败（仅 `--push-only`） |
| `2` | 授权没完成 / 配置不可用 |
| `3` | 网络或接口异常 |
| `130` | 被 Ctrl+C 中断 |

---

## 5. 安全性

- `auth_oauth.json` 是**明文令牌**，等同于登录状态：权限收紧（脚本会尝试
  `chmod 600`），不要进 git、不要打进镜像层。
- `config.json` 里有机器人 key：同样不要提交。本仓库的 `.gitignore` 已经
  挡住 `config.json`、`auth_oauth.json`、`*.log`。
- 授权链接里带 `state`，3 分钟内有效；不要把控制台输出整段贴到群里。
- 建议单独建系统用户跑 cron，目录权限 `700`。

---

## 6. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `✗ 没有可用的推送渠道` | `push_channels` 勾了但 key 没填对。跑 `--check` 看是哪一种 |
| 推了链接，手机点开是空白 | 链接过期（3 分钟）。重跑脚本，会重新申请 |
| 3 轮都超时 | 人没在 3 分钟内点开。加大 `--wait 300`，或先 `--auth-only` 单独授权 |
| `12153 Offline user session not found` | 令牌被别处刷新过（两台机器都在跑）。只留一处 |
| `OAUTH_NETWORK` | 服务器出不了网 / 被墙。检查代理与出站白名单 |
| cron 里不跑，手工跑正常 | cron 环境变量少、没 `cd`、python 路径不同。用上面的 `env -i` 验证 |
| 想换机器 | 整个目录拷走即可；旧机器上的 cron 记得停 |

日志在脚本目录的 `checkin.log`，每次运行一行 JSON（结果、渠道、错误详情）。
