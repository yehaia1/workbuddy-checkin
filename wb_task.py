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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from win_quiet import quiet_kwargs

TASK_NAME = "WorkBuddyCheckin"

# v1.0 的 bat 方案（setup_task.bat → run_checkin.bat）注册的遗留任务名。
# 那两个脚本已经移除，但装过旧版的机器上任务可能还留着，而它们指向的 bat
# 已经不存在了，留着只会每天失败一次。所以在这里兜底清理，保持幂等。
LEGACY_TASKS = ("WorkBuddyCheckin_0900", "WorkBuddyCheckin_2100")

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


def cleanup_legacy() -> List[str]:
    """清理 v1.0 bat 方案遗留的任务，返回**确实被删除**的任务名列表。

    注意：任务本来就不存在时**不计入**返回值。旧实现是先 remove 再判断，
    而 remove 是幂等的（不存在也返回成功），于是界面每次应用定时都会
    误报一句「已清理旧的计划任务：_0900、_2100」，其实什么都没清。
    """
    gone: List[str] = []
    for name in LEGACY_TASKS:
        try:
            if not query(name).get("exists"):
                continue        # 本来就没有，别去动它，也别在界面上瞎报
            ok, _msg = remove(name)
            if ok and not query(name).get("exists"):
                gone.append(name)
        except Exception:
            pass
    return gone


# ---------------- 本程序的启动命令 ----------------

def launcher() -> Tuple[str, str]:
    """返回计划任务应当执行的 (程序, 参数)。

    冻结态：exe 自己，加 --run（无界面签到入口）
    源码态：优先 pythonw（无控制台，不闪黑框），退回当前解释器
    """
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), RUN_ARG
    exe = Path(sys.executable).resolve()
    pyw = exe.with_name("pythonw.exe")
    if not pyw.is_file():
        alt = exe.with_name(exe.name.replace("python", "pythonw"))
        pyw = alt if alt.is_file() else exe
    script = str(Path(__file__).resolve().parent / "checkin_gui.py")
    return str(pyw), '"{}" {}'.format(script, RUN_ARG)


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
