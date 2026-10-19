# -*- coding: utf-8 -*-
"""WorkBuddy 每日签到 —— 图形界面（tkinter，零第三方依赖）。

界面只做三件事，对应三块区域：
    ① 账号授权   一次性登录，令牌自动续期
    ② 每日定时   写进 Windows 计划任务，程序不用常驻
    ③ 消息推送   企业微信 / 钉钉，只填 key

其它（启动客户端、隐藏窗口、超时、界面缩放）在 v2.0 全部删掉了——
账号直连不需要客户端，那些配置没有存在意义。

高 DPI：必须在创建 Tk **之前**声明 DPI 感知，否则 250% 缩放下字会小一半。
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import wb_config
import wb_notify
import wb_oauth
import wb_task
import workbuddy_checkin as core

APP_TITLE = wb_config.APP_TITLE
VERSION = wb_config.VERSION

# 界面缩放系数：物理像素 = 逻辑像素 × PX（由 apply_tk_scaling 按 DPI 设置）。
# tkinter 的 padding/width 是物理像素，不跟 tk scaling 走，必须自己乘。
PX = 1.0


def P(v: float) -> int:
    return max(1, int(round(v * PX)))


UI_FONT = ("Microsoft YaHei UI", 10)
UI_FONT_SM = ("Microsoft YaHei UI", 9)
UI_FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
TITLE_FONT = ("Microsoft YaHei UI", 14, "bold")
BIG_FONT = ("Microsoft YaHei UI", 13, "bold")
MONO_FONT = ("Consolas", 9)

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
    "checking": ("#EEF1F5", "#5A6472"),
    "ok":       ("#E8F6EE", "#1E7A46"),
    "warn":     ("#FFF6E5", "#9A6400"),
    "error":    ("#FDECEC", "#C0342B"),
}


# ---------------- DPI（必须在 Tk 之前调用） ----------------

def enable_dpi_awareness() -> None:
    """声明进程 DPI 感知。

    必须在 tk.Tk() 之前调用：Tk 初始化时按屏幕 DPI 换算字体，若那一刻进程
    还是"DIP 不感知"，Windows 会虚拟化成 96DPI，高缩放屏上字就特别小。
    """
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll
    except Exception:
        return
    try:
        windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def system_dpi() -> int:
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


def apply_tk_scaling(root: tk.Tk) -> float:
    """按实际 DPI 校准 tk scaling 与 PX 系数，返回缩放倍数（相对 96DPI）。"""
    global PX
    dpi = system_dpi() or 96
    factor = dpi / 96.0
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass
    PX = factor
    return factor


# ---------------- 通用小部件 ----------------

def add_checkbox(parent, text: str, var: tk.BooleanVar, bg: str = C_BG,
                 pady=None):
    """自绘勾选开关（方框 + ✓）。

    不用原生 Checkbutton：它的指示器是固定尺寸位图，高 DPI 下会缩成小方块。
    """
    row = tk.Frame(parent, bg=bg)
    row.pack(fill="x", padx=P(16), pady=(P(3) if pady is None else pady))
    box = tk.Label(row, font=UI_FONT_SM, bg=C_INPUT_BG, fg=C_PRIMARY,
                   width=2, anchor="center", cursor="hand2",
                   relief="solid", bd=1)
    box.pack(side="left", ipady=P(1))
    lab = tk.Label(row, text=text, font=UI_FONT_SM, bg=bg, fg=C_TEXT,
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


def flat_button(parent, text, cmd, primary=False, small=True):
    b = tk.Button(
        parent, text=text, command=cmd,
        font=UI_FONT_SM if small else UI_FONT,
        bg=C_PRIMARY if primary else C_CARD,
        fg="#FFFFFF" if primary else C_TEXT,
        activebackground=C_PRIMARY_DK if primary else "#EDEFF3",
        activeforeground="#FFFFFF" if primary else C_TEXT,
        relief="flat", bd=0,
        padx=P(14) if primary else P(12),
        pady=P(6) if primary else P(5),
        cursor="hand2", highlightbackground=C_BORDER,
        highlightthickness=1)
    return b


def short_time(iso) -> str:
    """2026-10-12T09:11:21 -> 10/12 09:11。"""
    try:
        day, tm = str(iso).split("T")
        return "{} {}".format("/".join(day.split("-")[1:]), tm[:5])
    except Exception:
        return str(iso)[:16]


def apply_dialog_theme(top: tk.Toplevel) -> ttk.Style:
    style = ttk.Style(top)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("Wb.TEntry", fieldbackground=C_INPUT_BG, foreground=C_TEXT,
                    font=UI_FONT_SM, padding=P(3))
    return style


def center_over(parent: tk.Misc, top: tk.Toplevel, frac: int = 3) -> None:
    """把对话框居中到父窗口（并夹在屏幕内）。

    显式连尺寸一起给：只给位置的话 Tk 不再按内容自动调整大小，宽输入框会被切掉。
    """
    try:
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        px_, py_ = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        x = max(0, min(px_ + (pw - w) // 2, sw - w))
        y = max(0, min(py_ + (ph - h) // frac, sh - h))
        top.geometry("{}x{}+{}+{}".format(w, h, x, y))
    except Exception:
        pass


# ---------------- 授权窗口 ----------------

class AuthDialog:
    """账号授权：本机浏览器 / 推送到手机 两种发起方式。

    为什么单独一个窗口：浏览器（或手机）那头登录完，用户需要看到"到底成没成"，
    否则只能靠再点一次签到去试，失败了也说不清是授权没完成还是别的问题。

    线程模型：网络轮询在后台线程，结果通过队列回主线程刷控件。
    """

    def __init__(self, app: "App", push: bool = False, on_done=None):
        self.app = app
        self.cfg = dict(app.cfg)
        self.push = push
        self.on_done = on_done
        self.stop = False
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.url = ""
        self.done = False
        self.started = time.time()
        self.timeout = int(self.cfg.get("oauth_auth_timeout", 300) or 300)

        top = self.top = tk.Toplevel(app.root)
        top.title("账号授权")
        top.configure(bg=C_BG)
        top.resizable(False, False)
        top.transient(app.root)
        top.protocol("WM_DELETE_WINDOW", self._cancel)
        apply_dialog_theme(top)

        body = tk.Frame(top, bg=C_BG)
        body.pack(fill="both", expand=True, padx=P(20), pady=(P(16), 0))

        title = "在手机上完成授权" if push else "在本机浏览器完成登录"
        tk.Label(body, text=title, font=UI_FONT_BOLD, bg=C_BG,
                 fg=C_TEXT, anchor="w").pack(fill="x")
        desc = ("正在生成授权链接并推送到手机…" if push
                else "正在申请授权链接…")
        self.lbl_desc = tk.Label(body, text=desc, font=UI_FONT_SM, bg=C_BG,
                                 fg=C_MUTED, anchor="w", justify="left",
                                 wraplength=P(480))
        self.lbl_desc.pack(fill="x", pady=(P(6), P(10)))

        self.var_url = tk.StringVar(value="")
        ttk.Entry(body, textvariable=self.var_url, style="Wb.TEntry",
                  width=56, state="readonly").pack(fill="x", ipady=P(3))

        row = tk.Frame(body, bg=C_BG)
        row.pack(fill="x", pady=(P(10), 0))
        self.btn_copy = flat_button(row, "复制链接", self._copy)
        self.btn_copy.pack(side="left", padx=(0, P(8)))
        self.btn_open = flat_button(row, "打开浏览器", self._open)
        self.btn_open.pack(side="left", padx=(0, P(8)))
        if push:
            self.btn_open.pack_forget()
        self.btn_close = flat_button(row, "取消", self._cancel)
        self.btn_close.pack(side="left")

        self.lbl_status = tk.Label(body, text="等待授权…", font=UI_FONT_SM,
                                   bg=C_BG, fg=C_WARN, anchor="w",
                                   justify="left", wraplength=P(480))
        self.lbl_status.pack(fill="x", pady=(P(12), P(16)))

        center_over(app.root, top)
        top.grab_set()

        threading.Thread(target=self._work, daemon=True).start()
        top.after(200, self._drain)

    def _status(self, text, level="info"):
        color = {"ok": C_OK, "bad": C_BAD, "warn": C_WARN}.get(level, C_MUTED)
        self.lbl_status.configure(text=text, fg=color)

    # ----- 后台任务 -----

    def _work(self):
        try:
            rec = core.login_oauth(
                self.cfg,
                progress=lambda m: self.q.put(("msg", m)),
                push=self.push,
                timeout=self.timeout,
                on_url=lambda u: self.q.put(("url", u)),
                should_stop=lambda: self.stop)
            if rec.get("result") == "AUTHORIZED":
                self.q.put(("done", rec))
            else:
                self.q.put(("error", (rec.get("result"), rec.get("message"))))
        except Exception as e:                   # 兜底，不让线程静默死掉
            self.q.put(("error", ("UNEXPECTED", str(e))))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "url":
                    self.url = str(payload)
                    self.var_url.set(self.url)
                    self.lbl_desc.configure(
                        text=("授权链接已发送，请在手机上点开并登录。"
                              "本窗口会一直等到授权完成。") if self.push else
                             ("浏览器已打开授权页；若没反应，点「打开浏览器」"
                              "或复制下方链接手动访问。"))
                elif kind == "msg":
                    self._status(str(payload), "info")
                elif kind == "done":
                    self._finish(True, payload)
                    return
                elif kind == "error":
                    self._finish(False, payload)
                    return
        except queue.Empty:
            pass
        if not self.done:
            elapsed = int(time.time() - self.started)
            if self.url:
                self._status("等待完成授权… 已等 {} 秒（最多 {} 秒）".format(
                    elapsed, self.timeout), "warn")
            self.top.after(300, self._drain)

    def _finish(self, ok, payload):
        self.done = True
        if ok:
            who = (payload or {}).get("nickname") or "账号"
            self._status("✅ 授权成功：{}。可以关闭本窗口了。".format(who), "ok")
            self.btn_close.configure(text="完成")
            try:
                self.top.after(1500, self._close)
            except Exception:
                pass
            self.app.log("授权成功：{}{}".format(
                who, "（uid {}）".format(payload.get("uid"))
                if payload and payload.get("uid") else ""))
        else:
            code, msg = payload
            hint = {
                "OAUTH_TIMEOUT": "超时了，重新发起一次授权。",
                "OAUTH_STATE_EXPIRED": "授权会话过期，请重新发起。",
                "OAUTH_NETWORK": "网络不通，检查代理/防火墙后重试。",
                "PUSH_NOT_CONFIGURED": "先在「推送」里选好渠道并填 key。",
                "PUSH_FAILED": "推送失败，检查机器人 key 是否有效。",
            }.get(code, "可重试；反复失败请把日志发出来看。")
            self._status("❌ {}　{}".format(msg, hint), "bad")
            self.btn_close.configure(text="关闭")
            self.app.log("授权失败：{} {}".format(code, msg))
        try:
            self.top.grab_release()
        except Exception:
            pass
        if self.on_done:
            try:
                self.on_done()
            except Exception:
                pass

    # ----- 按钮 -----

    def _copy(self):
        if not self.url:
            return
        try:
            self.top.clipboard_clear()
            self.top.clipboard_append(self.url)
            self._status("链接已复制。", "ok")
        except Exception:
            pass

    def _open(self):
        if self.url:
            wb_oauth._open_browser(self.url)

    def _cancel(self):
        self.stop = True
        self._close()

    def _close(self):
        self.done = True
        try:
            self.top.destroy()
        except Exception:
            pass


class ImportDialog:
    """导入服务端授权结果：粘贴 WBAT1.xxx 导入码，或选一个令牌文件。"""

    def __init__(self, app: "App", on_done=None):
        self.app = app
        self.on_done = on_done

        top = self.top = tk.Toplevel(app.root)
        top.title("导入授权")
        top.configure(bg=C_BG)
        top.resizable(False, False)
        top.transient(app.root)
        apply_dialog_theme(top)

        body = tk.Frame(top, bg=C_BG)
        body.pack(fill="both", expand=True, padx=P(20), pady=(P(16), 0))

        tk.Label(body, text="粘贴服务端给出的导入码", font=UI_FONT_BOLD,
                 bg=C_BG, fg=C_TEXT, anchor="w").pack(fill="x")
        tk.Label(body, text="服务端脚本跑完后会输出一串 WBAT1. 开头的导入码，"
                            "整段粘到下面即可。导入码等同于令牌本身，别外传。",
                 font=UI_FONT_SM, bg=C_BG, fg=C_MUTED, anchor="w",
                 justify="left", wraplength=P(470)).pack(
            fill="x", pady=(P(6), P(8)))

        self.txt = tk.Text(body, height=5, width=56, font=MONO_FONT,
                           bg=C_INPUT_BG, fg=C_TEXT, relief="flat",
                           wrap="char", padx=P(8), pady=P(6),
                           highlightbackground=C_BORDER, highlightthickness=1)
        self.txt.pack(fill="x")

        self.lbl_status = tk.Label(body, text="", font=UI_FONT_SM, bg=C_BG,
                                   fg=C_MUTED, anchor="w", justify="left",
                                   wraplength=P(470))
        self.lbl_status.pack(fill="x", pady=(P(8), P(4)))

        bar = tk.Frame(top, bg=C_BG)
        bar.pack(fill="x", padx=P(20), pady=P(14))
        flat_button(bar, "从文件导入…", self._from_file).pack(
            side="left", padx=(0, P(8)))
        flat_button(bar, "取消", self.top.destroy).pack(side="right")
        flat_button(bar, "导入", self._do_import, primary=True).pack(
            side="right", padx=(0, P(8)))

        center_over(app.root, top)
        top.bind("<Escape>", lambda _e: self.top.destroy())
        top.grab_set()
        top.focus_set()

    def _from_file(self):
        path = filedialog.askopenfilename(
            parent=self.top, title="选择令牌文件",
            filetypes=[("令牌文件", "auth_oauth.json"), ("JSON", "*.json"),
                       ("全部文件", "*.*")])
        if path:
            self._apply(path)

    def _do_import(self):
        raw = self.txt.get("1.0", "end").strip()
        if not raw:
            self.lbl_status.configure(text="请先粘贴导入码。", fg=C_BAD)
            return
        self._apply(raw)

    def _apply(self, raw):
        try:
            res = core.import_token(raw, self.app.cfg)
        except Exception as e:
            self.lbl_status.configure(text="❌ {}".format(e), fg=C_BAD)
            return
        who = res.get("nickname") or "账号"
        self.app.log("授权已导入：{}".format(who))
        self.lbl_status.configure(text="✅ 导入成功：{}".format(who), fg=C_OK)
        if self.on_done:
            try:
                self.on_done()
            except Exception:
                pass
        self.top.after(800, self.top.destroy)


# ---------------- 定时窗口 ----------------

class ScheduleDialog:
    """每日定时：写进 Windows 计划任务，程序本身不用常驻。

      * 「应用到系统」= 注册/覆盖计划任务；「取消定时」= 删掉它
      * 到点由系统启动 exe 的无界面入口（--run）签到，跑完退出
      * 随机抖动用计划任务原生的 RandomDelay，不靠脚本 sleep
    """

    def __init__(self, app: "App"):
        self.app = app
        self.cfg = dict(app.cfg)
        self.busy = False

        top = self.top = tk.Toplevel(app.root)
        top.title("每日定时")
        top.configure(bg=C_BG)
        top.resizable(False, False)
        top.transient(app.root)
        style = apply_dialog_theme(top)
        style.configure("Wb.TCheckbutton", background=C_BG, foreground=C_TEXT,
                        font=UI_FONT_SM, focuscolor=C_BG)

        body = tk.Frame(top, bg=C_BG)
        body.pack(fill="both", expand=True, padx=P(18), pady=(P(14), 0))

        tk.Label(body, text="定时由 Windows 计划任务负责，本程序不用一直开着 —— "
                            "到点由系统启动签到，跑完自动退出。",
                 font=UI_FONT_SM, bg=C_BG, fg=C_MUTED, anchor="w",
                 justify="left", wraplength=P(540)).pack(
            fill="x", padx=P(16), pady=(0, P(6)))

        self._section(body, "计划")
        self.var_on = tk.BooleanVar(value=bool(self.cfg.get("schedule_enabled")))
        add_checkbox(body, "启用每日自动签到", self.var_on)
        self.lbl_on_hint = tk.Label(
            body, text="", font=UI_FONT_SM, bg=C_BG, fg=C_MUTED, anchor="w")
        self.lbl_on_hint.pack(fill="x", padx=P(16), pady=(P(2), 0))

        row = tk.Frame(body, bg=C_BG)
        row.pack(fill="x", padx=P(16), pady=(P(8), 0))
        tk.Label(row, text="触发时间", font=UI_FONT_SM, bg=C_BG, fg=C_MUTED,
                 width=10, anchor="w").pack(side="left")
        self.var_times = tk.StringVar(
            value=", ".join(self.cfg.get("schedule_times") or ["09:00"]))
        ttk.Entry(row, textvariable=self.var_times, style="Wb.TEntry",
                  width=20).pack(side="left", ipady=P(3))
        tk.Label(row, text="24 小时制，多个时间用逗号分隔", font=UI_FONT_SM,
                 bg=C_BG, fg=C_MUTED).pack(side="left", padx=(P(8), 0))

        row2 = tk.Frame(body, bg=C_BG)
        row2.pack(fill="x", padx=P(16), pady=(P(8), 0))
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
                     self.var_exit, pady=P(10))

        self._section(body, "系统状态")
        self.lbl_state = tk.Label(
            body, text="正在读取系统计划任务状态…", font=UI_FONT_SM, bg=C_BG,
            fg=C_MUTED, anchor="w", justify="left", wraplength=P(540))
        self.lbl_state.pack(fill="x", padx=P(16), pady=(P(2), 0))

        bar = tk.Frame(top, bg=C_BG)
        bar.pack(fill="x", padx=P(18), pady=P(14))
        self.btn_apply = flat_button(bar, "应用到系统", self._apply,
                                     primary=True, small=False)
        self.btn_apply.pack(side="left")
        self.btn_remove = flat_button(bar, "取消定时", self._remove)
        self.btn_remove.pack(side="left", padx=(P(8), 0))
        flat_button(bar, "关闭", self.top.destroy).pack(side="right")

        threading.Thread(target=self._probe, daemon=True).start()
        center_over(app.root, top)
        top.bind("<Escape>", lambda _e: self.top.destroy())
        top.grab_set()
        top.focus_set()

    def _section(self, parent, text):
        f = tk.Frame(parent, bg=C_BG)
        f.pack(fill="x", padx=P(16), pady=(P(14), P(2)))
        tk.Label(f, text=text, font=UI_FONT_BOLD, bg=C_BG,
                 fg=C_TEXT).pack(side="left")
        tk.Frame(f, bg=C_BORDER, height=P(1)).pack(
            side="left", fill="x", expand=True, padx=(P(10), 0),
            pady=(P(8), 0))

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_apply, self.btn_remove):
            b.configure(state=state)

    # ----- 后台动作 -----

    def _probe(self):
        """两段式读状态：先读 XML（0.5ms），再补运行记录（PowerShell 约 5s）。"""
        fast = wb_task.query_fast()
        fast["stale"] = self._is_stale(fast)
        self.app.post("call", lambda: self._show(fast))

        info = wb_task.query()
        if info.get("partial") or (info.get("error") and fast.get("exists")):
            return
        info["stale"] = self._is_stale(info)
        self.app.post("call", lambda: self._show(info))

    @staticmethod
    def _is_stale(info: dict) -> bool:
        prog, _args = wb_task.launcher()
        return bool(info.get("exists")
                    and os.path.normcase(str(info.get("command") or ""))
                    != os.path.normcase(prog))

    def _show(self, info):
        self.lbl_on_hint.configure(
            text="应用后由系统接管，本程序可以退出。")
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
        if info.get("jitter_minutes"):
            bits.append("±{} 分".format(info["jitter_minutes"]))
        if info.get("next_run"):
            bits.append("下次 {}".format(short_time(str(info["next_run"]))))
        ok = True
        if info.get("partial"):
            bits.append("正在读取运行记录…")
        else:
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
        if not self.var_on.get():
            messagebox.showinfo("未启用",
                                "勾选「启用每日自动签到」后再应用。",
                                parent=self.top)
            return
        try:
            times, jitter = self._parse()
        except ValueError as e:
            messagebox.showerror("定时设置有误", str(e), parent=self.top)
            return
        prog, args = wb_task.launcher()
        if not os.path.isfile(prog):
            messagebox.showerror("无法应用", "找不到要执行的程序：\n{}".format(prog),
                                 parent=self.top)
            return
        if not getattr(sys, "frozen", False):
            self.app.log("注意：当前是源码运行，定时任务依赖本机 Python；"
                         "正式使用请用打包后的 exe。")

        self._set_busy(True)
        self.lbl_state.configure(text="正在写入系统计划任务…", fg=C_MUTED)

        def work():
            ok, msg = wb_task.apply(prog, args, times, jitter)
            self.app.post("call", lambda: self._after_apply(ok, msg, times, jitter))
        threading.Thread(target=work, daemon=True).start()

    def _after_apply(self, ok, msg, times, jitter):
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
            wb_config.save_patch(patch)
        except OSError as e:
            messagebox.showerror("保存配置失败", str(e), parent=self.top)
            return

        self.cfg.update(patch)
        self.app.cfg.update(patch)
        self.app.log("定时签到已应用：每天 {}（抖动 ±{} 分钟）".format(
            "、".join(times), jitter))
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
                "取消定时", "从系统里删除定时签到计划任务？\n（不影响手动签到）",
                parent=self.top):
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
            wb_config.save_patch({"schedule_enabled": False})
        except OSError as e:
            messagebox.showerror("保存配置失败", str(e), parent=self.top)
            return
        self.cfg["schedule_enabled"] = False
        self.app.cfg["schedule_enabled"] = False
        self.var_on.set(False)
        self.app.sched_state = None
        self.app.log("已取消定时签到")
        self.app.refresh_schedule_state()
        self._probe()


# ---------------- 推送窗口 ----------------

class NotifyDialog:
    """推送类型与配置：勾选渠道 + 填 key + 发测试。"""

    def __init__(self, app: "App"):
        self.app = app
        self.cfg = dict(app.cfg)

        top = self.top = tk.Toplevel(app.root)
        top.title("消息推送")
        top.configure(bg=C_BG)
        top.resizable(False, False)
        top.transient(app.root)
        apply_dialog_theme(top)

        body = tk.Frame(top, bg=C_BG)
        body.pack(fill="both", expand=True, padx=P(18), pady=(P(14), 0))

        tk.Label(body, text="签到结果会推到勾选的渠道。两个都不勾 = 不推送。",
                 font=UI_FONT_SM, bg=C_BG, fg=C_MUTED, anchor="w",
                 justify="left", wraplength=P(500)).pack(
            fill="x", padx=P(16), pady=(0, P(4)))

        picked = set(self.cfg.get("push_channels") or [])
        self._section(body, "推送类型")
        self.var_wecom = tk.BooleanVar(value="wecom" in picked)
        self.var_dingtalk = tk.BooleanVar(value="dingtalk" in picked)
        add_checkbox(body, "企业微信群机器人", self.var_wecom)
        add_checkbox(body, "钉钉群机器人", self.var_dingtalk)

        self._section(body, "推送配置")
        self.var_wecom_key = tk.StringVar(
            value=str(self.cfg.get("wecom_key") or ""))
        self.var_ding_key = tk.StringVar(
            value=str(self.cfg.get("dingtalk_key") or ""))

        self._entry(body, "企业微信 Key", self.var_wecom_key)
        self._hint(body, "webhook 链接里 key= 后面那段。只填 key，地址程序自动拼。")
        self._entry(body, "钉钉 Token", self.var_ding_key)
        self._hint(body, "链接里 access_token= 后面那段；"
                         "机器人安全设置用「自定义关键词」时需含「签到」二字。")

        self.lbl_result = tk.Label(body, text="", font=UI_FONT_SM, bg=C_BG,
                                   fg=C_MUTED, anchor="w", justify="left",
                                   wraplength=P(500))
        self.lbl_result.pack(fill="x", padx=P(16), pady=(P(10), 0))

        bar = tk.Frame(top, bg=C_BG)
        bar.pack(fill="x", padx=P(18), pady=P(14))
        flat_button(bar, "发送测试", self._test).pack(side="left")
        flat_button(bar, "取消", self.top.destroy).pack(side="right")
        flat_button(bar, "保存", self._save, primary=True).pack(
            side="right", padx=(0, P(8)))

        center_over(app.root, top)
        top.bind("<Escape>", lambda _e: self.top.destroy())
        top.grab_set()
        top.focus_set()

    def _section(self, parent, text):
        f = tk.Frame(parent, bg=C_BG)
        f.pack(fill="x", padx=P(16), pady=(P(14), P(2)))
        tk.Label(f, text=text, font=UI_FONT_BOLD, bg=C_BG,
                 fg=C_TEXT).pack(side="left")
        tk.Frame(f, bg=C_BORDER, height=P(1)).pack(
            side="left", fill="x", expand=True, padx=(P(10), 0),
            pady=(P(8), 0))

    def _entry(self, parent, label, var):
        row = tk.Frame(parent, bg=C_BG)
        row.pack(fill="x", padx=P(16), pady=(P(4), 0))
        tk.Label(row, text=label, font=UI_FONT_SM, bg=C_BG, fg=C_MUTED,
                 width=12, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, style="Wb.TEntry",
                  width=44).pack(side="left", ipady=P(3))

    def _hint(self, parent, text):
        tk.Label(parent, text=text, font=UI_FONT_SM, bg=C_BG, fg=C_MUTED,
                 anchor="w", justify="left", wraplength=P(500)).pack(
            fill="x", padx=(P(16), P(16)), pady=(P(2), 0))

    def _collect(self) -> dict:
        channels = []
        if self.var_wecom.get():
            channels.append("wecom")
        if self.var_dingtalk.get():
            channels.append("dingtalk")
        wecom = self.var_wecom_key.get().strip()
        ding = self.var_ding_key.get().strip()
        for name, on, key in (("企业微信", self.var_wecom.get(), wecom),
                              ("钉钉", self.var_dingtalk.get(), ding)):
            if on and not key:
                raise ValueError("勾选了{}，但没有填 key。".format(name))
        return {"push_channels": channels, "wecom_key": wecom,
                "dingtalk_key": ding}

    def _test(self):
        try:
            patch = self._collect()
        except ValueError as e:
            self.lbl_result.configure(text="⚠ {}".format(e), fg=C_BAD)
            return
        tmp = dict(self.cfg)
        tmp.update(patch)
        chans = wb_notify.selected_channels(tmp)
        if not chans:
            self.lbl_result.configure(
                text="⚠ 没有可推送的渠道：勾选渠道并填 key。", fg=C_BAD)
            return
        self.lbl_result.configure(text="正在发送测试消息…", fg=C_MUTED)

        def work():
            pushed = wb_notify.push(
                tmp, "【WorkBuddy 签到】🔧 测试消息：推送配置正确，链路正常。")
            self.app.post("call", lambda: self._after_test(pushed))
        threading.Thread(target=work, daemon=True).start()

    def _after_test(self, pushed):
        text = "；".join("{}：{}".format(n, d) for n, _ok, d in pushed)
        ok = all(o for _n, o, _d in pushed)
        self.lbl_result.configure(text=("✅ " if ok else "❌ ") + text,
                                  fg=C_OK if ok else C_BAD)
        for n, _o, d in pushed:
            self.app.log("测试推送 - {}：{}".format(n, d))

    def _save(self):
        try:
            patch = self._collect()
        except ValueError as e:
            messagebox.showerror("推送设置有误", str(e), parent=self.top)
            return
        try:
            wb_config.save_patch(patch)
        except OSError as e:
            messagebox.showerror("保存失败", str(e), parent=self.top)
            return
        self.cfg.update(patch)
        self.app.apply_config(self.cfg)
        self.app.log("推送设置已保存（{}）".format(
            "、".join(wb_notify.label(c) for c in patch["push_channels"])
            or "不推送"))
        self.top.destroy()


# ---------------- 主窗口 ----------------

class Section:
    """主界面上的一个功能区块：标题 + 状态 + 按钮行。"""

    def __init__(self, app: "App", parent, index: int, title: str):
        self.app = app
        wrap = tk.Frame(parent, bg=C_CARD, highlightbackground=C_BORDER,
                        highlightthickness=1)
        wrap.pack(fill="x", padx=P(14), pady=(P(10), 0))
        head = tk.Frame(wrap, bg=C_CARD)
        head.pack(fill="x", padx=P(14), pady=(P(10), 0))
        tk.Label(head, text="{}  {}".format(index, title), font=UI_FONT_BOLD,
                 bg=C_CARD, fg=C_TEXT).pack(side="left")
        self.lbl_status = tk.Label(head, text="", font=UI_FONT_SM, bg=C_CARD,
                                   fg=C_MUTED)
        self.lbl_status.pack(side="right")
        self.lbl_detail = tk.Label(wrap, text="", font=UI_FONT_SM, bg=C_CARD,
                                   fg=C_MUTED, anchor="w", justify="left",
                                   wraplength=P(560))
        self.lbl_detail.pack(fill="x", padx=P(14), pady=(P(2), 0))
        self.btn_row = tk.Frame(wrap, bg=C_CARD)
        self.btn_row.pack(fill="x", padx=P(12), pady=(P(8), P(10)))
        self.buttons = []

    def add(self, text, cmd, primary=False):
        b = flat_button(self.btn_row, text, cmd, primary=primary)
        b.pack(side="left", padx=(P(2), P(6)))
        self.buttons.append(b)
        return b

    def set(self, status: str, detail: str = "", color: str = C_OK):
        self.lbl_status.configure(text=status, fg=color)
        self.lbl_detail.configure(text=detail)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.cfg = core.load_config()
        self.sched_state = None
        self._fit_size = None

        root.title(APP_TITLE)
        root.configure(bg=C_BG)

        self._build_header()
        self._build_banner()
        self._build_cards()
        self._build_actions()
        self._build_sections()
        self._build_log()

        self.root.after(120, self._drain)
        cfg_path = wb_config.ensure_file()
        self.log("{} v{} 已启动".format(APP_TITLE, VERSION))
        self.log("运行方式：{}".format(
            "独立运行（exe）" if wb_config.run_mode() == "frozen"
            else "本地源码运行"))
        self.log("程序目录：{}".format(wb_config.SCRIPT_DIR))
        self.log("配置文件：{}".format(cfg_path))
        self.log("系统 DPI {}（缩放 {:.0f}%）".format(
            system_dpi() or 96, PX * 100))

        self.refresh_all()
        self.start_task(self.task_env_check, quiet=True)

    # ---------------- 界面搭建 ----------------

    def _build_header(self) -> None:
        bar = tk.Frame(self.root, bg=C_CARD, height=P(56))
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text=APP_TITLE, font=TITLE_FONT, bg=C_CARD,
                 fg=C_TEXT).pack(side="left", padx=P(18))
        self.header_right = tk.Label(bar, text="v{}".format(VERSION),
                                     font=UI_FONT_SM, bg=C_CARD, fg=C_MUTED)
        self.header_right.pack(side="right", padx=P(18))
        tk.Frame(self.root, bg=C_BORDER, height=P(1)).pack(fill="x")

    def _build_banner(self) -> None:
        wrap = tk.Frame(self.root, bg=C_BG)
        wrap.pack(fill="x", padx=P(14), pady=(P(10), 0))
        self.banner = tk.Frame(wrap, bg=BANNER["checking"][0])
        self.banner.pack(fill="x")
        inner = self.banner_inner = tk.Frame(self.banner,
                                             bg=BANNER["checking"][0])
        inner.pack(fill="x", padx=P(14), pady=P(8))
        self.banner_text = tk.Label(
            inner, text="正在检测环境…", font=UI_FONT_SM,
            bg=BANNER["checking"][0], fg=BANNER["checking"][1],
            anchor="w", justify="left", wraplength=P(540))
        self.banner_text.pack(side="left", fill="x", expand=True)
        self.banner_btn = tk.Label(inner, text="", font=UI_FONT_BOLD,
                                   bg=BANNER["checking"][0],
                                   fg=BANNER["checking"][1], cursor="hand2")
        self._banner_action = None

    def set_banner(self, level: str, text: str,
                   action: str = "", action_cmd=None) -> None:
        bg, fg = BANNER.get(level, BANNER["checking"])
        for w in (self.banner, self.banner_inner, self.banner_text,
                  self.banner_btn):
            w.configure(bg=bg)
        self.banner_text.configure(text=text, fg=fg)
        if action:
            self.banner_btn.configure(text=action, fg=fg)
            self.banner_btn.pack(side="right", padx=(P(12), 0))
            self.banner_btn.unbind("<Button-1>")
            if action_cmd:
                self.banner_btn.bind("<Button-1>", lambda _e: action_cmd())
        else:
            self.banner_btn.pack_forget()

    def _build_cards(self) -> None:
        wrap = tk.Frame(self.root, bg=C_BG)
        wrap.pack(fill="x", padx=P(14), pady=(P(10), 0))
        self.card_values = {}
        specs = [("今日状态", "today", "—"), ("连续签到", "streak", "—"),
                 ("本期累计", "total", "—"), ("活动截止", "end", "—")]
        for i, (label, key, default) in enumerate(specs):
            wrap.columnconfigure(i, weight=1, uniform="c")
            card = tk.Frame(wrap, bg=C_CARD, highlightbackground=C_BORDER,
                            highlightthickness=1)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else P(8), 0))
            tk.Label(card, text=label, font=UI_FONT_SM, bg=C_CARD,
                     fg=C_MUTED, anchor="w").pack(fill="x", padx=P(12),
                                                  pady=(P(8), 0))
            v = tk.Label(card, text=default, font=BIG_FONT, bg=C_CARD,
                         fg=C_TEXT, anchor="w")
            v.pack(fill="x", padx=P(12), pady=(0, P(9)))
            self.card_values[key] = v

    def _build_actions(self) -> None:
        wrap = tk.Frame(self.root, bg=C_BG)
        wrap.pack(fill="x", padx=P(14), pady=(P(12), 0))
        self.main_buttons = []
        b = flat_button(wrap, "立即签到", lambda: self.start_task(self.task_checkin),
                        primary=True, small=False)
        b.pack(side="left", padx=(0, P(8)))
        self.main_buttons.append(b)
        b = flat_button(wrap, "只查状态", lambda: self.start_task(self.task_status),
                        small=False)
        b.pack(side="left")
        self.main_buttons.append(b)

    def _build_sections(self) -> None:
        holder = tk.Frame(self.root, bg=C_BG)
        holder.pack(fill="x")

        self.sec_auth = Section(self, holder, 1, "账号授权")
        self.sec_auth.add("立即授权", lambda: self.open_auth(push=False),
                          primary=True)
        self.sec_auth.add("推到手机授权", lambda: self.open_auth(push=True))
        self.sec_auth.add("导入授权", self.open_import)

        self.sec_sched = Section(self, holder, 2, "每日定时")
        self.sec_sched.add("设置定时", self.open_schedule, primary=True)

        self.sec_push = Section(self, holder, 3, "消息推送")
        self.sec_push.add("设置推送", self.open_notify, primary=True)
        self.sec_push.add("发送测试", lambda: self.start_task(self.task_test_notify))

    def _build_log(self) -> None:
        wrap = tk.Frame(self.root, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=P(14), pady=(P(10), P(12)))
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
            height=8, wrap="word", padx=P(10), pady=P(8),
            highlightbackground=C_BORDER, highlightthickness=1)
        self.logbox.pack(fill="both", expand=True, pady=(P(6), 0))
        self.logbox.configure(state="disabled")

    # ---------------- 状态刷新 ----------------

    def refresh_all(self) -> None:
        self.refresh_auth()
        self.refresh_push()
        self.refresh_schedule_state()

    def refresh_auth(self) -> None:
        info = wb_oauth.summary(core.oauth_token_path(self.cfg))
        if not info.get("present"):
            self.sec_auth.set("● 未授权", "还没有登录过，先点「立即授权」"
                                         "（或让服务器发链接到手机）。", C_BAD)
            return
        if info.get("error"):
            self.sec_auth.set("● 令牌异常", str(info["error"]), C_BAD)
            return
        if info.get("disabled") or info.get("expired"):
            self.sec_auth.set("● 已失效", "登录会话过期了，重新授权一次即可。",
                              C_BAD)
            return
        who = info.get("nickname") or "已授权账号"
        uid = str(info.get("uid") or "")
        days = info.get("days_left")
        expire = info.get("expires_text") or ""
        detail = "{}{}".format(who, "（uid {}…）".format(uid[:8]) if uid else "")
        if expire:
            detail += "　令牌有效期至 {}（剩 {} 天，到期前自动续期）".format(
                expire, days if days is not None else "?")
        self.sec_auth.set(
            "● 已授权", detail,
            C_OK if (days is None or days > 3) else C_WARN)

    def refresh_push(self) -> None:
        chans = wb_notify.selected_channels(self.cfg)
        picked = [wb_notify.label(c) for c in (self.cfg.get("push_channels") or [])]
        if not picked:
            self.sec_push.set("● 未启用", "不推送签到结果。", C_MUTED)
            return
        if not chans:
            self.sec_push.set(
                "● 配置不完整",
                "已勾选 {}，但 key 没填对，实际不会推送。".format(
                    "、".join(wb_notify.label(c) for c in picked)), C_BAD)
            return
        self.sec_push.set("● " + "＋".join(wb_notify.label(c) for c in chans),
                          "签到结果推送到：{}".format(
                              "、".join(wb_notify.label(c) for c in chans)))

    def refresh_schedule_state(self) -> None:
        """读一次系统计划任务状态（用 query_fast，直读 XML，约 0.5 毫秒）。"""
        if not self.cfg.get("schedule_enabled"):
            self.sched_state = None
            self.sec_sched.set("● 未启用", "每天不会自动签到，只能手动点。", C_MUTED)
            return
        info = wb_task.query_fast()
        prog, _args = wb_task.launcher()
        info["stale"] = bool(
            info.get("exists")
            and os.path.normcase(str(info.get("command") or ""))
            != os.path.normcase(prog))
        self.sched_state = info
        times = "、".join(info.get("times") or []) or "?"
        jit = int(info.get("jitter_minutes") or 0)
        plan = "每天 {}{}".format(times, "（±{} 分）".format(jit) if jit else "")
        if not info.get("exists"):
            self.sec_sched.set("● 未注册", "配置里启用了，但系统里没有这个任务，"
                                          "点「设置定时」→「应用到系统」。", C_BAD)
        elif info["stale"]:
            self.sec_sched.set("● 路径失效", "{}，但任务指向的不是当前程序，"
                                            "重新应用一次即可。".format(plan), C_WARN)
        else:
            self.sec_sched.set("● 已启用", plan + "　由 Windows 计划任务触发，"
                                                    "本程序不用常驻。")

    def apply_config(self, cfg: dict) -> None:
        self.cfg = cfg
        self.refresh_all()

    # ---------------- 线程与消息 ----------------

    def start_task(self, fn, quiet: bool = False) -> None:
        if self.busy:
            self.log("上一个任务还在跑，稍等一下…")
            return
        self.busy = True
        self._set_buttons(False, quiet=quiet)
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

    def _set_buttons(self, enabled: bool, quiet: bool = False) -> None:
        if quiet:
            return
        state = "normal" if enabled else "disabled"
        for b in self.main_buttons:
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
                elif kind == "banner":
                    self.set_banner(*payload)
                elif kind == "cards":
                    self._apply_cards(payload)
                elif kind == "refresh":
                    self.refresh_all()
                elif kind == "busy":
                    self.busy = payload
                    self._set_buttons(not payload)
                elif kind == "call":
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
                text="已签到" if done else "未签到", fg=C_OK if done else C_BAD)
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
            text="v{}　最后更新 {}".format(VERSION, time.strftime("%H:%M:%S")))

    @staticmethod
    def _fmt_end(raw: str) -> str:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return time.strftime("%m/%d %H:%M", time.strptime(raw, fmt))
            except ValueError:
                continue
        return raw

    # ---------------- 具体任务 ----------------

    def task_env_check(self) -> None:
        self.log("开始环境自检…")
        env = core.detect_env(deep=True, cfg=self.cfg)
        for name, val in env.get("steps", []):
            self.log("  {}：{}".format(name, val))
        if env.get("message"):
            self.log("  → {}".format(env["message"]))
        # tkinter 不允许跨线程碰控件，横幅必须挪回主线程刷
        self.post("call", lambda: self._apply_env(env))
        if env.get("status_data"):
            self.post("cards", {"result": "STATUS", **env["status_data"]})
        self.post("refresh")

    def _apply_env(self, env: dict) -> None:
        code = env.get("code")
        tok = env.get("oauth_token") or {}
        if code == "OK":
            who = tok.get("nickname") or "已授权账号"
            days = tok.get("days_left")
            self.set_banner(
                "ok", "环境正常　账号 {}{}　可以直接签到。".format(
                    who, "，令牌剩 {} 天".format(days) if days is not None else ""))
        elif code == "OAUTH_NOT_AUTHORIZED":
            self.set_banner(
                "error", "尚未授权　点右边开始登录（只需一次）。",
                "  立即授权  ", lambda: self.open_auth(push=False))
        elif code in ("OAUTH_SESSION_DEAD", "OAUTH_TOKEN_BAD"):
            self.set_banner(
                "error", "授权已失效　{}　重新授权一次即可恢复。".format(
                    env.get("message", "")),
                "  重新授权  ", lambda: self.open_auth(push=False))
        elif code == "OAUTH_NETWORK":
            self.set_banner(
                "warn", "网络不通　{}　检查代理或防火墙后重试。".format(
                    env.get("message", "")),
                "  重新自检  ", lambda: self.start_task(self.task_env_check))
        else:
            self.set_banner(
                "error", "检测异常：{}　{}".format(code, env.get("message", "")),
                "  重新自检  ", lambda: self.start_task(self.task_env_check))

    def task_status(self) -> None:
        self.log("查询今日签到状态…")
        res = core.run_once(self.cfg, check_only=True,
                            progress=self.log, notify=False)
        self.log("结果：{}".format(json.dumps(res, ensure_ascii=False)))
        self.post("cards", res)
        if res.get("result") in ("STATUS", "ALREADY_CLAIMED", "INACTIVE"):
            self.post("banner", ("ok", "状态查询完成　连续 {} 天，本期累计 {} 积分。"
                                 .format(res.get("streak_days", "?"),
                                         res.get("total_credits", "?")), "", None))
        else:
            self._report_failure(res)

    def task_checkin(self) -> None:
        self.log("开始签到…")
        self.post("banner", ("checking", "正在签到…", "", None))
        res = core.run_once(self.cfg, check_only=False, progress=self.log)
        self.log("结果：{}".format(json.dumps(res, ensure_ascii=False)))
        self.post("cards", res)
        code = res.get("result")
        if code == "CLAIMED":
            amount = res.get("credit", res.get("credits",
                           res.get("today_credit", "?")))
            self.post("banner", ("ok", "✅ 签到成功，+{} 积分　连续 {} 天，"
                                 "本期累计 {} 积分。".format(
                                     amount, res.get("streak_days", "?"),
                                     res.get("total_credits", "?")), "", None))
        elif code == "ALREADY_CLAIMED":
            self.post("banner", ("ok", "今天已经签过了　连续 {} 天，"
                                 "本期累计 {} 积分。".format(
                                     res.get("streak_days", "?"),
                                     res.get("total_credits", "?")), "", None))
        elif code == "INACTIVE":
            self.post("banner", ("warn", "当前没有进行中的签到活动", "", None))
        else:
            self._report_failure(res)

    def _report_failure(self, res: dict) -> None:
        code = res.get("result")
        auth_like = code in ("OAUTH_NOT_LOGGED_IN", "OAUTH_SESSION_DEAD",
                             "OAUTH_TOKEN_BAD", "OAUTH_NO_REFRESH")
        if auth_like:
            self.post("banner", ("error", "签到失败：授权无效　{}".format(
                res.get("message", "")), "  重新授权  ",
                lambda: self.open_auth(push=False)))
        else:
            self.post("banner", ("error", "签到失败：{}　{}".format(
                code, res.get("message", "")), "  重新自检  ",
                lambda: self.start_task(self.task_env_check)))

    def task_test_notify(self) -> None:
        chans = wb_notify.selected_channels(self.cfg)
        if not chans:
            self.post("banner", ("warn", "还没有可用的推送渠道　先在「消息推送」里"
                                 "选渠道并填 key。", "  设置推送  ", self.open_notify))
            return
        self.log("发送测试消息到：{}…".format(
            "、".join(wb_notify.label(c) for c in chans)))
        pushed = wb_notify.push(
            self.cfg, "【WorkBuddy 签到】🔧 测试消息：推送配置正确，链路正常。")
        for name, _ok, detail in pushed:
            self.log("{}：{}".format(name, detail))
        failed = [n for n, ok, _d in pushed if not ok]
        if not failed:
            self.post("banner", ("ok", "测试消息已发送　请查看：{}。".format(
                "、".join(n for n, _o, _d in pushed)), "", None))
        else:
            self.post("banner", ("warn", "推送未成功：{}　检查 key。".format(
                "、".join(failed)), "  设置推送  ", self.open_notify))

    # ---------------- 弹窗 ----------------

    def open_auth(self, push: bool = False) -> None:
        try:
            AuthDialog(self, push=push, on_done=self.refresh_all)
        except Exception:
            self.log("打开授权窗口失败：\n" + traceback.format_exc())

    def open_import(self) -> None:
        try:
            ImportDialog(self, on_done=self.refresh_all)
        except Exception:
            self.log("打开导入窗口失败：\n" + traceback.format_exc())

    def open_schedule(self) -> None:
        try:
            ScheduleDialog(self)
        except Exception:
            self.log("打开定时设置失败：\n" + traceback.format_exc())

    def open_notify(self) -> None:
        try:
            NotifyDialog(self)
        except Exception:
            self.log("打开推送设置失败：\n" + traceback.format_exc())

    def open_log(self) -> None:
        if not wb_config.LOG_PATH.exists():
            try:
                wb_config.LOG_PATH.touch()
            except OSError:
                pass
        try:
            os.startfile(str(wb_config.LOG_PATH))  # noqa
        except OSError as e:
            self.log("打开日志失败：{}".format(e))


# ---------------- 无界面入口 ----------------

def selftest() -> int:
    """无界面自检：把结果写到程序目录的 selftest.json（排查环境问题用）。"""
    enable_dpi_awareness()
    cfg = core.load_config()
    out = {
        "version": VERSION,
        "system_dpi": system_dpi(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "app_dir": str(wb_config.SCRIPT_DIR),
        "config_path": str(wb_config.config_path()),
        "token_file": core.oauth_token_path(cfg),
        "push_channels": wb_notify.selected_channels(cfg),
    }
    try:
        out["env"] = core.detect_env(deep=True, cfg=cfg)
    except Exception:
        out["env_error"] = traceback.format_exc()
    try:
        out["token"] = wb_oauth.summary(core.oauth_token_path(cfg))
    except Exception:
        out["token_error"] = traceback.format_exc()
    try:
        out["schedule"] = wb_task.query_fast()
    except Exception:
        out["schedule_error"] = traceback.format_exc()
    try:
        (wb_config.SCRIPT_DIR / "selftest.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except OSError:
        return 1


# 这些参数表示「无界面执行」，绝不能创建窗口
HEADLESS_FLAGS = ("--run", "--check-only", "--diagnose", "--test-notify",
                  "--auth", "--auth-push", "--import-token", "--pull-token")


def _redirect_output() -> None:
    """无界面模式：既要保证输出不会把进程搞崩，也别吞掉用户要看的输出。

    两个真实存在的坑，都会导致「签到明明成功、退出码却是非 0」，
    从而被计划任务标记成失败：
      1) --windowed 打包后 sys.stdout / sys.stderr 可能是 None，print 直接抛异常；
      2) 即使存在，也可能是 GBK 文本流，中文 / emoji 触发 UnicodeEncodeError。

    所以分两种情况：
      * 流不存在（计划任务拉起 exe）→ 重定向到 UTF-8 的 checkin.log；
      * 流存在（终端里手敲、或 `--json` 接管道）→ 就地改成 UTF-8 + replace，
        保留输出能力。以前无条件重定向，`exe --json | jq` 会一个字都拿不到。
    """
    if sys.stdout is not None and sys.stderr is not None:
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        return
    stream = None
    try:
        stream = open(wb_config.LOG_PATH, "a", encoding="utf-8",
                      errors="replace", buffering=1)
    except OSError:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return
    sys.stdout = stream
    sys.stderr = stream


def run_headless(argv: list) -> int:
    _redirect_output()
    return core.main(argv)


SCHED_FLAG = "--sched-status"


def sched_status() -> int:
    """无界面查看计划任务状态（排查「定时不生效」用，不弹窗口）。"""
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
    except Exception:
        pass
    try:
        with open(wb_config.LOG_PATH, "a", encoding="utf-8",
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
    if any(a in HEADLESS_FLAGS for a in argv):
        return run_headless(argv)

    # 关键顺序：先声明 DPI 感知，再创建 Tk
    enable_dpi_awareness()
    root = tk.Tk()
    apply_tk_scaling(root)
    App(root)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w = min(P(640), int(sw * 0.94))
    h = min(P(760), int(sh * 0.90))
    root.geometry("{}x{}+{}+{}".format(w, h, P(120), max(0, P(40))))
    root.minsize(min(P(600), sw), min(P(560), sh))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
