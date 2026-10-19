# 服务端版（cron 定时脚本）

`wb_server.pyz`：无界面，一条命令跑完「授权（必要时）→ 签到 → 推送」，
交给 cron 定时调用即可。缺授权时它自己开一个**无头浏览器**取微信二维码链接
推给你扫；有授权就完全不启动浏览器。

和 Windows 版是**两套部署，选一个用**。两边同时定时会因为 `refreshToken`
轮换而互相踢掉（服务端返回 `12153`）。

---

## 1. 部署

服务端只发**一个文件**：`dist/server/wb_server.pyz`（约 38 KB）。

```bash
python3 build/make_server.py       # 本机构建，产出 dist/server/wb_server.pyz
scp dist/server/wb_server.pyz server:/opt/wbcheckin/
```

包里装了哪些模块不需要你操心——它按 `wb_server` 的 **import 闭包**自动收集
（源码在 `src/server/` + `src/common/`），只装服务端真正用到的：

```
wb_server            入口
workbuddy_checkin    签到流程（不含界面）
wb_oauth             设备授权 / 续期
wb_qrlogin           无头浏览器取二维码（只在授权时用，playwright 惰性导入）
wb_notify            推送
wb_config            配置与路径
```

`src/desktop/` 里的界面（tkinter）、Windows 计划任务、弹窗抑制**不会**进包；
闭包里一旦出现它们，构建会直接失败并告诉你是哪处导入。
`playwright` 也不在包里——它是运行环境自己装的，没装只影响授权那一步。

服务器上：

```bash
cd /opt/wbcheckin

# 1) 生成配置（写在 pyz 同目录）
python3 wb_server.pyz --init-config

# 2) 填 key（企业微信 key 或钉钉 access_token），勾选渠道
vi config.json

# 3) 体检：不联网，只看配置、授权状态、二维码能力
python3 wb_server.pyz --check

# 4) 先手工跑一次：缺授权时会推一条二维码链接，扫码即可，等 3 分钟
python3 wb_server.pyz
```

首次授权要装一次 playwright（**只有授权用得到，日常签到不碰**）：

```bash
# Linux 服务器（自带 chromium + 系统依赖，推荐）
pip install playwright
python -m playwright install --with-deps chromium

# Windows / macOS：装了 Chrome 或 Edge 的话只装库即可，不用下 chromium
pip install playwright
```

**只 `pip install playwright` 不算装好**：库在，但浏览器没下载，启动时会报
`Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome`。
脚本会按 chrome → msedge → 系统 chromium/chrome 路径 → playwright 自带
chromium 的顺序找，**有一处能起就行**：

```bash
export WB_BROWSER_PATH=/usr/bin/chromium        # 复用系统已有的浏览器
export PLAYWRIGHT_BROWSERS_PATH=/home/xxx/.cache/ms-playwright   # 浏览器在别的用户下
```

都没找到时脚本打一份「4 种来源分别为什么失败 + 该敲哪条命令」的报告，
然后**立即退出（退出码 2）**，不会去空等授权。

> cron 常以 root 运行，而 playwright 是装在你自己的用户下的 ——
> `~/.cache/ms-playwright` 按用户区分，这就是「手工跑正常、cron 里起不来」
> 的典型原因。用同一个用户跑，或指 `PLAYWRIGHT_BROWSERS_PATH`。

要求：Python 3.8+，**日常签到零第三方依赖**，Linux / Windows 都能跑
（不 import tkinter，也不碰 Windows 计划任务）。也可以直接 `./wb_server.pyz`
执行（文件带 shebang）。

**配置跟着脚本走**：`config.json`、`checkin.log`、`auth_oauth.json` 都写在
`wb_server.pyz` 所在目录。`wb_config` 认得出自己在 zip 里，会退到
`sys.argv[0]` 的目录，不会往父目录或 zip 内部写。
`--version` 显示「脚本运行」，与源码运行区分。

---

## 2. 每次运行做什么

```
python3 wb_server.pyz
```

```
① 检查授权
   有效      → 直接进入 ②（不启动浏览器）
   未授权 / 损坏 / 会话失效 → 进入授权流程
   网络不通  → 退出码 3（重试授权没有意义）

   授权流程（最多 3 轮，指数退避）
     轮 1：申请会话 → 起无头浏览器取二维码链接 → 推给用户 → 等 180 秒
     轮 2：（隔 60 秒）新的会话 → 二维码 → 推 → 等 180 秒
     轮 3：（隔 120 秒）同上
   拿到令牌立刻关掉浏览器；最坏耗时约 12 分钟

   没装 playwright / 浏览器起不来 → 打一行原因 + 安装命令，
                                    **立即退出码 2，不推链接、不等待、不重试**
                                   （没有二维码就扫不了码，等下去也是白等）

② 执行签到：查 → 领 → 复核

③ 推送结果到同一批渠道（含授权到期提醒）
```

推给你的是**二维码图片链接**，不是授权页链接：

```
【WorkBuddy 签到】服务端需要重新授权
① 点开二维码图片：
   https://open.weixin.qq.com/connect/qrcode/0813EhAu2z7Pkl2H
② 手机上长按这张图 →「识别图中二维码」
③ 点「允许」，服务端会自动拿到授权、继续签到。
```

微信这张二维码本身就是公网图片，所以**企业微信和钉钉都只发文字**就行，
不用图床、不用在服务器上开端口，也不用为"钉钉不能发图片"做降级。

为什么不能直接推授权页链接：那个页面登录成功后由**页面自己**回调
`/console/auth/login` 把 `state` 标记完成，登录态落在打开它的浏览器里。
把链接转到手机上，服务端轮询只会一直返回 `11217`。所以服务端自己开浏览器，
让登录发生在我们控制的浏览器里——实测扫码后 100 秒内拿到 `code=0`。

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
| `auth_qr` | `true` | `--no-qr` | 是否用二维码授权（关掉只会推授权页链接，仅排障） |

---

## 3. cron

```cron
# 每天 09:30，输出追加到 checkin.log
30 9 * * * cd /opt/wbcheckin && python3 wb_server.pyz >> checkin.log 2>&1
```

几点建议：

- **一定 `cd` 到脚本目录**，或写全路径 `/opt/wbcheckin/wb_server.pyz`；
- 用 `>> checkin.log 2>&1` 留下输出，出问题时不用猜；
- 退出码非 0 说明这次没签上，可以接报警（如 `|| echo fail | mail ...`）；
- 想错开高峰，让 cron 在 09:30 触发、脚本内部不需要再做随机延迟。

验证 cron 环境（path、权限、时区）：

```bash
env -i HOME=/root PATH=/usr/local/bin:/usr/bin:/bin \
  sh -c 'cd /opt/wbcheckin && python3 wb_server.pyz --check'
```

---

## 4. 常用命令

| 命令 | 作用 |
|---|---|
| `python3 wb_server.pyz` | 日常：该授权就授权，然后签到 |
| `--check` | 体检配置与授权状态（不联网、不签到） |
| `--init-config [PATH]` | 生成配置模板（`--force` 才覆盖） |
| `--auth-only` | 只授权，不签到（首次部署时先跑这个） |
| `--checkin-only` | 只签到，不检查授权 |
| `--channel wecom` | 强制只用一个渠道 |
| `--no-qr` | 不生成二维码，只推授权页链接（仅排障） |
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
- 授权二维码等于一次登录机会，约 5 分钟内有效；不要把控制台输出整段贴到群里。
- 服务端授权期间会起一个无头 Chrome/Chromium（约 200 MB 内存），
  拿到令牌立刻关掉；**有授权时不启动**。想彻底避免可用 `--no-qr`。
- 建议单独建系统用户跑 cron，目录权限 `700`。

---

## 6. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `✗ 没有可用的推送渠道` | `push_channels` 勾了但 key 没填对。跑 `--check` 看是哪一种 |
| `二维码授权：不可用` | 没装 playwright 或没装浏览器。`pip install playwright && python -m playwright install --with-deps chromium`（或 `WB_BROWSER_PATH=/usr/bin/chromium`）。**这时脚本会直接退出（退出码 2），不会等待** |
| `✗ 取二维码失败 —— 起不了浏览器` | 会在下面列出 4 种来源各自的原因、浏览器目录、当前用户和修复命令。照第 1 条装 chromium 即可；`cron 起不来、手工正常` 见上面 root/用户那段 |
| 二维码链接点开是空白 | 过期了（约 5 分钟）。重跑脚本会重新申请 |
| 3 轮都超时 | 人没在 3 分钟内扫码。加大 `--wait 300`，或先 `--auth-only` 单独授权 |
| `12153 Offline user session not found` | 令牌被别处刷新过（两台机器都在跑）。只留一处 |
| `OAUTH_NETWORK` | 服务器出不了网 / 被墙。检查代理与出站白名单 |
| cron 里不跑，手工跑正常 | cron 环境变量少、没 `cd`、python 路径不同。用上面的 `env -i` 验证 |
| 想换机器 | 整个目录拷走即可；旧机器上的 cron 记得停 |

日志在脚本目录的 `checkin.log`，每次运行一行 JSON（结果、渠道、错误详情）。
