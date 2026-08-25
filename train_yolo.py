"""Train a YOLOv8 detector for the game dataset.

The dataset YAML must define names including ``enemy`` and ``enemy_attack``.
See dataset.yaml.example for the expected layout.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Train a YOLOv8 game detector")
    parser.add_argument("--data", type=Path, default=Path("dataset/dataset.yaml"))
    parser.add_argument("--base", default="yolov8s.pt", help="Base Ultralytics checkpoint")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--device", default="0")
    parser.add_argument("--project", type=Path, default=Path("runs/yysls"))
    args = parser.parse_args()
    if not args.data.exists():
        parser.error(f"Dataset YAML not found: {args.data}")

    try:
        from ultralytics import YOLO
    except ImportError as error:
        parser.error("Install dependencies first: python -m pip install ultralytics")
        raise error

    model = YOLO(args.base)
    model.train(
        data=str(args.data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        project=str(args.project),
        name="train",
        patience=25,
        cache=False,
    )
    print("Training finished. Copy runs/yysls/train/weights/best.pt to models/yolov8s_yysls.pt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
