"""Semi-automatic video/image import, YOLO labeling, split and training UI."""

from __future__ import annotations

import random
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import cv2
import numpy as np
from PIL import Image, ImageTk


CLASSES = ("enemy", "enemy_attack", "yellow_flash")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".wmv"}
ROOT = Path(__file__).resolve().parent
WORK = ROOT / "dataset" / "studio"
IMAGES = WORK / "images"
LABELS = WORK / "labels"


class DatasetStudio:
    def __init__(self, parent: tk.Tk) -> None:
        self.parent = parent
        self.window = tk.Toplevel(parent)
        self.window.title("数据集采集与训练")
        self.window.geometry("1080x760")
        self.window.minsize(900, 650)
        self.window.transient(parent)
        self.files: list[Path] = []
        self.index = 0
        self.image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.scale = 1.0
        self.offset = (0, 0)
        self.drag_start: tuple[int, int] | None = None
        self.rect_id: int | None = None
        self._build()
        self._refresh_files()

    def _build(self) -> None:
        root = ttk.Frame(self.window, padding=14)
        root.pack(fill="both", expand=True)
        toolbar = ttk.Frame(root)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="上传视频/图片", command=self._import).pack(side="left")
        ttk.Button(toolbar, text="导入文件夹", command=self._import_folder).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="上一张", command=lambda: self._move(-1)).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="下一张", command=lambda: self._move(1)).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="保存无目标", command=self._save_empty).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="划分训练/验证集", command=self._split).pack(side="right")
        ttk.Button(toolbar, text="开始训练", command=self._train).pack(side="right", padx=(0, 8))

        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, padding=(0, 0, 10, 0))
        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        body.add(left, weight=4)
        body.add(right, weight=1)
        self.canvas = tk.Canvas(left, background="#20252b", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._start_box)
        self.canvas.bind("<B1-Motion>", self._drag_box)
        self.canvas.bind("<ButtonRelease-1>", self._finish_box)
        self.canvas.bind("<Configure>", lambda _event: self._show_current())

        ttk.Label(right, text="标注类别").pack(anchor="w")
        self.class_var = tk.StringVar(value=CLASSES[0])
        ttk.Combobox(right, textvariable=self.class_var, values=CLASSES, state="readonly").pack(fill="x", pady=(5, 14))
        self.file_label = ttk.Label(right, text="尚未导入文件", wraplength=220)
        self.file_label.pack(anchor="w", pady=(0, 10))
        ttk.Label(right, text="操作：在图像中拖拽目标框，松开鼠标后点击保存标注。攻击帧只标 enemy_attack，不要重复标 enemy。", wraplength=220).pack(anchor="w")
        ttk.Button(right, text="保存当前标注", command=self._save_box).pack(fill="x", pady=(18, 6))
        self.status = ttk.Label(right, text="", wraplength=220)
        self.status.pack(anchor="w")
        self.log = tk.Text(right, height=12, width=30, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(18, 0))

    def _write_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_files(self) -> None:
        IMAGES.mkdir(parents=True, exist_ok=True)
        LABELS.mkdir(parents=True, exist_ok=True)
        self.files = sorted(IMAGES.glob("*.jpg")) + sorted(IMAGES.glob("*.png"))
        self.index = min(self.index, max(0, len(self.files) - 1))
        self._show_current()

    def _import(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        paths = filedialog.askopenfilenames(
            parent=self.window,
            title="选择游戏截图或视频",
            filetypes=[
                ("支持的媒体", "*.png *.jpg *.jpeg *.bmp *.mp4 *.avi *.mov *.mkv *.wmv"),
                ("图片", "*.png *.jpg *.jpeg *.bmp"),
                ("视频", "*.mp4 *.avi *.mov *.mkv *.wmv"),
                ("所有文件", "*.*"),
            ],
        )
        if not paths:
            return
        self._start_import([Path(path) for path in paths])

    def _import_folder(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        directory = filedialog.askdirectory(parent=self.window, title="选择包含游戏截图或视频的文件夹")
        if not directory:
            return
        paths = [
            path for path in Path(directory).iterdir()
            if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES | VIDEO_SUFFIXES
        ]
        if not paths:
            messagebox.showwarning("未找到媒体", "该文件夹内没有支持的图片或视频文件。", parent=self.window)
            return
        self._start_import(paths)

    def _start_import(self, paths: list[Path]) -> None:
        self._write_log(f"开始导入 {len(paths)} 个文件，请等待。")
        self.status.configure(text="正在导入媒体...")
        def worker() -> None:
            added, failures = self._import_paths(paths)
            self.window.after(0, self._finish_import, added, failures)
        threading.Thread(target=worker, name="dataset-import", daemon=True).start()

    def _target_path(self, prefix: str) -> Path:
        return IMAGES / f"{prefix}_{uuid.uuid4().hex[:12]}.jpg"

    @staticmethod
    def _read_image(source: Path) -> np.ndarray | None:
        """Read image bytes first so paths containing Chinese work on Windows."""
        try:
            return cv2.imdecode(np.fromfile(source, dtype=np.uint8), cv2.IMREAD_COLOR)
        except (OSError, ValueError):
            return None

    def _import_paths(self, paths: list[Path]) -> tuple[int, list[str]]:
        added = 0
        failures: list[str] = []
        for source in paths:
            suffix = source.suffix.casefold()
            if suffix in IMAGE_SUFFIXES:
                image = self._read_image(source)
                target = self._target_path(f"image_{source.stem}")
                if image is None or not cv2.imwrite(str(target), image):
                    failures.append(f"图片无法读取：{source.name}")
                else:
                    added += 1
            elif suffix in VIDEO_SUFFIXES:
                count, error = self._extract_video(source)
                added += count
                if error:
                    failures.append(error)
            else:
                failures.append(f"不支持的文件类型：{source.name}")
        return added, failures

    def _extract_video(self, source: Path) -> tuple[int, str | None]:
        capture = cv2.VideoCapture(str(source))
        temporary_copy: Path | None = None
        if not capture.isOpened():
            # Some OpenCV backends cannot open a non-ASCII Windows pathname.
            # An internal ASCII copy keeps imported user videos usable.
            cache = WORK / ".video-cache"
            cache.mkdir(parents=True, exist_ok=True)
            temporary_copy = cache / f"source_{uuid.uuid4().hex}{source.suffix.lower()}"
            try:
                shutil.copy2(source, temporary_copy)
                capture = cv2.VideoCapture(str(temporary_copy))
            except OSError:
                pass
        if not capture.isOpened():
            if temporary_copy is not None:
                temporary_copy.unlink(missing_ok=True)
            return 0, f"视频无法打开：{source.name}"
        try:
            fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
            step = max(1, round(fps / 8.0))
            frame_no = 0
            added = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                if frame_no % step == 0:
                    if cv2.imwrite(str(self._target_path(f"video_{source.stem}_{frame_no:07d}")), frame):
                        added += 1
                frame_no += 1
            if frame_no == 0:
                return 0, f"视频没有可读取的画面：{source.name}"
            return added, None
        finally:
            capture.release()
            if temporary_copy is not None:
                temporary_copy.unlink(missing_ok=True)

    def _finish_import(self, added: int, failures: list[str]) -> None:
        self._refresh_files()
        self.status.configure(text=f"已导入 {added} 张图片")
        self._write_log(f"导入完成：{added} 张图片。")
        for failure in failures:
            self._write_log(failure)
        if added == 0:
            messagebox.showerror("导入失败", "没有成功导入任何图片。请查看窗口右侧日志中的具体原因。", parent=self.window)

    def _show_current(self) -> None:
        self.canvas.delete("all")
        if not self.files:
            self.file_label.configure(text="尚未导入文件")
            return
        path = self.files[self.index]
        self.image = Image.open(path).convert("RGB")
        width = max(100, self.canvas.winfo_width())
        height = max(100, self.canvas.winfo_height())
        self.scale = min(width / self.image.width, height / self.image.height, 1.0)
        display = self.image.resize((round(self.image.width * self.scale), round(self.image.height * self.scale)))
        self.photo = ImageTk.PhotoImage(display)
        self.offset = ((width - display.width) // 2, (height - display.height) // 2)
        self.canvas.create_image(*self.offset, image=self.photo, anchor="nw")
        self.file_label.configure(text=f"{self.index + 1}/{len(self.files)}\n{path.name}")
        self.status.configure(text="已标注" if (LABELS / f"{path.stem}.txt").exists() else "未标注")

    def _move(self, delta: int) -> None:
        if self.files:
            self.index = max(0, min(len(self.files) - 1, self.index + delta))
            self._show_current()

    def _start_box(self, event: tk.Event) -> None:
        self.drag_start = (event.x, event.y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#FDE047", width=3)

    def _drag_box(self, event: tk.Event) -> None:
        if self.drag_start and self.rect_id:
            self.canvas.coords(self.rect_id, *self.drag_start, event.x, event.y)

    def _finish_box(self, event: tk.Event) -> None:
        if self.drag_start and self.rect_id:
            self.canvas.coords(self.rect_id, *self.drag_start, event.x, event.y)

    def _label_path(self) -> Path:
        return LABELS / f"{self.files[self.index].stem}.txt"

    def _save_box(self) -> None:
        if not self.files or not self.drag_start or not self.rect_id or not self.image:
            messagebox.showwarning("缺少标注", "请先导入图片并拖拽目标框。", parent=self.window)
            return
        x1, y1, x2, y2 = self.canvas.coords(self.rect_id)
        ox, oy = self.offset
        left, right = sorted(((x1 - ox) / self.scale, (x2 - ox) / self.scale))
        top, bottom = sorted(((y1 - oy) / self.scale, (y2 - oy) / self.scale))
        left, right = max(0, left), min(self.image.width, right)
        top, bottom = max(0, top), min(self.image.height, bottom)
        if right - left < 3 or bottom - top < 3:
            messagebox.showwarning("区域过小", "目标框太小，请重新拖拽。", parent=self.window)
            return
        class_id = CLASSES.index(self.class_var.get())
        cx = ((left + right) / 2) / self.image.width
        cy = ((top + bottom) / 2) / self.image.height
        w = (right - left) / self.image.width
        h = (bottom - top) / self.image.height
        self._label_path().write_text(f"{class_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n", encoding="utf-8")
        self.status.configure(text="已标注")
        self._write_log(f"已保存 {self.files[self.index].name}: {self.class_var.get()}")

    def _save_empty(self) -> None:
        if self.files:
            self._label_path().write_text("", encoding="utf-8")
            self.status.configure(text="已保存为无目标")
            self._write_log(f"已保存无目标: {self.files[self.index].name}")

    def _split(self) -> bool:
        labeled = [p for p in self.files if (LABELS / f"{p.stem}.txt").exists()]
        if len(labeled) < 2:
            messagebox.showwarning("样本不足", "至少需要 2 张已标注图片。", parent=self.window)
            return False
        random.Random(42).shuffle(labeled)
        split = max(1, round(len(labeled) * 0.2))
        dataset = ROOT / "dataset"
        for subset in ("train", "val"):
            (dataset / "images" / subset).mkdir(parents=True, exist_ok=True)
            (dataset / "labels" / subset).mkdir(parents=True, exist_ok=True)
        for subset, items in (("val", labeled[:split]), ("train", labeled[split:])):
            for image in items:
                shutil.copy2(image, dataset / "images" / subset / image.name)
                shutil.copy2(LABELS / f"{image.stem}.txt", dataset / "labels" / subset / f"{image.stem}.txt")
        yaml = dataset / "dataset.yaml"
        yaml.write_text(
            f"path: {dataset.as_posix()}\ntrain: images/train\nval: images/val\n\nnames:\n  0: enemy\n  1: enemy_attack\n  2: yellow_flash\n",
            encoding="utf-8",
        )
        self._write_log(f"已划分 {len(labeled) - split} 张训练集、{split} 张验证集。")
        return True

    def _train(self) -> None:
        if not self._split():
            return
        yaml = ROOT / "dataset" / "dataset.yaml"
        if not yaml.exists():
            return
        command = [str(ROOT / ".venv" / "Scripts" / "python.exe"), str(ROOT / "train_yolo.py"), "--data", str(yaml), "--device", "0"]
        self._write_log("开始训练，日志将在此窗口追加。")
        def worker() -> None:
            try:
                process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                assert process.stdout is not None
                for line in process.stdout:
                    self.window.after(0, self._write_log, line.rstrip())
                code = process.wait()
                self.window.after(0, self._write_log, f"训练结束，退出码 {code}。")
                best = ROOT / "runs" / "yysls" / "train" / "weights" / "best.pt"
                if code == 0 and best.exists():
                    model_dir = ROOT / "models"
                    model_dir.mkdir(parents=True, exist_ok=True)
                    target = model_dir / "yolov8s_yysls.pt"
                    shutil.copy2(best, target)
                    self.window.after(0, self._write_log, f"模型已复制到 {target}")
            except Exception as error:
                self.window.after(0, self._write_log, f"训练失败: {error}")
        threading.Thread(target=worker, daemon=True).start()
