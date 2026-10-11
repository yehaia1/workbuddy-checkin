# -*- coding: utf-8 -*-
"""WorkBuddy 每日签到 —— 图形界面版（tkinter，零第三方依赖）。

设计要点：
  * 只做界面与编排，所有业务逻辑复用 workbuddy_checkin.py，不重复实现
  * 启动即自检：装没装、在不在跑、登录没登录，不满足就在顶部挂醒目横幅
  * 所有可调项都在界面里改（设置窗口），不用手工编辑 config.json
  * 后台线程干活，主线程只刷界面（tkinter 不允许跨线程碰控件）
  * 想直接双击就用 → 见 README，或用 PyInstaller 打成单文件 exe

高 DPI 说明（重要）：
  Windows 缩放到 125%/150%/250% 时，进程必须在**创建 Tk 之前**声明 DPI
  感知，否则 Tk 会按 96DPI 换算字体，界面看起来"字特别小"。声明之后还要
  用实际 DPI 校准 tk scaling（字体）与 PX 系数（边距/尺寸），
  这样字号和间距才都跟系统一致。

打包：
  python -m PyInstaller --noconfirm --onefile --windowed --name WorkBuddyCheckin checkin_gui.py
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import workbuddy_checkin as core
import wb_task

APP_TITLE = "WorkBuddy 每日签到"
VERSION = "1.6"

# 界面缩放系数：物理像素 = 逻辑像素 × PX。由 apply_tk_scaling() 按 DPI 设置。
# tkinter 的 padding/width 等参数都是物理像素，不跟着 tk scaling 走，
# 所以必须自己乘一遍，否则 250% 缩放下界面会"挤成一团"。
PX = 1.0


def P(v: float) -> int:
    """逻辑像素 → 物理像素。"""
    return max(1, int(round(v * PX)))


# 字号一律用「点」（正数），tk scaling 会按 DPI 自动换算成像素。
UI_FONT = ("Microsoft YaHei UI", 10)
UI_FONT_SM = ("Microsoft YaHei UI", 9)
UI_FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
TITLE_FONT = ("Microsoft YaHei UI", 14, "bold")
BIG_FONT = ("Microsoft YaHei UI", 13, "bold")
MONO_FONT = ("Consolas", 9)

# 配色（浅色主题，窗口独立于 IDE）
C_BG = "#F5F6F8"
C_CARD = "#FFFFFF"
C_BORDER = "#E3E6EB"
C_TEXT = "#1F2329"
C_MUTED = "#8A9099"
C_PRIMARY = "#2B6CF6"
C_PRIMARY_DK = "#1E56CE"
C_INPUT_BG = "#FFFFFF"
C_OK = "#1E7A46"
C_BAD = "#C0342B"
C_WARN = "#B26A00"

BANNER = {
    "checking": ("#EEF1F5", "#5A6472", "正在检测环境…"),
    "ok":       ("#E8F6EE", "#1E7A46", "环境正常"),
    "warn":     ("#FFF6E5", "#9A6400", "需要处理"),
    "error":    ("#FDECEC", "#C0342B", "无法签到"),
}

# 界面缩放选项：0 = 跟随系统 DPI
SCALE_CHOICES = [
    ("跟随系统 DPI（推荐）", 0),
    ("放大 10%", 110),
    ("放大 25%", 125),
    ("放大 50%", 150),
]


# ---------------- DPI（必须在 Tk 之前调用） ----------------

def enable_dpi_awareness() -> None:
    """声明进程 DPI 感知。

    必须在 tk.Tk() 之前调用：Tk 初始化时会读屏幕 DPI 来换算字体，如果那时
    进程还是"DIP 不感知"，Windows 会虚拟化成 96DPI；等 Tk 建完再声明感知
    就晚了——窗口不再被位图放大，字体却仍按 96DPI 计算，结果就是高缩放屏上
    "字特别小"（250% 缩放下会小 2.5 倍）。
    """
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll
    except Exception:
        return
    try:
        # 1 = system DPI aware：单屏/主屏下最稳，Tk 不必做跨屏 DPI 重排
        windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def system_dpi() -> int:
    """当前系统 DPI（拿不到返回 0）。"""
    if sys.platform != "win32":
        return 0
    try:
        from ctypes import windll
        try:
            dpi = int(windll.user32.GetDpiForSystem())
            if dpi > 0:
                return dpi
        except Exception:
            pass
        hdc = windll.user32.GetDC(0)
        try:
            return int(windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
        finally:
            windll.user32.ReleaseDC(0, hdc)
    except Exception:
        return 0


def apply_tk_scaling(root: tk.Tk, ui_scale: int = 0) -> float:
    """按实际 DPI 校准 tk scaling 与 PX 系数，返回缩放倍数（相对 96DPI）。"""
    global PX
    dpi = system_dpi() or 96
    user = (ui_scale / 100.0) if ui_scale and ui_scale > 0 else 1.0
    factor = (dpi / 96.0) * user
    try:
        # tk scaling 的单位是「每 point 多少像素」，96DPI 下为 96/72
        root.tk.call("tk", "scaling", (dpi / 72.0) * user)
    except Exception:
        pass
    PX = factor
    return factor


# ---------------- 配置读写 ----------------

def ensure_config() -> Path:
    """确保配置文件存在（缺失就按默认写一份出来）。"""
    path = core.CONFIG_PATH
    if not path.exists():
        try:
            path.write_text(
                json.dumps(core.DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass
    return path


def save_config_patch(patch: dict) -> None:
    """把若干配置项写回 config.json（保留用户已有的其它项）。"""
    path = ensure_config()
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cur, dict):
            cur = {}
    except (OSError, ValueError):
        cur = {}
    cur.update(patch)
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def schedule_times_of(cfg: dict) -> list:
    """规范化触发时间列表（兼容 "[\\'09:00\\']" 列表与 "09:00, 21:00" 字符串两种写法）。"""
    raw = cfg.get("schedule_times") or []
    if isinstance(raw, str):
        raw = raw.replace("，", ",").split(",")
    out = []
    for t in raw:
        t = str(t).strip()
        if t:
            out.append(t)
    return out


def add_checkbox(parent, text: str, var: tk.BooleanVar, pady=None):
    """自绘勾选开关（方框 + ✓）。

    为什么不用现成的 Checkbutton：
      * tk 原生 Checkbutton 的指示器是固定尺寸位图，高 DPI 下缩成小方块
      * ttk(clam) 的指示器在本机 250% 缩放下干脆画不出勾选状态
    自绘的话尺寸随字体走，勾没勾一眼可见。
    """
    row = tk.Frame(parent, bg=C_BG)
    row.pack(fill="x", padx=P(18),
             pady=(P(3) if pady is None else pady))
    box = tk.Label(row, font=UI_FONT_SM, bg=C_INPUT_BG, fg=C_PRIMARY,
                   width=2, anchor="center", cursor="hand2",
                   relief="solid", bd=1)
    box.pack(side="left", ipady=P(1))
    lab = tk.Label(row, text=text, font=UI_FONT_SM, bg=C_BG, fg=C_TEXT,
                   anchor="w", justify="left", cursor="hand2")
    lab.pack(side="left", padx=(P(8), 0))

    def paint():
        box.configure(text="✓" if var.get() else "")

    def toggle(_event=None):
        var.set(not var.get())

    box.bind("<Button-1>", toggle)
    lab.bind("<Button-1>", toggle)
    try:
        var.trace_add("write", lambda *_a: paint())
    except Exception:                            # pragma: no cover
        pass
    paint()
    return row


def short_time(iso: str) -> str:
    """2026-10-12T09:11:21 -> 10/12 09:11。"""
    try:
        day, tm = str(iso).split("T")
        return "{} {}".format("/".join(day.split("-")[1:]), tm[:5])
    except Exception:
        return str(iso)[:16]


# ---------------- 设置窗口 ----------------

class SettingsDialog:
    """把 config.json 里的可调项全部搬到界面上。"""

    def __init__(self, app: "App"):
        self.app = app
        self.cfg = dict(app.cfg)
        self.saved = False

        top = self.top = tk.Toplevel(app.root)
        top.title("设置")
        top.configure(bg=C_BG)
        top.resizable(False, False)
        top.transient(app.root)

        # 高 DPI 下 tk 原生的 Checkbutton 指示器是一张固定尺寸的位图，会缩成
        # 一个小方块；Entry 的边框也不跟随缩放。统一换成 ttk 并指定 clam
        # 主题——它由 Tk 自己绘制，尺寸按字体度量算，能跟着 DPI 走。
        style = ttk.Style(top)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Wb.TCheckbutton", background=C_BG, foreground=C_TEXT,
                        font=UI_FONT_SM, focuscolor=C_BG)
        style.map("Wb.TCheckbutton",
                  background=[("active", C_BG)],
                  foreground=[("disabled", C_MUTED)])
        style.configure("Wb.TEntry", fieldbackground=C_INPUT_BG,
                        foreground=C_TEXT, font=UI_FONT_SM, padding=P(3))
        style.configure("Wb.TCombobox", fieldbackground=C_INPUT_BG,
                        background=C_CARD, foreground=C_TEXT,
                        font=UI_FONT_SM, padding=P(2))

        body = tk.Frame(top, bg=C_BG)
        body.pack(fill="both", expand=True, padx=P(18), pady=(P(14), 0))

        # --- 消息推送（只填 key，地址由程序自动拼） ---
        self._section(body, "消息推送（只填 key，地址自动拼）")
        self.var_wecom = tk.StringVar(value=core.wecom_key_of(self.cfg))
        self._entry_row(body, "企业微信 Key", self.var_wecom, width=46)
        self._hint(body, "企业微信机器人：webhook 链接里 key= 后面那段。留空则不推送。")

        self.var_dingtalk = tk.StringVar(value=core.dingtalk_key_of(self.cfg))
        self._entry_row(body, "钉钉 Token", self.var_dingtalk, width=46)
        self._hint(body, "钉钉机器人：链接里 access_token= 后面那段"
                         "（机器人安全设置用「自定义关键词」时需含「签到」二字）。"
                         "留空则不推送。")

        # --- 客户端路径 ---
        self._section(body, "WorkBuddy 客户端")
        row = tk.Frame(body, bg=C_BG)
        row.pack(fill="x", padx=P(18), pady=(P(4), 0))
        self.var_exe = tk.StringVar(
            value=str(self.cfg.get("workbuddy_exe") or ""))
        ttk.Entry(row, textvariable=self.var_exe, style="Wb.TEntry",
                  width=38).pack(side="left", ipady=P(3))
        self._btn(row, "自动检测", self._detect_exe)
        self._btn(row, "浏览…", self._browse_exe)

        self.lbl_exe_state = tk.Label(
            body, text="", font=UI_FONT_SM, bg=C_BG, fg=C_MUTED,
            anchor="w", justify="left", wraplength=P(520))
        self.lbl_exe_state.pack(fill="x", padx=P(18), pady=(P(4), 0))
        self._refresh_exe_state()

        # 内置模块目录：只在自动探测失败时才需要填（特殊安装方式兜底）
        mrow = tk.Frame(body, bg=C_BG)
        mrow.pack(fill="x", padx=P(18), pady=(P(8), 0))
        tk.Label(mrow, text="内置模块目录", font=UI_FONT_SM, bg=C_BG,
                 fg=C_MUTED).pack(side="left", padx=(0, P(8)))
        self.var_ipc = tk.StringVar(value=str(self.cfg.get("ipc_dir") or ""))
        ttk.Entry(mrow, textvariable=self.var_ipc, style="Wb.TEntry",
                  width=30).pack(side="left", ipady=P(3))
        self._btn(mrow, "浏览…", self._browse_ipc)
        self._hint(body, "一般留空即可（自动探测）。只有当界面提示「缺少内置模块」"
                         "时才需要手动指定——选 workbuddy_ipc 所在的父目录，"
                         "该目录下应有 workbuddy_ipc\\__init__.py。")

        # --- 行为 ---
        self._section(body, "运行行为")
        self.var_auto_env = tk.BooleanVar(
            value=bool(self.cfg.get("auto_env_check", True)))
        self.var_launch = tk.BooleanVar(
            value=bool(self.cfg.get("launch_workbuddy", True)))
        self.var_hide = tk.BooleanVar(
            value=bool(self.cfg.get("hide_window", True)))
        self.var_close = tk.BooleanVar(
            value=bool(self.cfg.get("close_after_checkin", True)))
        self._check(body, "启动时自动做一次环境自检", self.var_auto_env)
        self._check(body, "客户端未运行时自动拉起", self.var_launch)
        self._check(body, "拉起后隐藏客户端窗口（后台运行，不打断当前工作）",
                    self.var_hide)
        self._check(body, "签到完成后关闭「由本程序拉起」的客户端", self.var_close)

        # --- 超时 ---
        self._section(body, "超时设置（秒）")
        trow = tk.Frame(body, bg=C_BG)
        trow.pack(fill="x", padx=P(18), pady=(P(4), 0))
        self.var_t_launch = tk.StringVar(
            value=str(self.cfg.get("launch_timeout", 120)))
        self.var_t_hide = tk.StringVar(
            value=str(self.cfg.get("hide_window_timeout", 25)))
        self.var_t_close = tk.StringVar(
            value=str(self.cfg.get("close_timeout", 30)))
        for label, var in (("拉起等待", self.var_t_launch),
                           ("隐藏等待", self.var_t_hide),
                           ("关闭等待", self.var_t_close)):
            cell = tk.Frame(trow, bg=C_BG)
            cell.pack(side="left", padx=(0, P(18)))
            tk.Label(cell, text=label, font=UI_FONT_SM, bg=C_BG,
                     fg=C_MUTED).pack(anchor="w")
            ttk.Entry(cell, textvariable=var, style="Wb.TEntry",
                      width=6).pack(ipady=P(2))

        # --- 界面 ---
        self._section(body, "界面")
        srow = tk.Frame(body, bg=C_BG)
        srow.pack(fill="x", padx=P(18), pady=(P(4), 0))
        tk.Label(srow, text="显示缩放", font=UI_FONT_SM, bg=C_BG,
                 fg=C_MUTED).pack(side="left", padx=(0, P(8)))
        cur = int(self.cfg.get("ui_scale", 0) or 0)
        self.var_scale = tk.StringVar(
            value=next((t for t, v in SCALE_CHOICES if v == cur),
                       SCALE_CHOICES[0][0]))
        ttk.Combobox(srow, textvariable=self.var_scale, state="readonly",
                     style="Wb.TCombobox", width=20,
                     values=[t for t, _ in SCALE_CHOICES]).pack(side="left")
        tk.Label(srow, text="（改完重启程序生效）", font=UI_FONT_SM, bg=C_BG,
                 fg=C_MUTED).pack(side="left", padx=(P(8), 0))

        # --- 底部按钮 ---
        bar = tk.Frame(top, bg=C_BG)
        bar.pack(fill="x", padx=P(18), pady=P(14))
        self._btn(bar, "恢复默认", self._restore_defaults)
        self._btn(bar, "取消", self._cancel)
        tk.Button(bar, text="保存", command=self._save, font=UI_FONT,
                  bg=C_PRIMARY, fg="#FFFFFF", activebackground=C_PRIMARY_DK,
                  activeforeground="#FFFFFF", relief="flat", bd=0,
                  padx=P(20), pady=P(6), cursor="hand2").pack(side="right")

        # 显式连尺寸一起给：wm geometry 一旦只给位置，Tk 就不再自动按内容
        # 调整窗口大小，宽输入框会被切掉。
        top.update_idletasks()
        try:
            w = top.winfo_reqwidth()
            h = top.winfo_reqheight()
            px_ = app.root.winfo_rootx()
            py_ = app.root.winfo_rooty()
            pw = app.root.winfo_width()
            ph = app.root.winfo_height()
            sw = top.winfo_screenwidth()
            sh = top.winfo_screenheight()
            x = px_ + max(0, (pw - w) // 2)
            y = py_ + max(0, (ph - h) // 3)
            x = max(0, min(x, sw - w))
            y = max(0, min(y, sh - h))
            top.geometry("{}x{}+{}+{}".format(w, h, x, y))
        except Exception:
            pass
        top.bind("<Escape>", lambda _e: self._cancel())
        top.grab_set()
        top.focus_set()

    # ----- 小部件工厂 -----

    def _section(self, parent, text):
        f = tk.Frame(parent, bg=C_BG)
        f.pack(fill="x", padx=P(18), pady=(P(14), P(2)))
        tk.Label(f, text=text, font=UI_FONT_BOLD, bg=C_BG,
                 fg=C_TEXT).pack(side="left")
        tk.Frame(f, bg=C_BORDER, height=P(1)).pack(
            side="left", fill="x", expand=True, padx=(P(10), 0), pady=(P(8), 0))

    def _hint(self, parent, text):
        tk.Label(parent, text=text, font=UI_FONT_SM, bg=C_BG, fg=C_MUTED,
                 anchor="w", justify="left", wraplength=P(520)).pack(
            fill="x", padx=P(18), pady=(P(3), 0))

    def _entry_row(self, parent, label, var, width=40):
        row = tk.Frame(parent, bg=C_BG)
        row.pack(fill="x", padx=P(18), pady=(P(4), 0))
        tk.Label(row, text=label, font=UI_FONT_SM, bg=C_BG, fg=C_MUTED,
                 width=14, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, style="Wb.TEntry",
                  width=width).pack(side="left", ipady=P(3))

    def _check(self, parent, text, var):
        add_checkbox(parent, text, var)

    def _btn(self, parent, text, cmd):
        b = tk.Button(parent, text=text, command=cmd, font=UI_FONT_SM,
                      bg=C_CARD, fg=C_TEXT, activebackground="#EDEFF3",
                      relief="flat", bd=0, padx=P(10), pady=P(5),
                      cursor="hand2", highlightbackground=C_BORDER,
                      highlightthickness=1)
        b.pack(side="left", padx=(P(8), 0))
        return b

    # ----- 动作 -----

    def _detect_exe(self):
        found = core.find_workbuddy_exe()
        if found:
            self.var_exe.set(found)
            self.app.log("自动检测到客户端：{}".format(found))
        else:
            self.app.log("自动检测未找到 WorkBuddy.exe，请手动浏览指定。")
        self._refresh_exe_state()

    def _browse_exe(self):
        init = self.var_exe.get().strip() or "C:/Program Files/WorkBuddy"
        path = filedialog.askopenfilename(
            parent=self.top, title="选择 WorkBuddy.exe",
            initialdir=os.path.dirname(init) or "C:/",
            filetypes=[("可执行文件", "*.exe"), ("全部文件", "*.*")])
        if path:
            self.var_exe.set(os.path.normpath(path))
            self._refresh_exe_state()

    def _browse_ipc(self):
        init = (self.var_ipc.get().strip()
                or core.install_root() or "C:/")
        path = filedialog.askdirectory(
            parent=self.top, title="选择 workbuddy_ipc 所在的父目录",
            initialdir=init if os.path.isdir(init) else "C:/")
        if path:
            self.var_ipc.set(os.path.normpath(path))

    def _refresh_exe_state(self):
        raw = self.var_exe.get().strip()
        if raw and os.path.isfile(raw):
            ok, txt = True, "✅ 已找到：{}".format(raw)
        else:
            found = core.find_workbuddy_exe()
            if found:
                ok, txt = True, ("ℹ 当前填写无效，但已自动检测到：{}"
                                 "（点「自动检测」填入）".format(found))
            else:
                ok, txt = False, "❌ 未找到 WorkBuddy.exe，请确认客户端已安装"
        self.lbl_exe_state.configure(text=txt, fg=C_OK if ok else C_BAD)

    def _restore_defaults(self):
        if not messagebox.askyesno("恢复默认", "把所有设置恢复为默认值？",
                                   parent=self.top):
            return
        d = core.DEFAULT_CONFIG
        self.var_wecom.set(d.get("wecom_key", ""))
        self.var_dingtalk.set(d.get("dingtalk_key", ""))
        self.var_exe.set(d.get("workbuddy_exe", ""))
        self.var_ipc.set(d.get("ipc_dir", ""))
        self.var_auto_env.set(bool(d.get("auto_env_check", True)))
        self.var_launch.set(bool(d.get("launch_workbuddy", True)))
        self.var_hide.set(bool(d.get("hide_window", True)))
        self.var_close.set(bool(d.get("close_after_checkin", True)))
        self.var_t_launch.set(str(d.get("launch_timeout", 120)))
        self.var_t_hide.set(str(d.get("hide_window_timeout", 25)))
        self.var_t_close.set(str(d.get("close_timeout", 30)))
        self.var_scale.set(SCALE_CHOICES[0][0])
        self._refresh_exe_state()

    def _collect(self) -> dict:
        def num(var, name, lo=1, hi=3600):
            raw = var.get().strip()
            try:
                v = int(float(raw))
            except ValueError:
                raise ValueError("「{}」必须是数字".format(name))
            if not (lo <= v <= hi):
                raise ValueError("「{}」需在 {}~{} 之间".format(name, lo, hi))
            return v

        exe = self.var_exe.get().strip()
        if exe and not os.path.isfile(exe):
            raise ValueError("客户端路径不存在：{}".format(exe))
        ipc_dir = self.var_ipc.get().strip()
        if ipc_dir and not core.is_ipc_parent(ipc_dir):
            raise ValueError(
                "「内置模块目录」下没有找到 workbuddy_ipc 模块：{}\n"
                "请选择包含 workbuddy_ipc\\__init__.py 的父目录，"
                "或留空交给程序自动探测。".format(ipc_dir))
        scale = next((v for t, v in SCALE_CHOICES
                      if t == self.var_scale.get()), 0)
        return {
            "wecom_key": self.var_wecom.get().strip(),
            "dingtalk_key": self.var_dingtalk.get().strip(),
            # 迁移：统一改用 key 字段，清掉可能残留的完整链接，避免两处配置打架
            "wecom_webhook": "",
            "dingtalk_webhook": "",
            "workbuddy_exe": exe,
            "ipc_dir": ipc_dir,
            "auto_env_check": bool(self.var_auto_env.get()),
            "launch_workbuddy": bool(self.var_launch.get()),
            "hide_window": bool(self.var_hide.get()),
            "close_after_checkin": bool(self.var_close.get()),
            "launch_timeout": num(self.var_t_launch, "拉起等待"),
            "hide_window_timeout": num(self.var_t_hide, "隐藏等待"),
            "close_timeout": num(self.var_t_close, "关闭等待"),
            "ui_scale": scale,
        }

    def _save(self):
        try:
            patch = self._collect()
        except ValueError as e:
            messagebox.showerror("设置有问题", str(e), parent=self.top)
            return
        try:
            save_config_patch(patch)
        except OSError as e:
            messagebox.showerror("保存失败", str(e), parent=self.top)
            return
        old_scale = int(self.cfg.get("ui_scale", 0) or 0)
        self.cfg.update(patch)
        self.app.apply_config(self.cfg)
        self.app.log("设置已保存到 {}".format(core.CONFIG_PATH))
        self.saved = True
        if patch.get("ui_scale") != old_scale:
            messagebox.showinfo("已保存", "显示缩放将在重启程序后生效。",
                                parent=self.top)
        self.top.destroy()

    def _cancel(self):
        self.top.destroy()


class ScheduleDialog:
    """定时签到设置：把定时写进 Windows 计划任务，本程序不需要常驻。

    设计取舍：
      * 界面上「启用定时」= 在系统里注册一个计划任务；「取消定时」= 删除它
      * 到点由系统启动 exe 的无界面入口（--run）执行签到，跑完自动退出
      * 注册走 PowerShell 的 ScheduledTasks 模块，普通权限即可，不弹 UAC
      * 随机抖动用计划任务原生的 RandomDelay，不靠脚本 sleep
    """

    def __init__(self, app: "App"):
        self.app = app
        self.cfg = dict(app.cfg)
        self.busy = False

        top = self.top = tk.Toplevel(app.root)
        top.title("定时签到")
        top.configure(bg=C_BG)
        top.resizable(False, False)
        top.transient(app.root)

        style = ttk.Style(top)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Wb.TCheckbutton", background=C_BG, foreground=C_TEXT,
                        font=UI_FONT_SM, focuscolor=C_BG)
        style.map("Wb.TCheckbutton", background=[("active", C_BG)])
        style.configure("Wb.TEntry", fieldbackground=C_INPUT_BG,
                        foreground=C_TEXT, font=UI_FONT_SM, padding=P(3))

        body = tk.Frame(top, bg=C_BG)
        body.pack(fill="both", expand=True, padx=P(18), pady=(P(14), 0))

        self._hint(body, "定时由 Windows 计划任务负责，本程序不用一直开着 ——"
                         "到点由系统自动启动签到，跑完自动退出。")

        self._section(body, "计划")
        self.var_on = tk.BooleanVar(
            value=bool(self.cfg.get("schedule_enabled")))
        add_checkbox(body, "启用每日自动签到", self.var_on)

        row = tk.Frame(body, bg=C_BG)
        row.pack(fill="x", padx=P(18), pady=(P(8), 0))
        tk.Label(row, text="触发时间", font=UI_FONT_SM, bg=C_BG, fg=C_MUTED,
                 width=10, anchor="w").pack(side="left")
        self.var_times = tk.StringVar(
            value=", ".join(schedule_times_of(self.cfg) or ["09:00"]))
        ttk.Entry(row, textvariable=self.var_times, style="Wb.TEntry",
                  width=20).pack(side="left", ipady=P(3))
        tk.Label(row, text="24 小时制，多个时间用逗号分隔", font=UI_FONT_SM,
                 bg=C_BG, fg=C_MUTED).pack(side="left", padx=(P(8), 0))

        row2 = tk.Frame(body, bg=C_BG)
        row2.pack(fill="x", padx=P(18), pady=(P(8), 0))
        tk.Label(row2, text="随机抖动", font=UI_FONT_SM, bg=C_BG, fg=C_MUTED,
                 width=10, anchor="w").pack(side="left")
        self.var_jitter = tk.StringVar(
            value=str(self.cfg.get("schedule_jitter", 15)))
        ttk.Entry(row2, textvariable=self.var_jitter, style="Wb.TEntry",
                  width=6).pack(side="left", ipady=P(3))
        tk.Label(row2, text="分钟（0 = 准点执行；避免整点扎堆）",
                 font=UI_FONT_SM, bg=C_BG, fg=C_MUTED).pack(
            side="left", padx=(P(8), 0))

        self.var_exit = tk.BooleanVar(
            value=bool(self.cfg.get("exit_after_apply", True)))
        add_checkbox(body, "应用后自动关闭本程序（不常驻内存）",
                     self.var_exit, pady=P(8))

        self._section(body, "系统状态")
        self.lbl_state = tk.Label(
            body, text="", font=UI_FONT_SM, bg=C_BG, fg=C_MUTED,
            anchor="w", justify="left", wraplength=P(560))
        self.lbl_state.pack(fill="x", padx=P(18), pady=(P(2), 0))

        bar = tk.Frame(top, bg=C_BG)
        bar.pack(fill="x", padx=P(18), pady=P(14))
        self.btn_apply = tk.Button(
            bar, text="应用到系统", command=self._apply, font=UI_FONT,
            bg=C_PRIMARY, fg="#FFFFFF", activebackground=C_PRIMARY_DK,
            activeforeground="#FFFFFF", relief="flat", bd=0, padx=P(18),
            pady=P(6), cursor="hand2")
        self.btn_apply.pack(side="left")
        self.btn_remove = tk.Button(
            bar, text="取消定时", command=self._remove, font=UI_FONT_SM,
            bg=C_CARD, fg=C_TEXT, activebackground="#EDEFF3", relief="flat",
            bd=0, padx=P(14), pady=P(6), cursor="hand2",
            highlightbackground=C_BORDER, highlightthickness=1)
        self.btn_remove.pack(side="left", padx=(P(8), 0))
        tk.Button(bar, text="关闭", command=self.top.destroy, font=UI_FONT_SM,
                  bg=C_CARD, fg=C_TEXT, activebackground="#EDEFF3",
                  relief="flat", bd=0, padx=P(14), pady=P(6), cursor="hand2",
                  highlightbackground=C_BORDER,
                  highlightthickness=1).pack(side="right")

        # 先显示"读取中"，再后台查 —— PowerShell 启动有 1~2 秒开销，别卡住开窗
        self.lbl_state.configure(text="正在读取系统计划任务状态…")
        threading.Thread(target=self._probe, daemon=True).start()

        top.update_idletasks()
        self._center()
        top.bind("<Escape>", lambda _e: self.top.destroy())
        top.grab_set()
        top.focus_set()

    # ----- 界面小工具 -----

    def _section(self, parent, text):
        f = tk.Frame(parent, bg=C_BG)
        f.pack(fill="x", padx=P(18), pady=(P(14), P(2)))
        tk.Label(f, text=text, font=UI_FONT_BOLD, bg=C_BG,
                 fg=C_TEXT).pack(side="left")
        tk.Frame(f, bg=C_BORDER, height=P(1)).pack(
            side="left", fill="x", expand=True, padx=(P(10), 0),
            pady=(P(8), 0))

    def _hint(self, parent, text):
        tk.Label(parent, text=text, font=UI_FONT_SM, bg=C_BG, fg=C_MUTED,
                 anchor="w", justify="left", wraplength=P(560)).pack(
            fill="x", padx=P(18), pady=(P(4), 0))

    def _center(self):
        try:
            w, h = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
            px_, py_ = self.app.root.winfo_rootx(), self.app.root.winfo_rooty()
            pw, ph = self.app.root.winfo_width(), self.app.root.winfo_height()
            sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
            x = max(0, min(px_ + (pw - w) // 2, sw - w))
            y = max(0, min(py_ + (ph - h) // 3, sh - h))
            self.top.geometry("{}x{}+{}+{}".format(w, h, x, y))
        except Exception:
            pass

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_apply, self.btn_remove):
            b.configure(state=state)

    # ----- 后台动作 -----

    def _probe(self):
        """读系统任务状态（跑在后台线程）。"""
        info = wb_task.query()
        prog, _args = wb_task.launcher()
        info["stale"] = bool(
            info.get("exists")
            and os.path.normcase(str(info.get("command") or ""))
            != os.path.normcase(prog))
        self.app.post("call", lambda: self._show(info))

    def _show(self, info):
        if info.get("error"):
            self.lbl_state.configure(
                text="⚠ 读取计划任务失败：{}".format(info["error"]), fg=C_WARN)
            return
        if not info.get("exists"):
            self.lbl_state.configure(
                text="○ 系统里还没有注册定时任务", fg=C_MUTED)
            return
        bits = ["● 已注册"]
        times = info.get("times") or []
        if times:
            bits.append("每天 {}".format("、".join(times)))
        if info.get("next_run"):
            bits.append("下次 {}".format(short_time(str(info["next_run"]))))
        res_txt, ok = wb_task.describe_last_result(info.get("last_result"))
        bits.append("上次 {}".format(res_txt))
        if info.get("stale"):
            bits.append("⚠ 任务指向的程序不是当前这个，请重新「应用到系统」")
        self.lbl_state.configure(
            text="　".join(bits),
            fg=C_MUTED if (ok and not info.get("stale")) else C_WARN)

    def _parse(self):
        raw = self.var_times.get().replace("，", ",").split(",")
        times = [t.strip() for t in raw if t.strip()]
        if not times:
            raise ValueError("请至少填一个触发时间，例如 09:00")
        norm = []
        for t in times:
            hh, mm = self._parse_time(t)
            norm.append("{:02d}:{:02d}".format(hh, mm))
        try:
            jitter = int(float(self.var_jitter.get().strip() or 0))
        except ValueError:
            raise ValueError("随机抖动必须是数字（分钟）")
        if not (0 <= jitter <= 120):
            raise ValueError("随机抖动需在 0~120 分钟之间")
        return norm, jitter

    @staticmethod
    def _parse_time(text):
        text = str(text).strip().replace("：", ":")
        try:
            if ":" in text:
                h, m = text.split(":", 1)
                hh, mm = int(h), int(m)
            else:
                hh, mm = int(text), 0
        except ValueError:
            raise ValueError("时间格式不对：{}（应为 HH:MM）".format(text))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("时间超出范围：{}".format(text))
        return hh, mm

    def _apply(self):
        if self.busy:
            return
        try:
            times, jitter = self._parse()
        except ValueError as e:
            messagebox.showerror("定时设置有误", str(e), parent=self.top)
            return
        prog, args = wb_task.launcher()
        if not os.path.isfile(prog):
            messagebox.showerror(
                "无法应用", "找不到要执行的程序：\n{}".format(prog),
                parent=self.top)
            return
        if not getattr(sys, "frozen", False):
            self.app.log("注意：当前是源码运行，定时任务会依赖本机 Python；"
                         "正式使用请用打包后的 exe。")

        self._set_busy(True)
        self.lbl_state.configure(text="正在写入系统计划任务…", fg=C_MUTED)

        def work():
            ok, msg = wb_task.apply(prog, args, times, jitter)
            gone = wb_task.cleanup_legacy() if ok else []
            self.app.post(
                "call", lambda: self._after_apply(ok, msg, times, jitter, gone))
        threading.Thread(target=work, daemon=True).start()

    def _after_apply(self, ok, msg, times, jitter, gone):
        self._set_busy(False)
        if not ok:
            self.lbl_state.configure(text="⚠ 应用失败：{}".format(msg), fg=C_BAD)
            messagebox.showerror(
                "应用失败", msg + "\n\n若反复失败，可尝试以管理员身份运行一次。",
                parent=self.top)
            return

        patch = {
            "schedule_enabled": True,
            "schedule_times": times,
            "schedule_jitter": jitter,
            "exit_after_apply": bool(self.var_exit.get()),
        }
        try:
            save_config_patch(patch)
        except OSError as e:
            messagebox.showerror("保存配置失败", str(e), parent=self.top)
            return

        self.cfg.update(patch)
        self.app.cfg.update(patch)
        self.app.log("定时签到已应用：每天 {}（抖动 ±{} 分钟）".format(
            "、".join(times), jitter))
        if gone:
            self.app.log("已清理旧的计划任务：{}".format("、".join(gone)))
        self.app.refresh_schedule_state()

        if bool(self.var_exit.get()):
            self.top.destroy()
            self.app.log("设置已应用，本程序自动退出；定时由系统接管。")
            self.app.root.after(400, self.app.root.destroy)
        else:
            self._probe()

    def _remove(self):
        if self.busy:
            return
        if not messagebox.askyesno(
                "取消定时", "从系统里删除定时签到计划任务？\n"
                            "（不影响手动签到）", parent=self.top):
            return
        self._set_busy(True)
        self.lbl_state.configure(text="正在删除系统计划任务…", fg=C_MUTED)

        def work():
            ok, msg = wb_task.remove()
            self.app.post("call", lambda: self._after_remove(ok, msg))
        threading.Thread(target=work, daemon=True).start()

    def _after_remove(self, ok, msg):
        self._set_busy(False)
        if not ok:
            messagebox.showerror("取消失败", msg, parent=self.top)
            return
        try:
            save_config_patch({"schedule_enabled": False})
        except OSError as e:
            messagebox.showerror("保存配置失败", str(e), parent=self.top)
            return
        self.cfg["schedule_enabled"] = False
        self.app.cfg["schedule_enabled"] = False
        self.app.sched_state = None
        self.app.log("已取消定时签到")
        self.app.report_config()
        self._probe()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.last_env: dict = {}
        self.cfg = core.load_config()
        self._fit_pending = False

        root.title(APP_TITLE)
        root.configure(bg=C_BG)
        root.geometry("{}x{}+{}+{}".format(P(600), P(440), P(120), P(80)))

        self._build_header()
        self._build_banner()
        self._build_cards()
        self._build_actions()
        self._build_configbar()
        self._build_log()

        self._fit_window()
        self.root.after(120, self._drain)
        # 首次运行就把 config.json 落到程序旁边，用户才知道该改哪个文件
        cfg_path = ensure_config()
        self.log("{} v{} 已启动".format(APP_TITLE, VERSION))
        self.log("系统 DPI {}（缩放 {:.0f}%）".format(
            system_dpi() or 96, PX * 100))
        self.log("程序目录：{}".format(core.SCRIPT_DIR))
        self.log("配置文件：{}".format(cfg_path))
        self.log("客户端路径：{}".format(
            core.find_workbuddy_exe(self.cfg) or "未找到"))
        self.sched_state = None
        self.report_config()
        if self.cfg.get("auto_env_check", True):
            self.start_task(self.task_env_check)

    # ---------------- 窗口自适应 ----------------

    def _schedule_fit(self) -> None:
        if self._fit_pending:
            return
        self._fit_pending = True
        self.root.after_idle(self._fit_window)

    def _fit_window(self) -> None:
        """按内容自适应窗口尺寸。

        不同 DPI 缩放下字体实际像素宽差异很大，而状态文字是异步刷新的
        （横幅、卡片值都会变长），写死尺寸必然被切。所以每次内容更新后
        重新量一次，只放大不缩小，并留出屏幕边界的余量。
        """
        self._fit_pending = False
        try:
            self.root.update_idletasks()
            w = max(self.root.winfo_reqwidth() + P(10), P(600))
            h = max(self.root.winfo_reqheight() + P(10), P(440))
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w = min(w, int(sw * 0.94))
            h = min(h, int(sh * 0.90))
            if self.root.winfo_width() > 1:      # 已显示：只放大，避免抖动
                w = max(w, self.root.winfo_width())
                h = max(h, self.root.winfo_height())
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            if x < 1:
                x = P(120)
            if y < 1:
                y = P(80)
            if x + w > sw:
                x = max(0, sw - w)
            if y + h > sh:
                y = max(0, sh - h)
            self.root.geometry("{}x{}+{}+{}".format(w, h, x, y))
            self.root.minsize(min(P(560), sw), min(P(430), sh))
        except Exception:
            pass

    # ---------------- 界面搭建 ----------------

    def _build_header(self) -> None:
        bar = tk.Frame(self.root, bg=C_CARD, height=P(58))
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text=APP_TITLE, font=TITLE_FONT,
                 bg=C_CARD, fg=C_TEXT).pack(side="left", padx=P(18))
        self.header_right = tk.Label(bar, text="", font=UI_FONT_SM,
                                     bg=C_CARD, fg=C_MUTED)
        self.header_right.pack(side="right", padx=P(18))
        tk.Frame(self.root, bg=C_BORDER, height=P(1)).pack(fill="x")

    def _build_banner(self) -> None:
        wrap = tk.Frame(self.root, bg=C_BG)
        wrap.pack(fill="x", padx=P(14), pady=(P(12), 0))
        self.banner = tk.Frame(wrap, bg=BANNER["checking"][0])
        self.banner.pack(fill="x")
        inner = self.banner_inner = tk.Frame(self.banner,
                                             bg=BANNER["checking"][0])
        inner.pack(fill="x", padx=P(14), pady=P(10))
        self.banner_title = tk.Label(
            inner, text=BANNER["checking"][2],
            font=("Microsoft YaHei UI", 11, "bold"),
            bg=BANNER["checking"][0], fg=BANNER["checking"][1], anchor="w",
            justify="left", wraplength=P(520))
        self.banner_title.pack(fill="x")
        self.banner_desc = tk.Label(inner, text="", font=UI_FONT_SM,
                                    bg=BANNER["checking"][0],
                                    fg=BANNER["checking"][1], anchor="w",
                                    justify="left", wraplength=P(520))
        self.banner_desc.pack(fill="x", pady=(P(2), 0))
        self.banner_btn = tk.Label(inner, text="", font=UI_FONT_SM,
                                   bg=BANNER["checking"][0],
                                   fg=BANNER["checking"][1], cursor="hand2")
        self._banner_inner = inner

    def set_banner(self, level: str, title: str, desc: str = "",
                   action: str = "", action_cmd=None) -> None:
        bg, fg, _ = BANNER.get(level, BANNER["checking"])
        for w in (self.banner, self._banner_inner, self.banner_title,
                  self.banner_desc, self.banner_btn):
            w.configure(bg=bg)
        self.banner_title.configure(text=title, fg=fg)
        self.banner_desc.configure(text=desc, fg=fg)
        if action:
            self.banner_btn.configure(text=action, fg=fg, font=UI_FONT_BOLD)
            self.banner_btn.pack(anchor="w", pady=(P(6), 0))
            self.banner_btn.unbind("<Button-1>")
            if action_cmd:
                self.banner_btn.bind("<Button-1>", lambda _e: action_cmd())
        else:
            self.banner_btn.pack_forget()
        self._schedule_fit()

    def _build_cards(self) -> None:
        wrap = tk.Frame(self.root, bg=C_BG)
        wrap.pack(fill="x", padx=P(14), pady=(P(10), 0))
        self.card_values = {}
        specs = [
            ("今日状态", "today", "—"),
            ("连续签到", "streak", "—"),
            ("本期累计", "total", "—"),
            ("活动截止", "end", "—"),
        ]
        for i, (label, key, default) in enumerate(specs):
            wrap.columnconfigure(i, weight=1, uniform="c")
            card = tk.Frame(wrap, bg=C_CARD, highlightbackground=C_BORDER,
                            highlightthickness=1)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else P(8), 0))
            tk.Label(card, text=label, font=UI_FONT_SM, bg=C_CARD,
                     fg=C_MUTED, anchor="w").pack(fill="x", padx=P(12),
                                                  pady=(P(9), 0))
            v = tk.Label(card, text=default, font=BIG_FONT,
                         bg=C_CARD, fg=C_TEXT, anchor="w")
            v.pack(fill="x", padx=P(12), pady=(0, P(10)))
            self.card_values[key] = v

    def _build_actions(self) -> None:
        wrap = tk.Frame(self.root, bg=C_BG)
        wrap.pack(fill="x", padx=P(14), pady=(P(12), 0))
        row1 = tk.Frame(wrap, bg=C_BG)
        row1.pack(fill="x")
        row2 = tk.Frame(wrap, bg=C_BG)
        row2.pack(fill="x", pady=(P(8), 0))
        self.buttons = []

        def add(parent, text, cmd, primary=False):
            b = tk.Button(
                parent, text=text, command=cmd, font=UI_FONT,
                bg=C_PRIMARY if primary else C_CARD,
                fg="#FFFFFF" if primary else C_TEXT,
                activebackground=C_PRIMARY_DK if primary else "#EDEFF3",
                activeforeground="#FFFFFF" if primary else C_TEXT,
                relief="flat", bd=0, padx=P(16), pady=P(8), cursor="hand2",
                highlightbackground=C_BORDER, highlightthickness=0)
            b.pack(side="left", padx=(0, P(8)))
            self.buttons.append(b)
            return b

        add(row1, "立即签到", lambda: self.start_task(self.task_checkin),
            primary=True)
        add(row1, "只查状态", lambda: self.start_task(self.task_status))
        add(row1, "环境自检", lambda: self.start_task(self.task_env_check))
        add(row2, "定时签到", self.open_schedule)
        add(row2, "测试推送", lambda: self.start_task(self.task_test_notify))
        add(row2, "打开客户端", lambda: self.start_task(self.task_open_client))
        add(row2, "设置", self.open_settings)

    def _build_configbar(self) -> None:
        bar = tk.Frame(self.root, bg=C_CARD, highlightbackground=C_BORDER,
                       highlightthickness=1)
        bar.pack(fill="x", padx=P(14), pady=(P(10), 0))
        self.cfg_summary = tk.Label(bar, text="", font=UI_FONT_SM, bg=C_CARD,
                                    fg=C_MUTED, anchor="w", justify="left",
                                    wraplength=P(420))
        self.cfg_summary.pack(side="left", padx=P(12), pady=P(8))
        link = tk.Label(bar, text="修改设置", font=UI_FONT_SM, bg=C_CARD,
                        fg=C_PRIMARY, cursor="hand2")
        link.pack(side="right", padx=P(12))
        link.bind("<Button-1>", lambda _e: self.open_settings())

    def report_config(self) -> None:
        c = self.cfg
        chans = core.configured_channels(c)
        bits = [
            "定时 {}".format(self._schedule_summary()),
            "自动拉起 {}".format("开" if c.get("launch_workbuddy") else "关"),
            "隐藏窗口 {}".format("开" if c.get("hide_window") else "关"),
            "签到后关闭 {}".format("开" if c.get("close_after_checkin") else "关"),
            "推送 {}".format("＋".join(chans) if chans else "关"),
        ]
        self.cfg_summary.configure(text="　·　".join(bits))
        self._schedule_fit()

    def _schedule_summary(self) -> str:
        """主界面摘要里的定时状态文案。"""
        c = self.cfg
        if not c.get("schedule_enabled"):
            return "未启用"
        txt = "、".join(schedule_times_of(c)) or "已启用"
        jit = int(c.get("schedule_jitter", 0) or 0)
        if jit:
            txt += "（±{}分）".format(jit)
        st = getattr(self, "sched_state", None)
        if st:
            if not st.get("exists"):
                txt += " · 未注册到系统"
            elif st.get("stale"):
                txt += " · 任务路径已失效"
            elif st.get("next_run"):
                txt += " · 下次 " + short_time(str(st["next_run"]))
        return txt

    def apply_config(self, cfg: dict) -> None:
        self.cfg = cfg
        self.report_config()
        self.last_env = {}
        if cfg.get("auto_env_check", True):
            self.start_task(self.task_env_check)

    def _build_log(self) -> None:
        wrap = tk.Frame(self.root, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=P(14), pady=(P(12), P(12)))
        head = tk.Frame(wrap, bg=C_BG)
        head.pack(fill="x")
        tk.Label(head, text="运行日志", font=UI_FONT_SM, bg=C_BG,
                 fg=C_MUTED).pack(side="left")
        link = tk.Label(head, text="打开日志文件", font=UI_FONT_SM, bg=C_BG,
                        fg=C_PRIMARY, cursor="hand2")
        link.pack(side="right")
        link.bind("<Button-1>", lambda _e: self.open_log())
        self.logbox = scrolledtext.ScrolledText(
            wrap, font=MONO_FONT, bg="#FFFFFF", fg="#2A2E35", relief="flat",
            height=11, wrap="word", padx=P(10), pady=P(8),
            highlightbackground=C_BORDER, highlightthickness=1)
        self.logbox.pack(fill="both", expand=True, pady=(P(6), 0))
        self.logbox.configure(state="disabled")

    # ---------------- 线程与消息 ----------------

    def start_task(self, fn) -> None:
        if self.busy:
            self.log("上一个任务还在跑，稍等一下…")
            return
        self.busy = True
        self._set_buttons(False)
        threading.Thread(target=self._guard(fn), daemon=True).start()

    def _guard(self, fn):
        def run():
            try:
                fn()
            except Exception:
                self.log("未处理异常：\n" + traceback.format_exc())
            finally:
                self.q.put(("busy", False))
        return run

    def _set_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for b in self.buttons:
            b.configure(state=state)

    def post(self, kind: str, payload=None) -> None:
        self.q.put((kind, payload))

    def log(self, text: str) -> None:
        self.post("log", text)

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "env":
                    self._apply_env(payload)
                elif kind == "banner":
                    self.set_banner(*payload)
                elif kind == "cards":
                    self._apply_cards(payload)
                elif kind == "busy":
                    self.busy = payload
                    self._set_buttons(not payload)
                elif kind == "call":
                    # 让后台线程能安全地把回调挪回主线程执行
                    try:
                        payload()
                    except Exception:
                        self.log("回调异常：\n" + traceback.format_exc())
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _append_log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.logbox.configure(state="normal")
        for line in str(text).splitlines() or [""]:
            self.logbox.insert("end", "[{}] {}\n".format(stamp, line))
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _apply_cards(self, res: dict) -> None:
        if not isinstance(res, dict):
            return
        code = res.get("result")
        if code == "INACTIVE":
            self.card_values["today"].configure(text="无活动", fg=C_MUTED)
        elif "today_checked_in" in res:
            done = bool(res.get("today_checked_in"))
            self.card_values["today"].configure(
                text="已签到" if done else "未签到",
                fg=C_OK if done else C_BAD)
        if res.get("streak_days") is not None:
            self.card_values["streak"].configure(
                text="{} 天".format(res.get("streak_days")))
        if res.get("total_credits") is not None:
            self.card_values["total"].configure(
                text="{} 积分".format(res.get("total_credits")))
        if res.get("end_time"):
            self.card_values["end"].configure(
                text=self._fmt_end(str(res["end_time"])))
        self.header_right.configure(
            text="最后更新 {}".format(time.strftime("%H:%M:%S")))
        self._schedule_fit()

    @staticmethod
    def _fmt_end(raw: str) -> str:
        """把 '2026-10-15 23:59:59' 压成 '10/15 23:59'，解析不了就原样回显。"""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return time.strftime("%m/%d %H:%M",
                                     time.strptime(raw, fmt))
            except ValueError:
                continue
        return raw

    def _apply_env(self, env: dict) -> None:
        self.last_env = env or {}
        code = env.get("code")
        exe = env.get("exe_path") or ""
        if code == "OK":
            acct = env.get("account") or {}
            who = acct.get("nickname") or (acct.get("uid", "")[:8] + "…")
            self.set_banner(
                "ok", "环境正常：客户端已运行并已登录",
                "账号 {}　宿主通道可用，随时可以签到。".format(who))
        elif code == "CLIENT_NOT_RUNNING":
            self.set_banner(
                "warn", "客户端已安装，但当前没有运行",
                "已找到：{}\n签到需要客户端注入鉴权，点下面按钮启动它"
                "（是否隐藏窗口由设置决定）。".format(exe),
                action="  打开客户端并签到  ",
                action_cmd=self._open_client_click)
        elif code == "NO_CLIENT":
            self.set_banner(
                "error", "未找到 WorkBuddy 客户端",
                "本机没有探测到 WorkBuddy.exe。若已安装但装在特殊位置，"
                "请在「设置」里手动指定客户端路径。",
                action="  打开设置  ", action_cmd=self.open_settings)
        elif code == "NO_ENDPOINT":
            self.set_banner(
                "warn", "客户端在运行，但宿主通道还没就绪",
                "可能仍在启动。等几秒后点「环境自检」重试；"
                "若长时间不动，可用「打开客户端」重新唤起。",
                action="  重新自检  ",
                action_cmd=lambda: self.start_task(self.task_env_check))
        elif code == "NO_MODULE":
            self.set_banner(
                "error", "缺少内置模块，无法连接客户端",
                "{}\n这通常不是没装客户端，而是模块不在预期路径上。"
                "可在「设置 → 内置模块目录」里手动指定它所在的父目录。".format(
                    env.get("message", "")),
                action="  打开设置  ", action_cmd=self.open_settings)
        elif code == "NOT_LOGGED_IN":
            self.set_banner(
                "error", "客户端未登录",
                "{}　请在客户端里完成登录后再回来签到。".format(
                    env.get("message", "")),
                action="  打开客户端  ", action_cmd=self._open_client_click)
        else:
            self.set_banner(
                "error", "检测异常：{}".format(code),
                env.get("message", ""),
                action="  重新自检  ",
                action_cmd=lambda: self.start_task(self.task_env_check))

    # ---------------- 具体任务 ----------------

    def task_env_check(self) -> None:
        self.post("banner", ("checking", "正在检测环境…", "", "", None))
        self.log("开始环境自检…")
        env = core.detect_env(deep=True, cfg=self.cfg)
        for name, val in env.get("steps", []):
            self.log("  {}：{}".format(name, val))
        if env.get("message"):
            self.log("  → {}".format(env["message"]))
        self.post("env", env)
        if env.get("status_data"):
            self.post("cards", {"result": "STATUS", **env["status_data"]})
        self.refresh_schedule_state(log_it=True)

    def refresh_schedule_state(self, log_it: bool = False) -> None:
        """读一次系统计划任务状态（本方法可能跑在后台线程里）。"""
        if not self.cfg.get("schedule_enabled"):
            self.sched_state = None
            self.post("call", self.report_config)
            return
        info = wb_task.query()
        prog, _args = wb_task.launcher()
        info["stale"] = bool(
            info.get("exists")
            and os.path.normcase(str(info.get("command") or ""))
            != os.path.normcase(prog))
        self.sched_state = info
        if log_it:
            if not info.get("exists"):
                self.log("定时任务：配置已启用，但系统里尚未注册"
                         "（点「定时签到」→「应用到系统」）")
            elif info["stale"]:
                self.log("定时任务：系统任务的程序路径与当前不一致，需重新应用")
            else:
                self.log("定时任务：已注册，下次执行 {}".format(
                    info.get("next_run") or "?"))
        self.post("call", self.report_config)

    def task_status(self) -> None:
        self.log("查询今日签到状态…")
        res = core.run_once(self.cfg, check_only=True,
                            progress=self.log, notify=False)
        self.log("结果：{}".format(json.dumps(res, ensure_ascii=False)))
        self.post("cards", res)
        if res.get("result") in ("STATUS", "ALREADY_CLAIMED", "INACTIVE"):
            self.set_banner(
                "ok", "状态查询完成",
                "连续 {} 天，本期累计 {} 积分。".format(
                    res.get("streak_days", "?"),
                    res.get("total_credits", "?")))

    def task_checkin(self) -> None:
        self.log("开始签到…")
        self.post("banner", ("checking", "正在签到…",
                             "如客户端未运行会按设置自动拉起。", "", None))
        res = core.run_once(self.cfg, check_only=False, progress=self.log)
        self.log("结果：{}".format(json.dumps(res, ensure_ascii=False)))
        self.post("cards", res)
        code = res.get("result")
        if code == "CLAIMED":
            amount = res.get("credit", res.get("credits",
                           res.get("today_credit", "?")))
            self.set_banner(
                "ok", "✅ 签到成功，+{} 积分".format(amount),
                "连续 {} 天，本期累计 {} 积分。".format(
                    res.get("streak_days", "?"),
                    res.get("total_credits", "?")))
        elif code == "ALREADY_CLAIMED":
            self.set_banner(
                "ok", "今天已经签过了，无需重复领取",
                "连续 {} 天，本期累计 {} 积分。".format(
                    res.get("streak_days", "?"),
                    res.get("total_credits", "?")))
        elif code == "INACTIVE":
            self.set_banner("warn", "当前没有进行中的签到活动", "")
        else:
            self.set_banner(
                "error", "签到失败：{}".format(code),
                res.get("message", ""),
                action="  重新自检  ",
                action_cmd=lambda: self.start_task(self.task_env_check))

    def task_test_notify(self) -> None:
        chans = core.configured_channels(self.cfg)
        if not chans:
            self.set_banner("warn", "尚未配置推送渠道",
                            "请在「设置」里填企业微信或钉钉机器人的 key。",
                            action="  打开设置  ",
                            action_cmd=self.open_settings)
            return
        self.log("发送测试消息到：{}…".format("、".join(chans)))
        pushed = core.push_all(
            self.cfg, "【WorkBuddy 签到】🔧 测试消息：推送配置正确，链路正常。")
        for name, _ok, detail in pushed:
            self.log("{}：{}".format(name, detail))
        failed = [n for n, ok, _d in pushed if not ok]
        if not failed:
            self.set_banner("ok", "测试消息已发送",
                            "请查看：{}。".format("、".join(chans)))
        else:
            self.set_banner("warn", "推送未成功",
                            "{} 失败，请在「设置」里检查 key。".format(
                                "、".join(failed)),
                            action="  打开设置  ",
                            action_cmd=self.open_settings)

    def _open_client_click(self) -> None:
        self.start_task(self.task_open_client)

    def task_open_client(self) -> None:
        self.log("正在打开 WorkBuddy 客户端…")
        ok, why = core.launch_workbuddy(self.cfg)
        self.log(why)
        if not ok:
            self.set_banner("error", "打开客户端失败", why,
                            action="  打开设置  ",
                            action_cmd=self.open_settings)
            return
        if self.cfg.get("hide_window", True):
            hid, note = core.hide_host_window(self.cfg)
            self.log("窗口隐藏：{}".format(note))
        self.log("等待宿主通道就绪…")
        for _ in range(40):
            time.sleep(3)
            env = core.detect_env(deep=True, cfg=self.cfg)
            if env.get("code") == "OK":
                self.post("env", env)
                if env.get("status_data"):
                    self.post("cards",
                              {"result": "STATUS", **env["status_data"]})
                self.log("宿主通道已就绪。")
                return
        self.set_banner("warn", "打开后仍未就绪",
                        "客户端可能还在启动或未登录，请稍后点「环境自检」。")

    # ---------------- 杂项 ----------------

    def open_settings(self) -> None:
        try:
            SettingsDialog(self)
        except Exception:
            self.log("打开设置失败：\n" + traceback.format_exc())

    def open_schedule(self) -> None:
        try:
            ScheduleDialog(self)
        except Exception:
            self.log("打开定时设置失败：\n" + traceback.format_exc())

    def open_log(self) -> None:
        if not core.LOG_PATH.exists():
            try:
                core.LOG_PATH.touch()
            except OSError:
                pass
        try:
            os.startfile(str(core.LOG_PATH))  # noqa
        except OSError as e:
            self.log("打开日志失败：{}".format(e))


def selftest() -> int:
    """无界面自检：把检测结果写到程序目录的 selftest.json。

    打包后排查"为什么提示检测不到客户端"，用它最直接：
        WorkBuddyCheckin.exe --selftest
    """
    enable_dpi_awareness()
    out = {"version": VERSION, "system_dpi": system_dpi(),
           "scale_percent": round((system_dpi() or 96) / 96.0 * 100),
           "config_path": str(core.CONFIG_PATH),
           "script_dir": str(core.SCRIPT_DIR)}
    try:
        out["env"] = core.detect_env(deep=True)
    except Exception:
        out["env_error"] = traceback.format_exc()
    try:
        import wb_host
        out["host"] = {
            "pids": wb_host.pids_of(),
            "running_exe": wb_host.running_exe_path(),
            "installed_candidates": wb_host.installed_exe_candidates(),
        }
    except Exception:
        out["host_error"] = traceback.format_exc()
    out["frozen"] = bool(getattr(sys, "frozen", False))
    cfg = core.load_config()
    out["install_root"] = core.install_root(cfg) or None
    # 候选目录逐条体检：哪条存在、哪条真的含 workbuddy_ipc 包，
    # 是"找不到模块"这类问题唯一能一眼看明白的证据。
    out["module_roots"] = core.probe_module_roots(cfg)
    try:
        ipc, root = core.load_ipc(cfg)
        out["ipc_module_ok"] = root
    except Exception as e:
        out["ipc_module_error"] = "{}: {}".format(type(e).__name__, e)
    out["ipc_import_errors"] = list(core.IPC_IMPORT_ERRORS)
    out["ipc_search_trace"] = list(core.IPC_SEARCH_TRACE)
    try:
        (core.SCRIPT_DIR / "selftest.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except OSError:
        return 1


# 这些参数表示「无界面执行」，绝不能创建窗口
HEADLESS_FLAGS = ("--run", "--check-only", "--diagnose", "--test-notify")


def _redirect_output() -> None:
    """无界面模式：把 stdout/stderr 无条件接到 UTF-8 的日志文件。

    有两个真实存在、且都会导致「签到明明成功、退出码却是非 0」的坑，
    从而被计划任务标记成失败：

      1) PyInstaller --windowed 打包后，sys.stdout / sys.stderr 可能是 None，
         此时任何 print() 直接抛 AttributeError；
      2) 即使 stdout 存在（例如被无控制台方式启动时仍是 GBK 文本流），
         中文和 emoji 会触发 UnicodeEncodeError。

    所以这里不判断、不保留原始流，统一重定向。
    """
    stream = None
    try:
        stream = open(core.SCRIPT_DIR / "checkin.log", "a",
                      encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return
    sys.stdout = stream
    sys.stderr = stream


def run_headless(argv: list) -> int:
    """无界面执行签到（由 Windows 计划任务调起），全程不创建任何窗口。"""
    _redirect_output()
    return core.main(argv)


SCHED_FLAG = "--sched-status"


def sched_status() -> int:
    """无界面查看计划任务状态（排查「定时不生效」用，不弹任何窗口）。

    windowed exe 自身没有控制台，print 出去没人看得见，所以结果同时落进
    checkin.log —— 这也是「定时不生效怎么查」里最省事的一步。
    """
    info = wb_task.query()
    desc, ok = wb_task.describe_last_result(info.get("last_result"))
    if not info.get("exists"):
        text = "定时签到：未设置（{}）".format(info.get("error") or "任务不存在")
    else:
        text = "\n".join([
            "定时签到：已设置",
            "  任务名    {}".format(info.get("task")),
            "  状态      {}".format(info.get("state")),
            "  触发时间  {}".format("、".join(info.get("times") or []) or "—"),
            "  下次运行  {}".format(info.get("next_run") or "—"),
            "  上次运行  {}（{}）".format(info.get("last_run") or "—", desc),
            "  执行命令  {} {}".format(info.get("command"),
                                      info.get("arguments")),
        ])
    try:
        print(text)
    except Exception:                      # 无控制台时 print 可能失败，忽略
        pass
    try:
        with open(core.SCRIPT_DIR / "checkin.log", "a", encoding="utf-8",
                  errors="replace") as fh:
            fh.write("== 定时状态 ==\n" + text + "\n")
    except OSError:
        pass
    return 0 if ok else 1


def main() -> int:
    argv = sys.argv[1:]
    if "--selftest" in argv:
        return selftest()
    if SCHED_FLAG in argv:
        return sched_status()
    # 计划任务/命令行入口：走这条分支时绝不能弹窗，否则到点会打断用户
    if any(a in HEADLESS_FLAGS for a in argv):
        return run_headless(argv)

    # 关键顺序：先声明 DPI 感知，再创建 Tk
    enable_dpi_awareness()
    root = tk.Tk()
    cfg = core.load_config()
    apply_tk_scaling(root, int(cfg.get("ui_scale", 0) or 0))
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
