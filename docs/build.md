# 构建与打包

本文只讲**怎么把源码变成可发布的 exe**。业务逻辑在 `src/`，
构建相关的所有东西都在 `build/`，两者不互相引用。

---

## 1. 前置条件

| 用途 | 要求 |
|---|---|
| 本地运行 | Python 3.8+，**带 tkinter** |
| 打包 | 同上 + PyInstaller（可装在隔离目录） |

本项目**零第三方运行时依赖**，但打包器本身要单独装：

```bash
# 装到仓库内的隔离目录，不污染系统环境
python -m pip install --target .build/pylibs pyinstaller
```

`.build/` 已在 `.gitignore` 中，构建产物不会进版本库。

---

## 2. 本地运行（不打包）

```bash
python run.py
```

`run.py` 只做路径引导：把 `src/` 加进 `sys.path`，然后调用
`checkin_gui.main()`。配置、日志、令牌都写在**仓库根目录**。

无界面调试：

```bash
python run.py --run --json
python run.py --selftest
python run.py --diagnose
```

> 本地运行和独立运行的数据目录是分开的（根目录 vs exe 旁边），
> 别拿根目录的 `config.json` 去推断 exe 的配置。

---

## 3. 打包

```bash
python build/build_exe.py
```

脚本做四件事：

1. **挑解释器** —— 从常见安装路径里找第一个 `import tkinter` 成功的
2. **调用 PyInstaller** —— `--onefile --windowed`，入口是 `build/entry.py`，
   源码搜索路径是 `src/`
3. **自检** —— 跑一次 `--selftest`，并校验 `frozen == true`
4. **出中文名** —— 顺手复制一份 `dist/WorkBuddy签到.exe`

产物：

```
dist/WorkBuddyCheckin.exe      # 主产物
dist/WorkBuddy签到.exe          # 直接对外发的那个名字
```

PyInstaller 先输出到 `.build/out/`，校验通过后再复制进 `dist/`——
这样在 `dist/` 里的 exe 正在运行时也不会写不进去（覆盖一个被占用的文件会
`PermissionError`）。

正常体积约 **11.4 MB**。如果只有 8 MB 左右，几乎一定是没打进去 tkinter，
脚本的自检会拦住这种产物。

### 指定解释器

```bash
python build/build_exe.py --python "C:\...\Python313\python3.exe"
```

### 等价的 PyInstaller 命令

脚本只是把这些参数固定下来；手工执行时必须保留 `--paths src`，
否则 PyInstaller 找不到 `src/` 里的模块：

```bash
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name WorkBuddyCheckin \
  --paths src \
  --distpath dist \
  --workpath .build/pyi/work \
  --specpath .build/pyi \
  --hidden-import wb_config --hidden-import wb_notify \
  --hidden-import wb_oauth --hidden-import wb_task \
  --hidden-import win_quiet --hidden-import workbuddy_checkin \
  --hidden-import checkin_gui \
  build/entry.py
```

---

## 4. 打包后验证

```bash
# 1) 冻结态自检：会写 dist/selftest.json
dist/WorkBuddy签到.exe --selftest

# 2) 无界面跑一次（建议先临时关掉推送，避免真发消息）
dist/WorkBuddy签到.exe --run --json

# 3) 确认没有控制台黑框
pythonw tools/no_popup_check.py --exe "dist/WorkBuddy签到.exe" -- --sched-status

# 4) 授权链路回归（本地桩服务器，不碰真接口）
python tools/oauth_mode_check.py
```

`selftest.json` 里重点看：

- `frozen` 必须是 `true`（否则你跑的是源码）
- `app_dir` 必须是 `dist`（配置和令牌才会跟着 exe 走）
- `token.present` / `expired`
- `schedule.exists` / `command` 是否指向当前 exe 路径

> 挪动 exe 之后，计划任务里的旧路径会失效，需要在界面里重新「应用到系统」。

---

## 5. 发布前检查清单

- [ ] `python tools/oauth_mode_check.py` 全绿
- [ ] `dist/WorkBuddy签到.exe --selftest` 退出码 0，`frozen=true`
- [ ] `--sched-status` 不弹控制台黑框
- [ ] `--run` 退出码 0（测之前把 `push_channels` 临时清空）
- [ ] `dist/config.json` 里没有真实 key（发布包不带配置）
- [ ] 不在 `dist/` 里留下 `auth_oauth.json`、`checkin.log`
- [ ] README 的截图与新界面一致

分发包只需要 **exe 一个文件**。`config.json` 由程序首次运行时生成，
不要把带 key 的配置一起发出去。

---

## 6. 踩过的坑

### 解释器没有 tkinter

PyInstaller **不会报错**，只是少收集 `_tkinter.pyd` 和 tcl/tk；产物能生成，
双击就 `ModuleNotFoundError`。`build_exe.py` 因此强制先 `import tkinter` 试一下。

### 上一版的自检结果冒充这一次

`selftest.json` 没删就跑，校验会无条件通过。脚本先 unlink 再跑，并且
额外校验 `frozen` 字段（源码态自检同样会生成这个文件）。

### 旧 exe 被占用

exe 正在运行（或计划任务刚拉起）时覆盖会 `PermissionError`。
先确认 `dist/` 里没有进程在跑；必要时换一个 `--distpath` 再复制回来。

### 无控制台程序的输出

`--windowed` 打包后 `sys.stdout` 可能是 `None`。无界面入口会把输出接到
UTF-8 的 `checkin.log`，否则中文/emoji 打印会抛异常，表现为
**签到成功但退出码非 0**，计划任务记成失败。细节见
[architecture.md](architecture.md#无控制台程序的输出)。

---

## 7. 目录约定

```
run.py            本地运行入口，不参与打包
build/entry.py    独立运行入口，只给 PyInstaller 用
src/              全部业务逻辑，不含构建代码
build/            全部构建脚本，不含业务逻辑
.build/           PyInstaller 中间产物、隔离安装的依赖（不入库）
dist/             发布产物（不入库，截图除外）
```

新增模块时：

- 业务代码放 `src/`，按现有风格 `import wb_xxx`
- 如果引入新的顶层模块，记得加进 `build_exe.py` 的 `HIDDEN_IMPORTS`
- 不要从 `src/` 反向 import `build/` 里的任何东西
