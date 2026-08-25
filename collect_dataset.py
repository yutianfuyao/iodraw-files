"""Capture training frames from a screen region with mss.

Examples:
  python collect_dataset.py --left 650 --top 180 --width 620 --height 620
  python collect_dataset.py --left 650 --top 180 --width 620 --height 620 --fps 10 --output dataset/raw

The resulting PNGs can be annotated with CVAT, Label Studio, or another YOLO
annotation tool. Press Esc in the preview window to stop.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import mss
import numpy as np


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture YOLO training frames from the screen")
    parser.add_argument("--left", type=int, required=True)
    parser.add_argument("--top", type=int, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=Path("dataset/raw"))
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    if args.width <= 0 or args.height <= 0 or args.fps <= 0:
        parser.error("width, height and fps must be positive")

    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "frames.jsonl"
    monitor = {"left": args.left, "top": args.top, "width": args.width, "height": args.height}
    interval = 1.0 / args.fps
    frame_id = 0
    next_frame = time.perf_counter()
    with manifest_path.open("a", encoding="utf-8") as manifest, mss.MSS() as screen:
        while True:
            now = time.perf_counter()
            if now < next_frame:
                time.sleep(next_frame - now)
            next_frame += interval
            raw = np.asarray(screen.grab(monitor), dtype=np.uint8)
            frame = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
            filename = f"frame_{frame_id:07d}.png"
            path = args.output / filename
            if not cv2.imwrite(str(path), frame):
                raise OSError(f"Failed to write {path}")
            manifest.write(json.dumps({"file": filename, "timestamp": time.time()}, ensure_ascii=False) + "\n")
            manifest.flush()
            frame_id += 1

            if args.preview:
                preview = frame.copy()
                cv2.putText(preview, f"frames={frame_id} | Esc: stop", (10, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.imshow("Dataset capture", preview)
                if cv2.waitKey(1) & 0xFF == 27:
                    break
    cv2.destroyAllWindows()
    print(f"Captured {frame_id} frames to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
