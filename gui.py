"""Tkinter control panel and screen-region selector for the automation helper."""

from __future__ import annotations

import json
import logging
import queue
import threading
import traceback
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import cv2
import mss
import numpy as np
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import messagebox, ttk

from main import CONFIG_PATH, Config, Region, run, target_process_is_running
from dataset_studio import DatasetStudio


BG = "#F4F6F8"
PANEL = "#FFFFFF"
INK = "#17212B"
MUTED = "#627281"
ACCENT = "#0B7285"
ACCENT_HOVER = "#075866"
SUCCESS = "#1B7F5A"
WARNING = "#B45309"
DANGER = "#B42318"
BORDER = "#D9E1E8"


class RegionSelector:
    """Full-screen screenshot overlay that returns a physical-pixel rectangle."""

    def __init__(self, parent: tk.Tk, on_selected: callable) -> None:
        self.parent = parent
        self.on_selected = on_selected
        self.start_x = 0
        self.start_y = 0
        self.rect_id: int | None = None

        with mss.MSS() as screen:
            self.monitor = screen.monitors[0]
            raw = np.asarray(screen.grab(self.monitor), dtype=np.uint8)
        rgb = cv2.cvtColor(raw, cv2.COLOR_BGRA2RGB)
        self.height, self.width = rgb.shape[:2]
        self.photo = ImageTk.PhotoImage(Image.fromarray(rgb))

        self.window = tk.Toplevel(parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.geometry(f"{self.width}x{self.height}{self.monitor['left']:+d}{self.monitor['top']:+d}")
        self.canvas = tk.Canvas(self.window, width=self.width, height=self.height, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.create_rectangle(12, 12, 510, 44, fill="#17212B", outline="")
        self.canvas.create_text(24, 28, anchor="w", fill="white", font=("Microsoft YaHei UI", 11),
                                text="拖拽框选屏幕区域，Enter 确认，Esc 取消")
        self.canvas.bind("<ButtonPress-1>", self._start)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._finish)
        self.window.bind("<Escape>", lambda _event: self._cancel())
        self.window.bind("<Return>", lambda _event: self._confirm())
        self.window.focus_force()

    def _start(self, event: tk.Event) -> None:
        self.start_x, self.start_y = event.x, event.y
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#FDE047", width=3)

    def _drag(self, event: tk.Event) -> None:
        if self.rect_id is not None:
            self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def _finish(self, _event: tk.Event) -> None:
        self._confirm()

    def _confirm(self) -> None:
        if self.rect_id is None:
            return
        x1, y1, x2, y2 = (round(value) for value in self.canvas.coords(self.rect_id))
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        if right - left < 10 or bottom - top < 10:
            messagebox.showwarning("区域过小", "请框选至少 10 x 10 像素的区域。", parent=self.window)
            return
        self.window.destroy()
        self.parent.deiconify()
        self.parent.lift()
        self.on_selected(Region(
            left=self.monitor["left"] + left,
            top=self.monitor["top"] + top,
            width=right - left,
            height=bottom - top,
        ))

    def _cancel(self) -> None:
        self.window.destroy()
        self.parent.deiconify()
        self.parent.lift()


class AutomationApp:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = tk.Tk()
        self.root.title("燕云十六声 - 自动按键")
        self.root.configure(bg=BG)
        self.root.geometry("780x570")
        self.root.minsize(720, 520)
        self.root.protocol("WM_DELETE_WINDOW", self._close_application)
        self.root.report_callback_exception = self._report_gui_exception
        self.status_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.current_view = "home"
        self._build_style()
        self._build_shell()
        self._show_home()
        self._poll_status()

    def _report_gui_exception(self, exc_type: type[BaseException], error: BaseException, trace: Any) -> None:
        details = "".join(traceback.format_exception(exc_type, error, trace))
        logging.error("GUI callback failed:\n%s", details)
        messagebox.showerror("界面错误", f"操作未完成：{error}\n\n详细信息已写入 automation.log。", parent=self.root)

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Title.TLabel", background=BG, foreground=INK, font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("Subtle.TLabel", background=BG, foreground=MUTED, font=("Microsoft YaHei UI", 10))
        style.configure("PanelTitle.TLabel", background=PANEL, foreground=INK, font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("PanelText.TLabel", background=PANEL, foreground=MUTED, font=("Microsoft YaHei UI", 10))
        style.configure("Field.TLabel", background=PANEL, foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("TEntry", fieldbackground="#FFFFFF", bordercolor=BORDER, padding=7)
        style.configure("Primary.TButton", background=ACCENT, foreground="#FFFFFF", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 10), borderwidth=0)
        style.map("Primary.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#9AA8B3")])
        style.configure("Secondary.TButton", background="#E7EEF2", foreground=INK, font=("Microsoft YaHei UI", 10), padding=(14, 9), borderwidth=0)
        style.map("Secondary.TButton", background=[("active", "#D7E1E7"), ("disabled", "#EEF2F4")])
        style.configure("Danger.TButton", background=DANGER, foreground="#FFFFFF", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 10), borderwidth=0)
        style.map("Danger.TButton", background=[("active", "#8F1D16"), ("disabled", "#D5A5A1")])
        style.configure("TCheckbutton", background=PANEL, foreground=INK, font=("Microsoft YaHei UI", 10))

    def _build_shell(self) -> None:
        self.container = ttk.Frame(self.root, style="App.TFrame", padding=(30, 26))
        self.container.pack(fill="both", expand=True)
        self.header = ttk.Frame(self.container, style="App.TFrame")
        self.header.pack(fill="x", pady=(0, 22))
        ttk.Label(self.header, text="燕云十六声", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.header, text="屏幕识别与按键控制面板", style="Subtle.TLabel").pack(anchor="w", pady=(4, 0))
        self.content = ttk.Frame(self.container, style="App.TFrame")
        self.content.pack(fill="both", expand=True)

    def _clear_content(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()

    def _panel(self, parent: ttk.Frame, padding: int = 22) -> ttk.Frame:
        return ttk.Frame(parent, style="Panel.TFrame", padding=padding)

    def _show_home(self) -> None:
        self.current_view = "home"
        self._clear_content()
        state_panel = self._panel(self.content)
        state_panel.pack(fill="x")
        ttk.Label(state_panel, text="运行状态", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.process_status = ttk.Label(state_panel, style="PanelText.TLabel")
        self.process_status.grid(row=1, column=0, sticky="w", pady=(16, 3))
        self.script_status = ttk.Label(state_panel, style="PanelText.TLabel")
        self.script_status.grid(row=2, column=0, sticky="w", pady=3)
        self.action_status = ttk.Label(state_panel, style="PanelText.TLabel", wraplength=650)
        self.action_status.grid(row=3, column=0, sticky="w", pady=(3, 0))
        state_panel.columnconfigure(0, weight=1)

        action_panel = self._panel(self.content)
        action_panel.pack(fill="x", pady=(14, 0))
        ttk.Label(action_panel, text="脚本控制", style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(action_panel, text="启动后控制面板会最小化并在后台待命。只有 yysls.exe 处于前台时才截图、识别和派发按键。", style="PanelText.TLabel").pack(anchor="w", pady=(5, 16))
        button_row = ttk.Frame(action_panel, style="Panel.TFrame")
        button_row.pack(fill="x")
        self.start_button = ttk.Button(button_row, text="启动脚本", style="Primary.TButton", command=self._start_automation)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(button_row, text="关闭脚本", style="Danger.TButton", command=self._stop_automation)
        self.stop_button.pack(side="left", padx=(10, 0))
        ttk.Button(button_row, text="配置脚本", style="Secondary.TButton", command=self._show_settings).pack(side="right")
        ttk.Button(button_row, text="数据集训练", style="Secondary.TButton", command=self._open_dataset_studio).pack(side="right", padx=(0, 10))

        note_panel = self._panel(self.content, 18)
        note_panel.pack(fill="x", pady=(14, 0))
        self.mode_note = ttk.Label(note_panel, style="PanelText.TLabel", wraplength=650)
        self.mode_note.pack(anchor="w")
        self._refresh_home()

    def _show_settings(self) -> None:
        if self._is_running():
            messagebox.showinfo("脚本运行中", "请先关闭脚本，再修改配置。", parent=self.root)
            return
        self.current_view = "settings"
        self._clear_content()
        panel = self._panel(self.content)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="配置脚本", style="PanelTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Button(panel, text="返回控制面板", style="Secondary.TButton", command=self._show_home).grid(row=0, column=4, sticky="e")
        ttk.Label(panel, text="可直接输入物理屏幕像素，或点击“截屏框选”后拖拽选区。", style="PanelText.TLabel").grid(row=1, column=0, columnspan=5, sticky="w", pady=(5, 20))

        self.region_vars: dict[str, dict[str, tk.StringVar]] = {}
        self._region_editor(panel, 2, "capture_region", "黄光会出现的范围")
        self._region_editor(panel, 5, "attack_region", "单个敌人模型")

        ttk.Separator(panel).grid(row=8, column=0, columnspan=5, sticky="ew", pady=(16, 14))
        self.output_var = tk.BooleanVar(value=not self.config.observe_only)
        self.debug_var = tk.BooleanVar(value=self.config.debug_window)
        ttk.Checkbutton(panel, text="启用按键输出（检测到提示时立即发送 Shift 或 E）", variable=self.output_var).grid(row=9, column=0, columnspan=5, sticky="w", pady=3)
        ttk.Checkbutton(panel, text="显示 OpenCV 调试窗口", variable=self.debug_var).grid(row=10, column=0, columnspan=5, sticky="w", pady=3)

        vision_frame = ttk.Frame(panel, style="Panel.TFrame")
        vision_frame.grid(row=11, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        ttk.Label(vision_frame, text="识别后端", style="Field.TLabel").pack(side="left")
        self.backend_var = tk.StringVar(value=self.config.vision_backend)
        ttk.Combobox(vision_frame, textvariable=self.backend_var, state="readonly",
                     values=("color_motion", "yolo_hybrid"), width=16).pack(side="left", padx=(12, 8))
        ttk.Label(vision_frame, text="YOLO 模式需要已训练权重；默认模型路径：", style="PanelText.TLabel").pack(side="left")
        self.model_path_var = tk.StringVar(value=self.config.yolo_model_path)
        ttk.Entry(vision_frame, textvariable=self.model_path_var, width=28).pack(side="left", padx=(8, 0))

        hold_frame = ttk.Frame(panel, style="Panel.TFrame")
        hold_frame.grid(row=12, column=0, columnspan=5, sticky="w", pady=(12, 0))
        ttk.Label(hold_frame, text="按键保持时间（毫秒）", style="Field.TLabel").pack(side="left")
        self.key_hold_var = tk.StringVar(value=str(self.config.key_hold_ms))
        ttk.Entry(hold_frame, textvariable=self.key_hold_var, width=10).pack(side="left", padx=(12, 8))
        ttk.Label(hold_frame, text="按下后保持该时长，再自动松开；Shift 和 E 共用此值。建议 80-180ms。", style="PanelText.TLabel").pack(side="left")

        controls = ttk.Frame(panel, style="Panel.TFrame")
        controls.grid(row=13, column=0, columnspan=5, sticky="ew", pady=(22, 0))
        ttk.Button(controls, text="保存配置", style="Primary.TButton", command=self._save_settings).pack(side="left")
        ttk.Button(controls, text="返回", style="Secondary.TButton", command=self._show_home).pack(side="right")
        for column in range(5):
            panel.columnconfigure(column, weight=1 if column in (1, 2, 3, 4) else 0)

    def _open_dataset_studio(self) -> None:
        if self._is_running():
            messagebox.showinfo("脚本运行中", "请先关闭脚本，再进行数据集采集和训练。", parent=self.root)
            return
        DatasetStudio(self.root)

    def _region_editor(self, parent: ttk.Frame, row: int, name: str, label: str) -> None:
        region: Region = getattr(self.config, name)
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=(0, 8))
        ttk.Button(parent, text="截屏框选", style="Secondary.TButton", command=lambda: self._select_region(name)).grid(row=row, column=4, sticky="e", pady=(0, 8))
        variables = {field: tk.StringVar(value=str(getattr(region, field))) for field in ("left", "top", "width", "height")}
        self.region_vars[name] = variables
        field_labels = {"left": "左", "top": "上", "width": "宽", "height": "高"}
        for index, field in enumerate(("left", "top", "width", "height"), start=1):
            field_frame = ttk.Frame(parent, style="Panel.TFrame")
            field_frame.grid(row=row + 1, column=index, sticky="ew", padx=(0 if index == 1 else 8, 0), pady=(0, 16))
            ttk.Label(field_frame, text=field_labels[field], style="PanelText.TLabel").pack(anchor="w")
            ttk.Entry(field_frame, textvariable=variables[field], width=10).pack(fill="x", pady=(4, 0))

    def _select_region(self, name: str) -> None:
        self.root.withdraw()

        def selected(region: Region) -> None:
            for field in ("left", "top", "width", "height"):
                self.region_vars[name][field].set(str(getattr(region, field)))

        self.root.after(180, lambda: RegionSelector(self.root, selected))

    def _config_from_form(self) -> Config:
        def make_region(name: str) -> Region:
            try:
                return Region(**{field: int(variable.get().strip()) for field, variable in self.region_vars[name].items()})
            except ValueError as error:
                raise ValueError(f"{name} 的坐标必须是整数") from error

        updated = replace(
            self.config,
            capture_region=make_region("capture_region"),
            attack_region=make_region("attack_region"),
            observe_only=not self.output_var.get(),
            debug_window=self.debug_var.get(),
            vision_backend=self.backend_var.get().strip(),
            yolo_model_path=self.model_path_var.get().strip(),
        )
        try:
            updated = replace(updated, key_hold_ms=int(self.key_hold_var.get().strip()))
        except ValueError as error:
            raise ValueError("按键保持时间必须是 10-2000 之间的整数毫秒") from error
        updated.validate()
        return updated

    def _save_settings(self) -> bool:
        try:
            self.config = self._config_from_form()
            payload = asdict(self.config)
            CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            messagebox.showinfo("已保存", "配置已写入 config.json。", parent=self.root)
            return True
        except (OSError, ValueError) as error:
            messagebox.showerror("无法保存配置", str(error), parent=self.root)
            return False

    def _start_automation(self) -> None:
        if self._is_running():
            return
        self.stop_event = threading.Event()
        config = self.config
        self.worker = threading.Thread(target=self._worker_entry, args=(config, self.stop_event), name="automation-loop", daemon=True)
        self.worker.start()
        self._refresh_home()
        self.root.iconify()

    def _worker_entry(self, config: Config, stop_event: threading.Event) -> None:
        try:
            run(config, stop_event, self.status_queue.put)
        except Exception as error:
            self.status_queue.put({"event": "error", "message": str(error)})
        finally:
            self.status_queue.put({"event": "stopped"})

    def _stop_automation(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        self._refresh_home()

    def _is_running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _refresh_home(self, data: dict[str, Any] | None = None) -> None:
        if self.current_view != "home":
            return
        process_ok = target_process_is_running(self.config.target_process_name)
        self.process_status.configure(
            text=f"游戏进程：{'已检测到 yysls.exe' if process_ok else '未检测到 yysls.exe'}",
            foreground=SUCCESS if process_ok else WARNING,
        )
        running = self._is_running()
        self.script_status.configure(
            text=f"脚本状态：{'后台待命' if running else '已关闭'}",
            foreground=SUCCESS if running else MUTED,
        )
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        if data:
            action = data.get("last_action", "等待检测")
            foreground = "游戏前台，正在识别" if data.get("foreground_ok") else "后台待命，等待游戏窗口置前"
            self.action_status.configure(text=f"识别：黄光 {data.get('yellow_ratio', 0):.2%} | 动作 {data.get('attack_score', 0):.2%} | {foreground} | {action}")
        elif not running:
            self.action_status.configure(text="识别：未运行")
        self.mode_note.configure(text=(
            "当前为观察模式：不会发送 Shift 或 E。请在配置脚本中启用按键输出。" if self.config.observe_only
            else f"按键输出已启用：每次按键保持 {self.config.key_hold_ms}ms 后松开；黄色检测优先于动作检测；按 F8 可随时紧急停止。"
        ))

    def _poll_status(self) -> None:
        latest: dict[str, Any] | None = None
        try:
            while True:
                event = self.status_queue.get_nowait()
                if event.get("event") == "error":
                    messagebox.showerror("脚本异常", event["message"], parent=self.root)
                elif event.get("event") == "stopped":
                    latest = None
                else:
                    latest = event
        except queue.Empty:
            pass
        self._refresh_home(latest)
        self.root.after(250, self._poll_status)

    def _close_application(self) -> None:
        if self._is_running():
            self._stop_automation()
            self.root.after(150, self._wait_for_worker_then_close)
            return
        self.root.destroy()

    def _wait_for_worker_then_close(self) -> None:
        if self._is_running():
            self.root.after(150, self._wait_for_worker_then_close)
        else:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
