"""Screen-based key helper for Yan Yun Shi Liu Sheng on Windows.

This program reads pixels from user-configured screen regions and sends regular
keyboard events only when explicitly enabled in config.json. It does not access
or modify the game's process or files.
"""

from __future__ import annotations

import ctypes
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable

import cv2
import mss
import numpy as np
import psutil
from pynput import keyboard

try:
    import win32gui
    import win32process
except ImportError:  # Allows detector-only use if the optional foreground guard is unavailable.
    win32gui = None
    win32process = None


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.json"
LOG_PATH = ROOT / "automation.log"


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int

    def as_mss(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}

    def union(self, other: "Region") -> "Region":
        right = max(self.left + self.width, other.left + other.width)
        bottom = max(self.top + self.height, other.top + other.height)
        left = min(self.left, other.left)
        top = min(self.top, other.top)
        return Region(left, top, right - left, bottom - top)

    def validate(self, name: str) -> None:
        if self.left < 0 or self.top < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError(f"{name} must have non-negative left/top and positive width/height")


@dataclass(frozen=True)
class Config:
    debug_window: bool
    observe_only: bool
    target_process_name: str
    capture_region: Region
    attack_region: Region
    yellow_hsv_lower: tuple[int, int, int]
    yellow_hsv_upper: tuple[int, int, int]
    trigger_ratio: float
    yellow_min_pixels: int
    yellow_stable_frames: int
    morph_kernel_size: int
    attack_score_threshold: float
    attack_stable_frames: int
    motion_pixel_threshold: int
    shift_cooldown_ms: int
    e_cooldown_ms: int
    key_hold_ms: int
    fps_limit: int
    emergency_stop_key: str
    vision_backend: str
    yolo_model_path: str
    yolo_confidence: float
    yolo_iou: float
    yolo_imgsz: int
    yolo_device: str
    yolo_enemy_class: str
    yolo_attack_class: str
    yolo_yellow_class: str

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise FileNotFoundError(
                f"Missing {path.name}. Copy {EXAMPLE_CONFIG_PATH.name} to {path.name} and calibrate it."
            )
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        raw["capture_region"] = Region(**raw["capture_region"])
        raw["attack_region"] = Region(**raw["attack_region"])
        raw["yellow_hsv_lower"] = tuple(raw["yellow_hsv_lower"])
        raw["yellow_hsv_upper"] = tuple(raw["yellow_hsv_upper"])
        valid_names = {item.name for item in fields(cls)}
        unknown = set(raw) - valid_names
        if unknown:
            raise ValueError(f"Unknown config fields: {', '.join(sorted(unknown))}")
        config = cls(**raw)
        config.validate()
        return config

    def validate(self) -> None:
        self.capture_region.validate("capture_region")
        self.attack_region.validate("attack_region")
        if not 0 <= self.trigger_ratio <= 1:
            raise ValueError("trigger_ratio must be between 0 and 1")
        if not 0 <= self.attack_score_threshold <= 1:
            raise ValueError("attack_score_threshold must be between 0 and 1")
        if self.vision_backend not in {"color_motion", "yolo_hybrid"}:
            raise ValueError("vision_backend must be color_motion or yolo_hybrid")
        if not 0 <= self.yolo_confidence <= 1 or not 0 <= self.yolo_iou <= 1:
            raise ValueError("yolo_confidence and yolo_iou must be between 0 and 1")
        if self.yolo_imgsz < 160:
            raise ValueError("yolo_imgsz must be at least 160")
        if not self.yolo_enemy_class.strip() or not self.yolo_attack_class.strip():
            raise ValueError("YOLO class names cannot be empty")
        if any(not 0 <= value <= 255 for value in (*self.yellow_hsv_lower, *self.yellow_hsv_upper)):
            raise ValueError("HSV values must be between 0 and 255")
        if self.yellow_hsv_lower[0] > 179 or self.yellow_hsv_upper[0] > 179:
            raise ValueError("OpenCV HSV hue must be between 0 and 179")
        if self.morph_kernel_size < 1 or self.morph_kernel_size % 2 == 0:
            raise ValueError("morph_kernel_size must be a positive odd number")
        if not 10 <= self.key_hold_ms <= 2000:
            raise ValueError("key_hold_ms must be between 10 and 2000 milliseconds")
        positive_names = (
            "yellow_min_pixels", "yellow_stable_frames", "attack_stable_frames",
            "motion_pixel_threshold", "shift_cooldown_ms", "e_cooldown_ms",
            "key_hold_ms", "fps_limit",
        )
        for name in positive_names:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not self.target_process_name.strip().lower().endswith(".exe"):
            raise ValueError("target_process_name must be an executable name, such as yysls.exe")


@dataclass(frozen=True)
class YellowResult:
    mask: np.ndarray
    pixels: int
    ratio: float
    active: bool
    stable_frames: int


@dataclass(frozen=True)
class AttackResult:
    score: float
    active: bool
    stable_frames: int
    diff_mask: np.ndarray


class YellowDetector:
    def __init__(self, config: Config) -> None:
        self.lower = np.array(config.yellow_hsv_lower, dtype=np.uint8)
        self.upper = np.array(config.yellow_hsv_upper, dtype=np.uint8)
        self.trigger_ratio = config.trigger_ratio
        self.min_pixels = config.yellow_min_pixels
        self.required_frames = config.yellow_stable_frames
        self.kernel = np.ones((config.morph_kernel_size, config.morph_kernel_size), np.uint8)
        self._stable_frames = 0

    def detect(self, bgr_frame: np.ndarray) -> YellowResult:
        hsv = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.lower, self.upper)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.kernel)
        pixels = int(cv2.countNonZero(mask))
        ratio = pixels / mask.size
        candidate = ratio >= self.trigger_ratio and pixels >= self.min_pixels
        self._stable_frames = self._stable_frames + 1 if candidate else 0
        return YellowResult(mask, pixels, ratio, self._stable_frames >= self.required_frames, self._stable_frames)


class MotionAttackDetector:
    """Prototype only: temporal motion energy, not a trained attack recognizer."""

    def __init__(self, config: Config) -> None:
        self.pixel_threshold = config.motion_pixel_threshold
        self.score_threshold = config.attack_score_threshold
        self.required_frames = config.attack_stable_frames
        self._previous_gray: np.ndarray | None = None
        self._stable_frames = 0

    def update(self, bgr_frame: np.ndarray) -> AttackResult:
        gray = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        if self._previous_gray is None:
            self._previous_gray = gray
            return AttackResult(0.0, False, 0, np.zeros_like(gray))

        diff = cv2.absdiff(gray, self._previous_gray)
        self._previous_gray = gray
        _, diff_mask = cv2.threshold(diff, self.pixel_threshold, 255, cv2.THRESH_BINARY)
        diff_mask = cv2.morphologyEx(diff_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        score = cv2.countNonZero(diff_mask) / diff_mask.size
        candidate = score >= self.score_threshold
        self._stable_frames = self._stable_frames + 1 if candidate else 0
        return AttackResult(score, self._stable_frames >= self.required_frames, self._stable_frames, diff_mask)


class YoloAttackDetector:
    """YOLOv8 detector with temporal confirmation for attack poses.

    The trained model should contain at least ``enemy`` and ``enemy_attack``
    classes.  ``yellow_flash`` is optional; the default pipeline continues to
    use the color detector for the yellow cue because it is cheaper and easier
    to calibrate.
    """

    def __init__(self, config: Config) -> None:
        try:
            from ultralytics import YOLO
        except ImportError as error:
            raise RuntimeError(
                "YOLO backend requires ultralytics. Install it with "
                "'.\\.venv\\Scripts\\python.exe -m pip install ultralytics'."
            ) from error

        model_path = Path(config.yolo_model_path)
        if not model_path.is_absolute():
            model_path = ROOT / model_path
        if not model_path.exists():
            raise FileNotFoundError(
                f"YOLO model not found: {model_path}. Train a model and set yolo_model_path in config.json."
            )
        self.model = YOLO(str(model_path))
        self.confidence = config.yolo_confidence
        self.iou = config.yolo_iou
        self.imgsz = config.yolo_imgsz
        self.device = config.yolo_device or None
        self.enemy_class = config.yolo_enemy_class.casefold()
        self.attack_class = config.yolo_attack_class.casefold()
        self.yellow_class = config.yolo_yellow_class.casefold()
        self.required_frames = config.attack_stable_frames
        self._attack_stable_frames = 0
        self._names = self.model.names

        logging.info(
            "Loaded YOLO model=%s device=%r imgsz=%d classes=%s",
            model_path, self.device, self.imgsz, self._names,
        )

    def _class_name(self, index: int) -> str:
        if isinstance(self._names, dict):
            return str(self._names.get(index, index)).casefold()
        return str(self._names[index] if index < len(self._names) else index).casefold()

    def detect(self, bgr_frame: np.ndarray) -> tuple[AttackResult, bool, int]:
        results = self.model.predict(
            source=bgr_frame,
            conf=self.confidence,
            iou=self.iou,
            imgsz=self.imgsz,
            device=self.device,
            verbose=False,
        )
        attack_score = 0.0
        yellow_detected = False
        enemy_count = 0
        if results:
            result = results[0]
            boxes = getattr(result, "boxes", None)
            if boxes is not None and len(boxes) > 0:
                classes = boxes.cls.detach().cpu().numpy().astype(int)
                confidences = boxes.conf.detach().cpu().numpy().astype(float)
                for class_index, confidence in zip(classes, confidences):
                    name = self._class_name(int(class_index))
                    if name == self.attack_class:
                        attack_score = max(attack_score, float(confidence))
                    elif name == self.enemy_class:
                        enemy_count += 1
                    elif self.yellow_class and name == self.yellow_class:
                        yellow_detected = True

        candidate = attack_score >= self.confidence
        self._attack_stable_frames = self._attack_stable_frames + 1 if candidate else 0
        attack = AttackResult(
            score=attack_score,
            active=self._attack_stable_frames >= self.required_frames,
            stable_frames=self._attack_stable_frames,
            diff_mask=np.zeros((1, 1), dtype=np.uint8),
        )
        return attack, yellow_detected, enemy_count


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    # INPUT must include the full Windows union; omitting MOUSEINPUT changes
    # sizeof(INPUT) and makes SendInput return ERROR_INVALID_PARAMETER (87).
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


class InputController:
    """Dispatch short keyboard events through SendInput.

    Scan-code input is used instead of the legacy ``keybd_event`` API because
    many games read keyboard state through DirectInput/raw input paths.  The
    return value is checked so an injection blocked by Windows (for example by
    a privilege/UIPI mismatch) is visible in ``automation.log``.
    """

    _INPUT_KEYBOARD = 1
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_SCANCODE = 0x0008
    _KEY_SCANCODES: dict[keyboard.Key | str, int] = {keyboard.Key.shift: 0x2A, "e": 0x12}

    def __init__(self, key_hold_ms: int, observe_only: bool, target_process_name: str) -> None:
        self._hold_seconds = key_hold_ms / 1000
        self._observe_only = observe_only
        self._target_process_name = target_process_name
        self._queue: queue.PriorityQueue[tuple[int, int, keyboard.Key | str | None]] = queue.PriorityQueue(maxsize=2)
        self._stop = threading.Event()
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._send_input = self._user32.SendInput
        self._send_input.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
        self._send_input.restype = ctypes.c_uint
        self._worker = threading.Thread(target=self._run, name="key-dispatcher", daemon=True)
        self._worker.start()

    def press(self, key: keyboard.Key | str) -> bool:
        if self._observe_only:
            logging.info("OBSERVE: would press %s", key)
            return True
        try:
            # Shift is kept ahead of a queued E event; both remain non-blocking.
            priority = 0 if key == keyboard.Key.shift else 1
            with self._sequence_lock:
                self._sequence += 1
                self._queue.put_nowait((priority, self._sequence, key))
            return True
        except queue.Full:
            logging.warning("Input queue busy; skipped %s", key)
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                _, _, key = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if key is None:
                return
            if not target_process_is_foreground(self._target_process_name):
                logging.info("Blocked queued %s: %s is no longer foreground", key, self._target_process_name)
                continue
            scan_code = self._KEY_SCANCODES[key]
            try:
                logging.info("Key down %s for %dms", key, round(self._hold_seconds * 1000))
                self._send_key(scan_code, key, key_up=False)
                time.sleep(self._hold_seconds)
            finally:
                self._send_key(scan_code, key, key_up=True)

    def _send_key(self, scan_code: int, key: keyboard.Key | str, key_up: bool) -> bool:
        flags = self._KEYEVENTF_SCANCODE | (self._KEYEVENTF_KEYUP if key_up else 0)
        event = _INPUT(
            type=self._INPUT_KEYBOARD,
            ki=_KEYBDINPUT(wVk=0, wScan=scan_code, dwFlags=flags, time=0, dwExtraInfo=None),
        )
        sent = int(self._send_input(1, ctypes.byref(event), ctypes.sizeof(_INPUT)))
        if sent != 1:
            error_code = ctypes.get_last_error()
            logging.error(
                "SendInput failed for %s (%s), key_up=%s, sent=%d, last_error=%d",
                key, hex(scan_code), key_up, sent, error_code,
            )
            if error_code == 5:
                logging.error(
                    "SendInput was denied (ERROR_ACCESS_DENIED). Start the script with the "
                    "same or higher privilege level as yysls.exe."
                )
            return False
        logging.info("SendInput ok for %s, key_up=%s, scan=%s", key, key_up, hex(scan_code))
        return True

    def close(self) -> None:
        self._stop.set()
        try:
            with self._sequence_lock:
                self._sequence += 1
                self._queue.put_nowait((0, self._sequence, None))
        except queue.Full:
            pass
        self._worker.join(timeout=1)
        for key in (keyboard.Key.shift, "e"):
            try:
                self._send_key(self._KEY_SCANCODES[key], key, key_up=True)
            except Exception:
                logging.exception("Failed to release %s during shutdown", key)


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except AttributeError:
        pass


def target_process_is_running(process_name: str) -> bool:
    target = process_name.casefold()
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info["name"] or "").casefold() == target:
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return False


def target_process_is_foreground(process_name: str) -> bool:
    if win32gui is None or win32process is None:
        logging.warning("pywin32 unavailable; refusing to dispatch keys")
        return False
    foreground = win32gui.GetForegroundWindow()
    if not foreground or win32gui.IsIconic(foreground):
        return False
    try:
        _, process_id = win32process.GetWindowThreadProcessId(foreground)
        return psutil.Process(process_id).name().casefold() == process_name.casefold()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False


def resolve_stop_key(name: str) -> keyboard.Key:
    normalized = name.strip().lower()
    try:
        return getattr(keyboard.Key, normalized)
    except AttributeError as error:
        raise ValueError(f"emergency_stop_key must be a pynput key name, such as f8; got {name!r}") from error


def grab_bgr(sct: mss.mss, region: Region) -> np.ndarray:
    raw = np.asarray(sct.grab(region.as_mss()), dtype=np.uint8)
    return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)


def crop_region(frame: np.ndarray, base: Region, target: Region) -> np.ndarray:
    """Crop a target screen region from a frame captured at ``base``."""
    x1 = max(0, target.left - base.left)
    y1 = max(0, target.top - base.top)
    x2 = min(frame.shape[1], x1 + target.width)
    y2 = min(frame.shape[0], y1 + target.height)
    return frame[y1:y2, x1:x2]


def cooldown_remaining_ms(last_trigger: float, cooldown_ms: int, now: float) -> int:
    if last_trigger == float("-inf"):
        return 0
    return max(0, round((cooldown_ms / 1000 - (now - last_trigger)) * 1000))


def draw_debug(
    yellow_frame: np.ndarray,
    yellow: YellowResult,
    attack_frame: np.ndarray,
    attack: AttackResult,
    observe_only: bool,
    last_action: str,
    shift_remaining: int,
    e_remaining: int,
) -> bool:
    yellow_display = yellow_frame.copy()
    attack_display = attack_frame.copy()
    cv2.putText(yellow_display, f"yellow={yellow.ratio:.3%} pixels={yellow.pixels} stable={yellow.stable_frames}",
                (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2, cv2.LINE_AA)
    cv2.putText(attack_display, f"motion={attack.score:.3%} stable={attack.stable_frames}",
                (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2, cv2.LINE_AA)
    status = np.zeros((110, 700, 3), dtype=np.uint8)
    mode = "OBSERVE ONLY" if observe_only else "INPUT ENABLED"
    cv2.putText(status, f"{mode} | last={last_action}", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 220, 255), 2)
    cv2.putText(status, f"shift cooldown={shift_remaining}ms | e cooldown={e_remaining}ms | Esc: quit | F8: stop",
                (8, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1)
    cv2.putText(status, "Prototype attack mode detects motion, not confirmed attacks.",
                (8, 92), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 180, 255), 1)
    cv2.imshow("Yellow ROI", yellow_display)
    cv2.imshow("Yellow Mask", yellow.mask)
    cv2.imshow("Attack ROI", attack_display)
    cv2.imshow("Attack Motion Mask", attack.diff_mask)
    cv2.imshow("Status", status)
    return (cv2.waitKey(1) & 0xFF) != 27


def run(
    config: Config,
    stop_requested: threading.Event | None = None,
    status_callback: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    yellow_detector = YellowDetector(config)
    attack_detector: MotionAttackDetector | YoloAttackDetector
    yolo_detector: YoloAttackDetector | None = None
    if config.vision_backend == "yolo_hybrid":
        yolo_detector = YoloAttackDetector(config)
        attack_detector = yolo_detector
    else:
        attack_detector = MotionAttackDetector(config)
    inputs = InputController(config.key_hold_ms, config.observe_only, config.target_process_name)
    stop_requested = stop_requested or threading.Event()
    stop_key = resolve_stop_key(config.emergency_stop_key)

    def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key == stop_key:
            logging.warning("Emergency stop requested by %s", config.emergency_stop_key)
            stop_requested.set()

    key_listener = keyboard.Listener(on_press=on_press)
    key_listener.start()
    frame_seconds = 1 / config.fps_limit
    last_shift = float("-inf")
    last_e = float("-inf")
    last_action = "none"
    last_status_update = 0.0
    last_yellow_ratio = 0.0
    last_attack_score = 0.0

    logging.info(
        "Started. mode=%s target_process=%r python=%s input_backend=SendInput input_size=%d",
        "observe" if config.observe_only else "active",
        config.target_process_name,
        sys.executable,
        ctypes.sizeof(_INPUT),
    )
    try:
        with mss.MSS() as sct:
            capture_region = config.capture_region.union(config.attack_region)
            while not stop_requested.is_set():
                loop_started = time.monotonic()
                process_running = target_process_is_running(config.target_process_name)
                foreground_ok = process_running and target_process_is_foreground(config.target_process_name)
                now = time.monotonic()

                # Do not capture or analyze another application while the game is in the background.
                if not foreground_ok:
                    last_action = (
                        f"waiting: {config.target_process_name} is not running"
                        if not process_running
                        else f"waiting: {config.target_process_name} is not foreground"
                    )
                    if status_callback and now - last_status_update >= 0.2:
                        status_callback({
                            "process_running": process_running,
                            "foreground_ok": False,
                            "yellow_ratio": last_yellow_ratio,
                            "attack_score": last_attack_score,
                            "last_action": last_action,
                        })
                        last_status_update = now
                    time.sleep(0.1)
                    continue

                combined_frame = grab_bgr(sct, capture_region)
                yellow_frame = crop_region(combined_frame, capture_region, config.capture_region)
                attack_frame = crop_region(combined_frame, capture_region, config.attack_region)
                yellow = yellow_detector.detect(yellow_frame)
                yolo_yellow_detected = False
                if yolo_detector is not None:
                    attack, yolo_yellow_detected, _ = yolo_detector.detect(attack_frame)
                else:
                    attack = attack_detector.update(attack_frame)
                now = time.monotonic()
                last_yellow_ratio = yellow.ratio
                last_attack_score = attack.score

                yellow_active = yellow.active or yolo_yellow_detected
                if yellow_active and cooldown_remaining_ms(last_shift, config.shift_cooldown_ms, now) == 0:
                    if inputs.press(keyboard.Key.shift):
                        last_shift, last_action = now, "Shift"
                elif not yellow_active and attack.active and cooldown_remaining_ms(last_e, config.e_cooldown_ms, now) == 0:
                    if inputs.press("e"):
                        last_e, last_action = now, "E"

                if status_callback and now - last_status_update >= 0.2:
                    status_callback({
                        "process_running": process_running,
                        "foreground_ok": True,
                        "yellow_ratio": yellow.ratio,
                        "attack_score": attack.score,
                        "last_action": last_action,
                    })
                    last_status_update = now

                if config.debug_window and not draw_debug(
                    yellow_frame, yellow, attack_frame, attack, config.observe_only, last_action,
                    cooldown_remaining_ms(last_shift, config.shift_cooldown_ms, now),
                    cooldown_remaining_ms(last_e, config.e_cooldown_ms, now),
                ):
                    break

                delay = frame_seconds - (time.monotonic() - loop_started)
                if delay > 0:
                    time.sleep(delay)
    finally:
        stop_requested.set()
        key_listener.stop()
        inputs.close()
        cv2.destroyAllWindows()
        logging.info("Stopped")


def configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )


def main() -> int:
    enable_dpi_awareness()
    configure_logging()
    try:
        config = Config.load(CONFIG_PATH)
        if "--headless" in sys.argv:
            print(json.dumps({name.name: getattr(config, name.name) for name in fields(Config)}, ensure_ascii=False, default=str, indent=2))
            run(config)
        else:
            from gui import AutomationApp

            AutomationApp(config).run()
        return 0
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        logging.error("Configuration error: %s", error)
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception:
        logging.exception("Fatal error")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
