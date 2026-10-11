# -*- coding: utf-8 -*-
"""Windows 侧宿主辅助（零依赖，仅标准库 + ctypes）。

职责边界：
  * 找进程 / 找窗口 / 显示隐藏窗口 —— 全部走 Win32 API
  * 读登录态快照 —— 只读文件，不做任何鉴权判断
业务逻辑（IPC 连接、签到接口）不在这里，见 workbuddy_checkin.py。

为什么要自己做窗口控制：
  WorkBuddy 桌面端没有官方"静默启动"参数（全量扫过 app.asar，主进程只解析
  --debug / --serve）。但它原生支持"托盘 + 关闭到托盘"，即"窗口隐藏、进程
  存活"是合法状态。所以做法是：正常拉起 → 用 ShowWindow(SW_HIDE) 把主窗口
  藏起来，进程与 WBIPC 宿主通道继续存活。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from win_quiet import quiet_kwargs

WORKBUDDY_EXE = "WorkBuddy.exe"

# ---------------- Win32 绑定 ----------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_MINIMIZE = 6
SW_RESTORE = 9
GW_OWNER = 4

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL

# 进程快照 / 进程路径（HANDLE 是指针宽度，必须显式声明 restype，
# 否则 64 位下返回值被截断成 32 位，句柄失效 → 枚举结果为空）
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _console_encoding() -> str:
    """Windows 控制台/外部命令的输出编码（中文系统是 GBK 而非 UTF-8）。"""
    try:
        import locale
        return locale.getpreferredencoding(False) or "utf-8"
    except Exception:
        return "utf-8"


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


TH32CS_SNAPPROCESS = 0x00000002

# 结构体定义之后才能绑定这两个签名（argtypes 里要引用具体结构类型，
# 前置的字符串前向引用 ctypes 解析不了，会报 POINTER _type_ type is not set）
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                      ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL


# ---------------- 配置目录 / 登录态文件 ----------------

def config_dir() -> Path:
    """WorkBuddy 配置根目录（与 wbipc 客户端发现规则一致）。"""
    env = (os.environ.get("WORKBUDDY_CONFIG_DIR") or "").strip()
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".workbuddy"


def endpoint_path() -> Path:
    """WBIPC 宿主端点发现文件。存在即意味着宿主通道大概率可用。"""
    return config_dir() / "wbipc" / "endpoint.json"


def account_snapshot_path() -> Path:
    return config_dir() / "storage" / "skeleton" / "account-snapshot.json"


def account_info() -> Dict[str, Any]:
    """读取账号快照。返回 {} 表示未登录 / 文件不可读。

    注意：登出后该文件可能残留，所以调用方不能只凭这里判断"已登录"，
    必须再实际连一次 IPC（见 workbuddy_checkin.detect_env）。
    """
    try:
        obj = json.loads(account_snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    prim = obj.get("primary") if isinstance(obj, dict) else None
    if not isinstance(prim, dict):
        return {}
    uid = str(prim.get("uid") or "").strip()
    if not uid:
        return {}
    return {
        "uid": uid,
        "nickname": str(prim.get("nickname") or ""),
        "edition": str(prim.get("editionType") or ""),
        "is_pro": bool(prim.get("isPro")),
    }


# ---------------- 进程 ----------------

def pids_of(exe_name: str = WORKBUDDY_EXE) -> List[int]:
    """列出同名进程 PID。

    快照优先：CreateToolhelp32Snapshot 是纯 Win32 调用，不依赖外部进程、
    不依赖控制台句柄、不受输出编码影响——打包成无控制台的 windowed exe
    后这条路径最可靠。tasklist 仅作兜底。
    """
    pids = _pids_via_snapshot(exe_name)
    if pids:
        return pids
    return _pids_via_tasklist(exe_name)


def _pids_via_tasklist(exe_name: str) -> List[int]:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq " + exe_name,
             "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
            encoding=_console_encoding(), errors="replace",
            **quiet_kwargs()).stdout
    except Exception:
        return []
    pids: List[int] = []
    target = exe_name.lower()
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == target:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _pids_via_snapshot(exe_name: str) -> List[int]:
    pids: List[int] = []
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return pids
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        target = exe_name.lower()
        while ok:
            if str(entry.szExeFile).lower() == target:
                pids.append(int(entry.th32ProcessID))
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return pids


def process_image_path(pid: int) -> str:
    """取某进程的完整可执行文件路径（拿不到就返回空串）。

    这是"用户实际在用哪个客户端"的最权威线索：比猜安装目录可靠。
    """
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(h, 0, buf,
                                                    ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        _kernel32.CloseHandle(h)


def running_exe_path(exe_name: str = WORKBUDDY_EXE) -> str:
    """正在运行的客户端 exe 路径（多个进程时取最短的那条，即主程序）。"""
    paths = []
    for pid in pids_of(exe_name):
        p = process_image_path(pid)
        if p and p.lower().endswith(exe_name.lower()):
            paths.append(p)
    if not paths:
        return ""
    paths.sort(key=len)
    return paths[0]


# ---------------- 安装位置发现 ----------------

_COMMON_TAILS = (
    "WorkBuddy/WorkBuddy.exe",           # <base>/WorkBuddy/WorkBuddy.exe
    "Programs/WorkBuddy/WorkBuddy.exe",  # %LOCALAPPDATA%/Programs/...
)


def _registry_exe_candidates() -> List[str]:
    """从注册表挖安装路径：App Paths、卸载项 InstallLocation / DisplayIcon。"""
    out: List[str] = []
    try:
        import winreg
    except Exception:
        return out

    def _read(root, sub, wow64=0):
        try:
            key = winreg.OpenKey(root, sub, 0, winreg.KEY_READ | wow64)
        except OSError:
            return None
        return key

    app_path_keys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WorkBuddy.exe",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"
        r"\WorkBuddy.exe",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\CodeBuddy.exe",
    )
    for sub in app_path_keys:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            key = _read(root, sub)
            if not key:
                continue
            try:
                val = winreg.QueryValue(key, None)
                if val:
                    out.append(str(val).strip('"'))
            except OSError:
                pass
            finally:
                winreg.CloseKey(key)

    uninstall_roots = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    for sub_root in uninstall_roots:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            key = _read(root, sub_root)
            if not key:
                continue
            try:
                n = winreg.QueryInfoKey(key)[0]
                for i in range(n):
                    try:
                        name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    sub_key = _read(root, sub_root + "\\" + name)
                    if not sub_key:
                        continue
                    try:
                        try:
                            disp = winreg.QueryValueEx(sub_key,
                                                       "DisplayName")[0]
                        except OSError:
                            disp = ""
                        # 只认 WorkBuddy / CodeBuddy 本体，别把插件错认成主程序
                        if "workbuddy" not in str(disp).lower() and \
                                "codebuddy" not in str(disp).lower():
                            continue
                        for prop in ("DisplayIcon", "InstallLocation"):
                            try:
                                v = winreg.QueryValueEx(sub_key, prop)[0]
                            except OSError:
                                continue
                            v = str(v).strip('"')
                            if not v:
                                continue
                            if v.lower().endswith(".exe"):
                                out.append(v.split(",")[0])
                            else:
                                out.append(v.rstrip("\\/") + r"\WorkBuddy.exe")
                    finally:
                        winreg.CloseKey(sub_key)
            except OSError:
                pass
            finally:
                winreg.CloseKey(key)
    return out


def _common_path_candidates() -> List[str]:
    """常见安装位置。覆盖 64/32 位与用户级安装，盘符 C~G。"""
    out: List[str] = []
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    bases = [pf, pf86]
    if local:
        bases.append(os.path.join(local, "Programs"))
        bases.append(local)
    for base in bases:
        if not base:
            continue
        for tail in _COMMON_TAILS:
            out.append(os.path.join(base, tail))
    for drv in ("C:", "D:", "E:", "F:", "G:"):
        for pfname in ("Program Files", "Program Files (x86)"):
            out.append(drv + "/" + pfname + "/WorkBuddy/WorkBuddy.exe")
        out.append(drv + "/WorkBuddy/WorkBuddy.exe")
    return out


def installed_exe_candidates(exe_name: str = WORKBUDDY_EXE) -> List[str]:
    """按可信度排序的候选安装路径（只返回真实存在的文件）。"""
    cands: List[str] = []
    running = running_exe_path(exe_name)
    if running:
        cands.append(running)
    cands.extend(_registry_exe_candidates())
    cands.extend(_common_path_candidates())

    seen = set()
    out: List[str] = []
    for c in cands:
        if not c:
            continue
        p = os.path.normpath(os.path.expandvars(c))
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if os.path.isfile(p) and p.lower().endswith(exe_name.lower()):
                out.append(p)
        except OSError:
            continue
    return out


def find_workbuddy_exe(preferred: str = "",
                       exe_name: str = WORKBUDDY_EXE) -> str:
    """定位客户端可执行文件。返回空串表示没装/找不到。"""
    if preferred:
        p = os.path.normpath(os.path.expandvars(str(preferred).strip('"')))
        if os.path.isfile(p):
            return p
    cands = installed_exe_candidates(exe_name)
    return cands[0] if cands else ""


def install_root(exe_path: str) -> str:
    """exe 的安装根目录（用于资源/模块发现）。"""
    if not exe_path:
        return ""
    return os.path.dirname(os.path.abspath(exe_path))


# ---------------- 窗口 ----------------

def _window_title(hwnd: int) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def list_windows(pid: Optional[int] = None) -> List[Dict[str, Any]]:
    """枚举顶层窗口。pid 为 None 时返回全部。"""
    found: List[Dict[str, Any]] = []

    def _cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if pid is not None and int(wpid.value) != int(pid):
            return True
        found.append({
            "hwnd": int(hwnd),
            "pid": int(wpid.value),
            "title": _window_title(hwnd),
            "class": _window_class(hwnd),
            "visible": bool(_user32.IsWindowVisible(hwnd)),
            "minimized": bool(_user32.IsIconic(hwnd)),
            "owner": int(_user32.GetWindow(hwnd, GW_OWNER) or 0),
        })
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return found


def host_windows(pids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """WorkBuddy 的所有顶层窗口（含隐藏的）。"""
    if pids is None:
        pids = pids_of()
    out: List[Dict[str, Any]] = []
    for pid in pids:
        out.extend(list_windows(pid))
    return out


def main_window(pids: Optional[List[int]] = None) -> Optional[Dict[str, Any]]:
    """挑出适合当"主窗口"的那一个。

    规则：无 owner 的顶层窗口，优先"有标题且可见"，其次"有标题"。
    这样能跳过托盘/提示之类的辅助窗口。
    """
    wins = [w for w in host_windows(pids) if not w["owner"]]
    titled = [w for w in wins if w["title"]]
    for w in titled:
        if w["visible"] and not w["minimized"]:
            return w
    for w in titled:
        if w["visible"]:
            return w
    if titled:
        return titled[0]
    visible = [w for w in wins if w["visible"]]
    return visible[0] if visible else (wins[0] if wins else None)


def set_window_state(hwnd: int, cmd: int) -> bool:
    _user32.ShowWindow(hwnd, cmd)
    return True


def hide_host_window(timeout: float = 25.0, poll: float = 0.5,
                     pids: Optional[List[int]] = None
                     ) -> Tuple[bool, str]:
    """等待宿主主窗口出现并隐藏它。返回 (是否成功, 说明)。

    窗口可能还没创建，所以轮询等待。已经在跑的客户端也可以直接调，
    隐藏不影响进程存活与 IPC 通道。
    """
    deadline = time.time() + max(1.0, timeout)
    last = "未发现窗口"
    while time.time() < deadline:
        win = main_window(pids)
        if win:
            set_window_state(win["hwnd"], SW_HIDE)
            time.sleep(0.3)
            now = list_windows(win["pid"])
            still = [w for w in now if w["hwnd"] == win["hwnd"]]
            if still and not still[0]["visible"]:
                return True, "已隐藏主窗口（hwnd={}）".format(win["hwnd"])
            if not still:
                return True, "窗口已销毁（进程仍在，视为已隐藏）"
            last = "调用了隐藏但窗口仍可见"
        time.sleep(poll)
    return False, last


def show_host_window() -> Tuple[bool, str]:
    """把宿主主窗口恢复显示（探针 / 调试用）。"""
    win = main_window()
    if not win:
        return False, "未找到主窗口"
    set_window_state(win["hwnd"], SW_RESTORE)
    set_window_state(win["hwnd"], SW_SHOWMINIMIZED)
    try:
        _user32.SetForegroundWindow(win["hwnd"])
    except Exception:
        pass
    return True, "已恢复主窗口（hwnd={}）".format(win["hwnd"])


def snapshot() -> Dict[str, Any]:
    """当前宿主窗口状态摘要（探针输出用）。"""
    pids = pids_of()
    wins = host_windows(pids)
    return {
        "process_running": bool(pids),
        "pids": pids,
        "exe_path": running_exe_path(),
        "installed": bool(installed_exe_candidates()),
        "windows": [
            {"hwnd": w["hwnd"], "title": w["title"],
             "visible": w["visible"], "minimized": w["minimized"],
             "owner": bool(w["owner"])}
            for w in wins
        ],
        "visible_top_level": sum(1 for w in wins if w["visible"] and not w["owner"]),
    }
