# -*- coding: utf-8 -*-
"""Windows 计划任务管理（WorkBuddy 签到定时）。

为什么不用 schtasks.exe：
  * 命令行转义麻烦，中文路径极易踩编码坑
  * 直接设置「随机抖动」（RandomDelay）几乎没法拼
  * 一个任务挂多个触发器需要另外写 XML

改用 PowerShell 的 ScheduledTasks 模块（Win8+ 系统自带）：
  * 通过 -EncodedCommand 传脚本（Base64/UTF-16LE），彻底绕开引号与编码问题
  * 脚本把结果以 JSON 输出，Python 侧解析，失败也能拿到可读的英文/中文原因

权限：以「当前用户 + 仅登录时运行（InteractiveToken）+ 普通权限(Limited)」
注册，**不需要管理员权限，也不会弹 UAC**。代价是任务的触发条件是"用户已登录"，
本场景正好合适。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree

from win_quiet import quiet_kwargs

TASK_NAME = "WorkBuddyCheckin"

# 计划任务执行 exe 时传的参数（见 checkin_gui.main 的无界面分流）
RUN_ARG = "--run"


# ---------------- PowerShell 调用 ----------------

def _ps_quote(text: Any) -> str:
    """PowerShell 单引号字符串：内部单引号翻倍转义。"""
    return "'" + str(text).replace("'", "''") + "'"


def powershell_path() -> str:
    """定位 powershell.exe（Windows 自带，找不到时退回 PATH 查找）。"""
    for cand in (
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "WindowsPowerShell", "v1.0",
                     "powershell.exe"),
        "powershell.exe",
    ):
        if os.path.isfile(cand) or os.sep not in cand:
            return cand
    return "powershell.exe"


def run_ps(script: str, timeout: int = 90) -> Tuple[bool, Dict[str, Any], str]:
    """执行 PowerShell 脚本片段，返回 (是否成功, 解析后的 JSON, 原始信息)。

    脚本约定：把结果对象 ConvertTo-Json 输出；本函数负责抽 JSON 行。
    """
    body = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
            + script)
    try:
        encoded = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
    except Exception as e:                       # pragma: no cover
        return False, {}, "编码脚本失败：{}".format(e)

    cmd = [powershell_path(), "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    try:
        # quiet_kwargs()：无控制台环境下别给 powershell 新开一个黑框窗口
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              **quiet_kwargs())
    except FileNotFoundError:
        return False, {}, "找不到 powershell.exe，无法管理计划任务"
    except subprocess.TimeoutExpired:
        return False, {}, "PowerShell 执行超时（{} 秒）".format(timeout)
    except Exception as e:                       # pragma: no cover
        return False, {}, "调用 PowerShell 失败：{}".format(e)

    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()

    data = _extract_json(out)
    if data is None:
        detail = err or out or "无输出"
        return False, {}, "无法解析 PowerShell 返回：{}".format(detail[:300])
    if data.get("ok"):
        return True, data, ""
    return False, data, str(data.get("error") or "未知错误")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从输出里找出第一段合法 JSON 对象（跳过警告、空行等噪声）。"""
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    # 输出可能是多行美化 JSON，整体再试一次
    try:
        obj = json.loads(text[text.index("{"):text.rindex("}") + 1])
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return None


# ---------------- 任务查询 ----------------

_QUERY_TEMPLATE = """
$ErrorActionPreference = 'Stop'
try {{
  $t = Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue
  if ($null -eq $t) {{
    $r = [pscustomobject]@{{ ok = $true; exists = $false; task = {name} }}
  }} else {{
    $i = Get-ScheduledTaskInfo -TaskName {name}
    $next = ''; if ($i.NextRunTime) {{ $next = $i.NextRunTime.ToString('yyyy-MM-ddTHH:mm:ss') }}
    $last = ''; if ($i.LastRunTime) {{ $last = $i.LastRunTime.ToString('yyyy-MM-ddTHH:mm:ss') }}
    $cmd = ''; $argStr = ''
    if ($t.Actions.Count -gt 0) {{
      $cmd = [string]$t.Actions[0].Execute
      $argStr = [string]$t.Actions[0].Arguments
    }}
    $times = @()
    foreach ($tr in $t.Triggers) {{
      if ($tr.StartBoundary) {{
        try {{ $times += ([datetime]$tr.StartBoundary).ToString('HH:mm') }} catch {{}}
      }}
    }}
    $r = [pscustomobject]@{{
      ok = $true; exists = $true; task = {name}
      state = [string]$t.State
      next_run = $next; last_run = $last
      last_result = [int64]$i.LastTaskResult
      command = $cmd; arguments = $argStr
      times = $times
    }}
  }}
}} catch {{
  $r = [pscustomobject]@{{ ok = $false; error = $_.Exception.Message }}
}}
$r | ConvertTo-Json -Compress -Depth 4
"""


def query(task_name: str = TASK_NAME) -> Dict[str, Any]:
    """查询任务。返回字段：exists / state / next_run / last_run / last_result
    / command / arguments / times；查询本身失败时带 error。"""
    ok, data, msg = run_ps(_QUERY_TEMPLATE.format(name=_ps_quote(task_name)),
                           timeout=45)
    if not ok:
        return {"exists": False, "error": msg}
    data.pop("ok", None)
    return data


def exists(task_name: str = TASK_NAME) -> bool:
    return bool(query(task_name).get("exists"))


# ---------------- 任务查询（快速路径） ----------------

# 系统把每个计划任务落成一份 XML 定义文件，人人可读。直接读它只要约
# 0.1 毫秒，而走 PowerShell 要重新加载 ScheduledTasks 模块——本机实测
# 单次查询 5.0 秒。启动自检、应用定时前的遗留清理都只需要"任务在不在、
# 指向哪个程序、几点跑"，用这份文件完全够，于是把它们全部切到快速路径。
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def task_xml_path(task_name: str = TASK_NAME) -> Path:
    """计划任务定义文件的路径。"""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return Path(root) / "System32" / "Tasks" / task_name


def _iso_to_local_hm(raw: Any) -> str:
    """'2026-10-11T01:00:00Z' → 本地 '09:00'（UTC 标记会被换算）。

    任务 XML 里的 StartBoundary 一律是 UTC；不带 Z 的按本地时间处理，
    解析不了返回空串，由调用方忽略该触发器。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "." in text:                      # 容错：个别系统会带小数秒
        text = text.split(".", 1)[0] + text[text.find("Z"):]
    try:
        if text.endswith("Z"):
            dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).astimezone()
        else:
            dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""
    return dt.strftime("%H:%M")


def _iso_duration_minutes(raw: Any) -> int:
    """'PT15M' → 15；'PT1H30M' → 90。解析不了返回 0。"""
    text = str(raw or "").strip().upper()
    if not text.startswith("PT"):
        return 0
    total = 0
    num = ""
    for ch in text[2:]:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in ("H", "M", "S"):
            try:
                val = float(num or 0)
            except ValueError:
                val = 0.0
            if ch == "H":
                total += int(val * 60)
            elif ch == "M":
                total += int(val)
            num = ""
    return total


def query_fast(task_name: str = TASK_NAME) -> Dict[str, Any]:
    """极速查询：直读任务定义 XML，实测约 0.1 毫秒。

    代价是**拿不到运行记录**：下次运行时间 / 上次运行时间 / 上次返回码
    都不在 XML 里，统一给 None，并以 ``partial=True`` 标记。展示方据此
    跳过这些字段，避免出现"上次 未知"这种误导。
    需要完整信息（例如排查"定时不生效"）时再调 query()。
    """
    miss: Dict[str, Any] = {"exists": False, "task": task_name, "partial": True}
    try:
        text = task_xml_path(task_name).read_text(encoding="utf-16",
                                                  errors="replace")
    except (OSError, ValueError):
        return miss
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return miss

    cmd = args = ""
    act = root.find(TASK_NS + "Actions/" + TASK_NS + "Exec")
    if act is not None:
        cmd = (act.findtext(TASK_NS + "Command") or "").strip()
        args = (act.findtext(TASK_NS + "Arguments") or "").strip()

    times: List[str] = []
    jitter = 0
    for tr in root.findall(TASK_NS + "Triggers/" + TASK_NS + "CalendarTrigger"):
        hm = _iso_to_local_hm(tr.findtext(TASK_NS + "StartBoundary") or "")
        if hm and hm not in times:
            times.append(hm)
        jitter = max(jitter, _iso_duration_minutes(
            tr.findtext(TASK_NS + "RandomDelay") or ""))

    enabled = (root.findtext(TASK_NS + "Settings/" + TASK_NS + "Enabled")
               or "true").strip().lower() != "false"

    return {
        "exists": True,
        "task": task_name,
        "state": "Ready" if enabled else "Disabled",
        "enabled": enabled,
        "times": sorted(times),
        "jitter_minutes": jitter,
        "command": cmd,
        "arguments": args,
        "next_run": None,
        "last_run": None,
        "last_result": None,
        "partial": True,
        "source": "xml",
    }


# ---------------- 注册 / 更新 ----------------

_APPLY_TEMPLATE = """
$ErrorActionPreference = 'Stop'
try {{
  $exe = {exe}
  $argStr = {arg}
  $times = {times}
  $jitter = {jitter}
  $name = {name}

  $action = New-ScheduledTaskAction -Execute $exe -Argument $argStr

  $triggers = @()
  foreach ($t in $times) {{
    $params = @{{ Daily = $true; At = $t }}
    if ($jitter -gt 0) {{
      $params['RandomDelay'] = (New-TimeSpan -Minutes $jitter)
    }}
    $triggers += New-ScheduledTaskTrigger @params
  }}

  $settings = New-ScheduledTaskSettingsSet `
      -StartWhenAvailable `
      -AllowStartIfOnBatteries `
      -DontStopIfGoingOnBatteries `
      -ExecutionTimeLimit (New-TimeSpan -Hours 1)

  $principal = New-ScheduledTaskPrincipal `
      -UserId ($env:USERDOMAIN + '\\' + $env:USERNAME) `
      -LogonType Interactive -RunLevel Limited

  Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers `
      -Settings $settings -Principal $principal -Force | Out-Null

  $r = [pscustomobject]@{{ ok = $true; task = $name; triggers = $times.Count }}
}} catch {{
  $r = [pscustomobject]@{{ ok = $false; error = $_.Exception.Message }}
}}
$r | ConvertTo-Json -Compress
"""


def apply(program: str, arguments: str, times: List[str],
          jitter_minutes: int = 0,
          task_name: str = TASK_NAME) -> Tuple[bool, str]:
    """注册或更新计划任务（覆盖同名任务）。

    program    要执行的可执行文件绝对路径
    arguments  传给它的参数（这里是 --run）
    times      ['09:00', '21:00'] —— 每个时间点挂一个每日触发器
    jitter     随机抖动分钟数，0 表示准点执行
    """
    if not program or not os.path.isfile(program):
        return False, "可执行文件不存在：{}".format(program)
    clean = []
    for t in times:
        t = str(t).strip()
        if t:
            clean.append(t)
    if not clean:
        return False, "至少需要一个触发时间"

    script = _APPLY_TEMPLATE.format(
        exe=_ps_quote(program), arg=_ps_quote(arguments),
        times="@(" + ",".join(_ps_quote(t) for t in clean) + ")",
        jitter=int(jitter_minutes or 0), name=_ps_quote(task_name))
    ok, _data, msg = run_ps(script)
    if not ok:
        return False, msg
    return True, "已注册计划任务（{}，每天 {}）".format(
        task_name, "、".join(clean))


# ---------------- 删除 ----------------

_REMOVE_TEMPLATE = """
$ErrorActionPreference = 'Stop'
try {{
  $t = Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue
  if ($null -eq $t) {{
    $r = [pscustomobject]@{{ ok = $true; removed = $false }}
  }} else {{
    Unregister-ScheduledTask -TaskName {name} -Confirm:$false
    $r = [pscustomobject]@{{ ok = $true; removed = $true }}
  }}
}} catch {{
  $r = [pscustomobject]@{{ ok = $false; error = $_.Exception.Message }}
}}
$r | ConvertTo-Json -Compress
"""


def remove(task_name: str = TASK_NAME) -> Tuple[bool, str]:
    """删除任务；任务不存在也算成功（幂等）。"""
    ok, data, msg = run_ps(_REMOVE_TEMPLATE.format(name=_ps_quote(task_name)),
                           timeout=45)
    if not ok:
        return False, msg
    if data.get("removed"):
        return True, "已取消计划任务"
    return True, "计划任务本就不存在"


# ---------------- 本程序的启动命令 ----------------

def launcher() -> Tuple[str, str]:
    """返回计划任务应当执行的 (程序, 参数)。

    **独立运行（exe）永远是"exe 自己 + --run"**：定时执行的脚本就是 exe
    内部的无界面入口，不依赖 Python、不依赖源码目录，exe 拷到哪台机器都能跑。

    源码态只是开发自测：pythonw + 仓库根目录的 run.py（同样 --run）。
    两种情况都用无控制台程序，避免每次触发闪一下黑框。
    """
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), RUN_ARG
    exe = Path(sys.executable).resolve()
    pyw = exe.with_name("pythonw.exe")
    if not pyw.is_file():
        alt = exe.with_name(exe.name.replace("python", "pythonw"))
        pyw = alt if alt.is_file() else exe
    # wb_task.py 在 src/ 下，仓库根目录是它上一级
    entry = Path(__file__).resolve().parent.parent / "run.py"
    return str(pyw), '"{}" {}'.format(entry, RUN_ARG)


def _norm_cmd(text: Any) -> str:
    """比较命令行用：忽略大小写、引号、多余空格。"""
    return " ".join(str(text or "").replace('"', " ").split()).lower()


def same_target(info: Dict[str, Any], program: str = "",
                arguments: str = "") -> bool:
    """计划任务指向的是不是当前这个程序（+ 同样的参数）。"""
    if not info.get("exists"):
        return False
    program = program or launcher()[0]
    arguments = arguments or launcher()[1]
    cmd = str(info.get("command") or "").strip()
    if not cmd:
        return False
    try:
        same_exe = (os.path.normcase(os.path.abspath(cmd))
                    == os.path.normcase(os.path.abspath(program)))
    except (OSError, ValueError):
        same_exe = _norm_cmd(cmd) == _norm_cmd(program)
    return same_exe and _norm_cmd(info.get("arguments")) == _norm_cmd(arguments)


def ensure_current(times: List[str], jitter_minutes: int = 0,
                   task_name: str = TASK_NAME) -> Dict[str, Any]:
    """任务不存在 / 指向旧路径时，按当前程序重新注册一次。

    为什么要静默修：exe 换个目录（桌面 → D 盘）、或者从旧版本升级上来，
    任务里的绝对路径就失效了，症状是"定时突然不跑了"而又没有明显提示。
    每次启动顺手校一次，代价只有一次 XML 读取（约 0.5 毫秒）。

    返回 {"changed", "ok", "message"}：changed=False 表示本来就对，没动它。
    """
    program, arguments = launcher()
    info = query_fast(task_name)
    if same_target(info, program, arguments):
        return {"changed": False, "ok": True, "message": ""}
    if not times:
        return {"changed": False, "ok": False, "message": "没有可用的触发时间"}
    ok, msg = apply(program, arguments, times, jitter_minutes, task_name)
    return {"changed": True, "ok": ok, "message": msg}


# 任务计划自身的状态码（SCHED_S_*），它们**不是**失败，别误报成异常
SCHED_STATUS = {
    267009: ("正在运行", True),
    267010: ("任务已被禁用", False),
    267011: ("尚未运行过", True),      # 新建任务第一次触发前就是这个值
    267012: ("没有后续计划", False),
    267013: ("任务被终止", False),
    267014: ("就绪，尚未运行", True),
}


def describe_last_result(code: Any) -> Tuple[str, bool]:
    """把任务返回码翻译成人话。返回 (描述, 是否成功)。

    计划任务的返回码通常就是被调起进程的退出码，本程序约定：
      0  成功
      2  环境问题（客户端缺失/未登录等）
      其他非零 视为异常
    但有两种情况要单独识别，否则会把正常状态误报成失败：
      * SCHED_S_* 系列（如 267011 = 尚未运行过）
      * HRESULT 形式的系统错误码
    """
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "未知", True
    if c in SCHED_STATUS:
        return SCHED_STATUS[c]
    if c == 0:
        return "成功", True
    if c == 2:
        return "未完成（客户端未就绪或未登录）", False
    # 任务计划自身的 HRESULT，例如 0x800710E0 = 任务实例冲突/被拒
    if c < 0 or c > 0xFFFF:
        return "系统标记为异常（0x{:X}）".format(c & 0xFFFFFFFF), False
    return "失败（退出码 {}）".format(c), False
