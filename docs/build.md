# 构建与打包

## 1. 版本管理

**版本号只有一个来源**：`src/wb_config.py` 的 `VERSION`

```python
VERSION = "3.0.0"
RELEASE_DATE = "2026-10-19"
```

三处会用到它，都从这里读：

| 位置 | 表现 |
|---|---|
| 界面右上角 / 启动日志 | `v3.0.0` |
| `--version`（exe 与服务端脚本） | `WorkBuddyCheckin 3.0.0 (2026-10-19)` |
| exe 文件属性（右键 → 详细信息） | 文件版本 / 产品版本 `3.0.0` |

打包时 `build/build_exe.py` 会读取 `VERSION`，生成 PyInstaller 的版本资源
（`.build/pyi/version_info.txt`）再喂给 `--version-file`。所以**改了版本号，
exe 属性里的版本就跟着变**，不需要另外维护 `.rc` 文件。

发版流程：

1. 改 `src/wb_config.py` 的 `VERSION` / `RELEASE_DATE`
2. 在 `CHANGELOG.md` 顶部加一节（`## [3.1.0] - 2026-xx-xx`）
3. `python build/build_exe.py`
4. 打包脚本末尾会打印 `打包完成：v3.1.0`，异常说明版本没进产物

版本语义按 [SemVer](https://semver.org/lang/zh-CN/)：破坏性改动进次版本号
（本项目 2.0 → 3.0 都改了运行方式与数据目录），功能进 MINOR，修 bug 进 PATCH。

## 2. 环境要求

- Windows（计划任务、`pythonw`、无控制台行为都只在 Windows 上有意义）
- **带 tkinter 的 Python 3.8+**
- PyInstaller（本机隔离安装即可）

> 打包脚本会自己挑一个能 `import tkinter` 的解释器。本机可用的是
> `C:\Users\<你>\AppData\Local\Programs\Python\Python313\python3.exe`
> （该目录没有 `python.exe`，只有 `python3.exe` / `pythonw3.exe`）。

第一次打包先装 PyInstaller 到本地隔离目录：

```bash
python -m pip install --target .build/pylibs pyinstaller
```

## 3. 打包

```bash
python build/build_exe.py                    # 自动挑解释器
python build/build_exe.py --python <解释器>   # 指定解释器
```

产物**只有一个**：

```
dist/WorkBuddyCheckin.exe      # 单文件，双击即用，目标机器不用装 Python
```

不再生成中文名副本。中文名在命令行、计划任务、日志、编码转换里都会被折腾，
而 exe 名字只影响用户看到什么——所以统一用英文名。如果 dist 里还留着
`WorkBuddy签到.exe` 这类旧产物，打包脚本会顺手删掉，避免两个文件混在一起。

打包过程：

```
src/            --paths 挂进去（业务代码都在这里）
build/entry.py  PyInstaller 的入口（冻结态启动 + 设数据目录）
--onefile       单文件
--windowed      不带控制台（配合 win_quiet 保证不弹黑框）
--version-file  版本资源
先出到 .build/out，校验通过再 copy 到 dist/
```

先出到 `.build/out` 再复制，是为了避开 dist 里的 exe 正在运行时的
`PermissionError`。

---

## 4. 打服务端包

服务端版是脚本，不是 exe，但**也不能把整个 `src/` 拷过去**——里面混着
`checkin_gui.py`（tkinter 界面）、`wb_task.py`（Windows 计划任务）、
`win_quiet.py`（弹窗抑制），服务器上一概用不到。

```bash
python build/make_server_bundle.py            # 产出 dist/server/
python build/make_server_bundle.py --out /tmp/wb   # 换输出目录
```

它从 `wb_server.py` 出发做 **import 闭包**（AST 静态解析，只看模块级导入），
自动决定要带哪几个文件：

```
wb_server.py          入口
workbuddy_checkin.py  签到流程
wb_oauth.py           设备授权 / 续期
wb_notify.py          推送
wb_config.py          配置与路径
config.example.json   配置模板（复用 wb_config.DEFAULT_CONFIG，键名不会写歪）
README.txt            三步部署说明
_bundle.json          清单，下次打包据此清理旧文件
```

几条设计约束：

- **闭包里出现 `checkin_gui` / `wb_task` / `win_quiet` 就直接失败**，
  提示把那处导入改成函数内惰性导入。这条拦住过一次真实回归：
  `workbuddy_checkin.diagnose()` 里 `import wb_task` 查计划任务，
  虽然被 try 包着不会崩，但静态闭包会把它算进依赖。
- 只清理**上一次由自己放进去的 `.py`**（读 `_bundle.json`），
  不碰 `config.json` / `auth_oauth.json` / `checkin.log`——那可能是线上数据。
- 打完自动在产物目录跑一次 `wb_server.py --version`，闭包缺文件会立刻暴露。
- 产物里的 `.py` 统一写成 LF；`__pycache__` 用 `-B` 避免生成。

> 服务端目录是**平铺**的，`wb_config.is_repo_layout()` 靠 `run.py` / `.git` /
> `build/` 判断是不是在开发仓库里：不是就认为数据目录 = 脚本目录，
> 而不是脚本的父目录。`--version` 会显示「脚本运行」以区分。

---

## 5. 打包后的自动校验

`build_exe.py` 打完会立刻运行一次 `dist/WorkBuddyCheckin.exe --selftest`，
只有同时满足下面条件才算成功：

- 退出码为 `0`
- 重新生成了 `selftest.json`（打包前会先删旧的，防止拿旧结果蒙过去）
- `selftest.json` 里 `frozen` 为 `true`

校验通过会打印版本、授权状态、推送渠道。**最重要的一条是 `frozen=true`**：
缺 tkinter 的坏包通常在更早的导入阶段就崩了，但偶尔会有"能跑但不是冻结态"
的诡异产物。

`selftest.json` 写在**数据目录**（exe 是 `%LOCALAPPDATA%\WorkBuddyCheckin`），
里面重点看：

```json
{
  "version": "3.0.0",
  "frozen": true,
  "data_dir": "C:\\Users\\<你>\\AppData\\Local\\WorkBuddyCheckin",
  "data_origin": "用户缓存目录",
  "install_dir": "D:\\dist",
  "token": {"present": true, "days_left": 27},
  "schedule": {"exists": true, "command": "D:/dist/WorkBuddyCheckin.exe"}
}
```

## 6. 发布前检查清单

- [ ] `python tools/oauth_mode_check.py` 全绿（77 项）
- [ ] `dist/WorkBuddyCheckin.exe --version` 版本号正确
- [ ] `--selftest` 退出码 0，`frozen=true`，`data_origin=用户缓存目录`
- [ ] 把 exe 拷到**另一个目录**再运行，确认仍能读到配置（数据目录不在 exe 旁）
- [ ] `--sched-status` 不弹控制台黑框
- [ ] `--run` 退出码 0（**测之前先清空 `push_channels`**，别真发消息）
- [ ] 未授权时打开界面，除「立即授权」外按钮全灰
- [ ] `python build/make_server_bundle.py` 成功，`dist/server/` 里**没有**
      `checkin_gui.py` / `wb_task.py` / `win_quiet.py`
- [ ] 服务端包拷到仓库外的空目录后 `python3 wb_server.py --version` 正常
      （输出「脚本运行」，数据目录就是该目录）
- [ ] 没有把 `config.json` / `auth_oauth.json` / `*.log` 打进发布包
- [ ] README 截图与当前界面一致

## 7. 踩过的坑

**用不带 tkinter 的解释器打包**：PyInstaller 不报错，只是悄悄少收集
`_tkinter.pyd` 和 tcl/tk 运行时，产物一启动就 `ModuleNotFoundError: tkinter`。
唯一线索是体积小掉约 3 MB（正常约 11 MB）。所以脚本强制挑解释器 + 打完自检。

**拿旧 `selftest.json` 当通过依据**：文件在 ≠ 这次跑过。打包前必须先删。

**dist 里的 exe 正在运行**：往 dist 写会 `PermissionError`。先出到 `.build/out`
再复制。

**中文名 exe**：曾经打两个名字，结果计划任务、文档、日志里各写各的，
还出现过"改了新 exe、用户双击的是旧中文名"的情况。v3.0 起只出英文名。

**`--windowed` 下 stdout 可能是 None**：日志重定向和 `--json` 输出都要先判断，
否则"签到成功但退出码非 0"，计划任务天天标红。
