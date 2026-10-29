# -*- coding: utf-8 -*-
"""
QQ Bot × ComfyUI 控制面板

功能：
1. 填写并保存 AppID / AppSecret
2. 启动 / 停止 QQ Bot 服务，实时查看日志
3. 验证本地 ComfyUI 服务是否已启动（ComfyUI 仍由秋叶启动器自行启动）

运行：双击「启动QQ机器人面板.bat」，或 python qq_bot_panel.py
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "qq_bot_config.json"
BRIDGE_FILE = BASE_DIR / "qq_comfy_bridge.py"
COMFYUI_URL = "http://127.0.0.1:8188"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def ui_python():
    """优先使用整合包自带 Python（已含 aiohttp），否则用当前解释器。"""
    for parent in [BASE_DIR, *list(BASE_DIR.parents)[:4]]:
        bundled = parent / "python" / "python.exe"
        if bundled.is_file():
            return str(bundled)
    return sys.executable


class Panel:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.events = queue.Queue()
        self.comfy_busy = False
        self.auto_check = tk.BooleanVar(value=True)
        self.show_secret = tk.BooleanVar(value=False)

        self.cfg = self.load_config()
        self.http = requests.Session()
        self.http.trust_env = False  # 本地检测不走系统代理
        self.build_ui()
        self.root.after(100, self.drain_events)
        self.root.after(300, lambda: self.check_comfy())

    # ---------------- 配置 ----------------
    def load_config(self):
        cfg = {"appid": "", "appsecret": "", "openids": []}
        if CONFIG_FILE.exists():
            try:
                cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
        return cfg

    def save_config(self):
        cfg = self.load_config()
        cfg["appid"] = self.appid_var.get().strip()
        cfg["appsecret"] = self.secret_var.get().strip()
        cfg.setdefault("openids", [])
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.cfg = cfg
        self.refresh_openids()

    # ---------------- UI ----------------
    def build_ui(self):
        self.root.title("QQ Bot × ComfyUI 控制面板")
        self.root.geometry("960x760")
        self.root.minsize(820, 620)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", 9), foreground="#666666")
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 6))

        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        # 标题 + 状态圆点
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="QQ Bot × ComfyUI", style="Title.TLabel").pack(side="left")
        self.bot_dot = ttk.Label(head, text="●  机器人未启动", foreground="#999999",
                                 font=("Microsoft YaHei UI", 10))
        self.bot_dot.pack(side="right", padx=(12, 0))
        self.comfy_dot = ttk.Label(head, text="●  ComfyUI 检测中", foreground="#999999",
                                   font=("Microsoft YaHei UI", 10))
        self.comfy_dot.pack(side="right")

        ttk.Separator(outer).pack(fill="x", pady=12)

        # 配置区
        conf = ttk.LabelFrame(outer, text=" 机器人配置 ", padding=12)
        conf.pack(fill="x")

        ttk.Label(conf, text="AppID").grid(row=0, column=0, sticky="w", pady=4)
        self.appid_var = tk.StringVar(value=self.cfg.get("appid", ""))
        ttk.Entry(conf, textvariable=self.appid_var, width=52).grid(
            row=0, column=1, columnspan=2, sticky="we", padx=10, pady=4)

        ttk.Label(conf, text="AppSecret").grid(row=1, column=0, sticky="w", pady=4)
        self.secret_var = tk.StringVar(value=self.cfg.get("appsecret", ""))
        self.secret_entry = ttk.Entry(conf, textvariable=self.secret_var, width=52, show="•")
        self.secret_entry.grid(row=1, column=1, sticky="we", padx=10, pady=4)
        ttk.Checkbutton(conf, text="显示", variable=self.show_secret,
                        command=self.toggle_secret).grid(row=1, column=2, sticky="w")

        ttk.Label(conf, text="已授权 OpenID").grid(row=2, column=0, sticky="w", pady=4)
        self.openid_label = ttk.Label(conf, text="（首次对话后自动绑定）", style="Hint.TLabel")
        self.openid_label.grid(row=2, column=1, sticky="w", padx=10, pady=4)

        btns = ttk.Frame(conf)
        btns.grid(row=3, column=1, columnspan=2, sticky="w", padx=10, pady=(8, 0))
        ttk.Button(btns, text="保存配置", command=self.on_save).pack(side="left")
        ttk.Button(btns, text="清空白名单", command=self.on_clear_openids).pack(side="left", padx=8)

        conf.columnconfigure(1, weight=1)
        self.refresh_openids()

        # 控制区
        ctrl = ttk.Frame(outer)
        ctrl.pack(fill="x", pady=(14, 0))
        self.btn_start = ttk.Button(ctrl, text="▶  启动 Bot 服务", style="Big.TButton",
                                    command=self.on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(ctrl, text="■  停止 Bot 服务", style="Big.TButton",
                                   command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=10)
        ttk.Label(ctrl, text="ComfyUI 请用秋叶启动器启动，本面板只检测其服务状态",
                  style="Hint.TLabel").pack(side="right")

        # ComfyUI 状态
        comfy = ttk.LabelFrame(outer, text=" ComfyUI 服务状态 ", padding=12)
        comfy.pack(fill="x", pady=(14, 0))
        row = ttk.Frame(comfy)
        row.pack(fill="x")
        ttk.Button(row, text="检测 ComfyUI", command=self.check_comfy).pack(side="left")
        ttk.Checkbutton(row, text="每 5 秒自动检测", variable=self.auto_check).pack(side="left", padx=10)
        self.comfy_detail = ttk.Label(comfy, text="尚未检测", style="Hint.TLabel",
                                      justify="left", wraplength=880)
        self.comfy_detail.pack(fill="x", pady=(8, 0))

        # 日志
        logbox = ttk.LabelFrame(outer, text=" 运行日志 ", padding=8)
        logbox.pack(fill="both", expand=True, pady=(14, 0))
        bar = ttk.Frame(logbox)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="清空日志", command=self.clear_log).pack(side="right")
        ttk.Label(bar, text=f"Python: {ui_python()}", style="Hint.TLabel").pack(side="left")

        self.log_text = scrolledtext.ScrolledText(
            logbox, height=16, wrap="word", font=("Consolas", 9),
            background="#1e1e1e", foreground="#d6d6d6", insertbackground="#d6d6d6",
            borderwidth=0, padx=8, pady=6)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(5000, self.auto_check_loop)

    def toggle_secret(self):
        self.secret_entry.configure(show="" if self.show_secret.get() else "•")

    def refresh_openids(self):
        openids = self.cfg.get("openids") or []
        if openids:
            text = "、".join(o[:10] + "…" for o in openids)
        else:
            text = "（首次对话后自动绑定）"
        self.openid_label.configure(text=text)

    # ---------------- 日志 ----------------
    def append_log(self, line):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ---------------- 配置操作 ----------------
    def on_save(self):
        if not self.appid_var.get().strip() or not self.secret_var.get().strip():
            messagebox.showwarning("提示", "AppID 和 AppSecret 都需要填写。")
            return
        self.save_config()
        self.append_log("[面板] 配置已保存到 qq_bot_config.json")

    def on_clear_openids(self):
        if not messagebox.askyesno("确认", "清空后，下一个给机器人发消息的人会被自动绑定。继续？"):
            return
        cfg = self.load_config()
        cfg["openids"] = []
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.cfg = cfg
        self.refresh_openids()
        self.append_log("[面板] 已清空 OpenID 白名单")

    # ---------------- Bot 进程 ----------------
    def on_start(self):
        if self.proc and self.proc.poll() is None:
            return
        appid = self.appid_var.get().strip()
        secret = self.secret_var.get().strip()
        if not appid or not secret:
            messagebox.showwarning("提示", "请先填写 AppID 和 AppSecret 并保存。")
            return
        self.save_config()

        if not BRIDGE_FILE.exists():
            messagebox.showerror("错误", f"找不到 {BRIDGE_FILE.name}")
            return

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # QQ 与本地 ComfyUI 都走直连，避免系统代理干扰
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)

        try:
            self.proc = subprocess.Popen(
                [ui_python(), "-u", str(BRIDGE_FILE)],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            messagebox.showerror("启动失败", str(e))
            return

        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.bot_dot.configure(text="●  机器人启动中", foreground="#e6a23c")
        self.append_log("[面板] 正在启动 Bot 服务…")

    def read_output(self, proc):
        try:
            for line in proc.stdout:
                self.events.put(("log", line.rstrip()))
        except Exception as e:
            self.events.put(("log", f"[面板] 读取日志失败：{e}"))
        code = proc.wait()
        self.events.put(("exit", code))

    def on_stop(self):
        if not self.proc or self.proc.poll() is not None:
            return
        self.append_log("[面板] 正在停止 Bot 服务…")
        self.proc.terminate()
        threading.Thread(target=self.wait_then_kill, args=(self.proc,), daemon=True).start()

    def wait_then_kill(self, proc):
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ---------------- ComfyUI 检测 ----------------
    def check_comfy(self):
        if self.comfy_busy:
            return
        self.comfy_busy = True
        threading.Thread(target=self.comfy_worker, daemon=True).start()

    def comfy_worker(self):
        try:
            r = self.http.get(f"{COMFYUI_URL}/system_stats", timeout=3)
            r.raise_for_status()
            stats = r.json()
            devices = stats.get("devices") or []
            dev = devices[0] if devices else {}
            name = dev.get("name", "未知设备")
            vram_total = dev.get("vram_total", 0) / (1024 ** 3) if dev.get("vram_total") else 0
            vram_free = dev.get("vram_free", 0) / (1024 ** 3) if dev.get("vram_free") else 0
            version = (stats.get("system") or {}).get("comfyui_version", "未知")

            running = pending = 0
            try:
                q = self.http.get(f"{COMFYUI_URL}/queue", timeout=3).json()
                running = len(q.get("queue_running", []))
                pending = len(q.get("queue_pending", []))
            except Exception:
                pass

            self.events.put(("comfy", {
                "ok": True,
                "text": f"已启动 · 版本 {version}\n设备：{name}"
                        + (f"（显存 {vram_free:.1f} / {vram_total:.1f} GB 可用）" if vram_total else "")
                        + f"\n队列：运行 {running} / 等待 {pending}",
            }))
        except Exception as e:
            self.events.put(("comfy", {"ok": False, "text": f"未启动或无法连接 127.0.0.1:8188（{type(e).__name__}）"}))
        finally:
            self.comfy_busy = False

    def auto_check_loop(self):
        if self.auto_check.get():
            self.check_comfy()
        self.root.after(5000, self.auto_check_loop)

    # ---------------- 事件泵 ----------------
    def drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "exit":
                    self.append_log(f"[面板] Bot 服务已退出（退出码 {payload}）")
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.bot_dot.configure(text="●  机器人未启动", foreground="#999999")
                    self.proc = None
                elif kind == "comfy":
                    if payload["ok"]:
                        self.comfy_dot.configure(text="●  ComfyUI 在线", foreground="#2e9e4f")
                    else:
                        self.comfy_dot.configure(text="●  ComfyUI 离线", foreground="#d9534f")
                    self.comfy_detail.configure(text=payload["text"])
        except queue.Empty:
            pass
        self.root.after(100, self.drain_events)

    # ---------------- 关闭 ----------------
    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("退出", "Bot 服务仍在运行，是否停止并退出？"):
                return
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.root.destroy()


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    Panel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
