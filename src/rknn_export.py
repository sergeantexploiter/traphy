#!/usr/bin/env python3
"""
Convert a YOLOv8 model to RKNN for the Orange Pi RK3588 6 TOPS NPU.

Typical flow (ONNX can be exported on any machine; RKNN conversion needs
rknn-toolkit2 on Linux x86_64 or on the Orange Pi itself):

    # 1. Export ONNX (works on this Mac) and convert if toolkit2 is present
    python3 src/rknn_export.py --model yolov8n.pt --target rk3588 --dtype fp

    # 2. INT8 (uses the NPU hardest) — pass camera frames for calibration
    python3 src/rknn_export.py --model yolov8n.pt --target rk3588 --dtype i8 \\
        --video videos/real_footage/pole_1_right.mp4

    # 3. Plate detector (1 class)
    python3 src/rknn_export.py --model models/license_plate_detector.pt --target rk3588 --dtype fp

    # 4. Already-exported ONNX (run this on the Orange Pi if conversion failed here)
    python3 src/rknn_export.py --model models/yolov8n.onnx --target rk3588 --dtype i8 \\
        --dataset models/calib

On the Orange Pi, load the result with lane_detector / plate_reader:

    python3 src/lane_detector.py --lane south_right --rtsp "..." \\
        --model models/yolov8n.rknn
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

import cv2

_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import config  # noqa: E402

logger = logging.getLogger("rknn_export")

RK3588_FAMILY = {
    "rk3588", "rk3588s", "orangepi5", "orangepi5plus", "orangepi5pro", "opi5",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Export YOLOv8 (.pt/.onnx) to RKNN for the Orange Pi NPU.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--model",
        default=getattr(config, "YOLO_MODEL", "yolov8n.pt"),
        help="Source weights: .pt (Ultralytics) or .onnx.",
    )
    p.add_argument(
        "--target",
        default=getattr(config, "RKNN_TARGET", "rk3588"),
        help="NPU platform (rk3588 = Orange Pi 5 / 5 Plus / 5 Pro, 6 TOPS).",
    )
    p.add_argument(
        "--dtype",
        choices=("i8", "u8", "fp"),
        default="fp",
        help="i8/u8 = quantized (fastest on NPU, needs --dataset/--video). "
             "fp = FP16, no calibration, still runs on the NPU. Default: fp.",
    )
    p.add_argument("--imgsz", type=int, default=getattr(config, "RKNN_IMGSZ", 640),
                   help="Square input size used for export and inference.")
    p.add_argument("--output", help="Destination .rknn path. Default: models/<stem>.rknn")
    p.add_argument("--onnx", help="Where to write the intermediate ONNX. Default: models/<stem>.onnx")
    p.add_argument("--dataset", help="Calibration image folder or a .txt list (one path per line).")
    p.add_argument("--video", help="Extract calibration frames from this video (INT8).")
    p.add_argument("--calib-frames", type=int, default=50, help="Max frames to pull from --video.")
    p.add_argument("--calib-dir", default="models/calib", help="Folder for extracted calib images.")
    p.add_argument("--opset", type=int, default=12, help="ONNX opset (12 is the RKNN-safe default).")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _resolve_existing(path: str) -> Path:
    p = Path(path)
    if p.is_file():
        return p.resolve()
    cand = _PROJECT_ROOT / path
    if cand.is_file():
        return cand.resolve()
    return p


def export_onnx(pt_path: Path, onnx_path: Path, imgsz: int, opset: int) -> Path:
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit("ultralytics is required to export .pt → ONNX. pip install ultralytics") from exc

    logger.info("Exporting ONNX from %s (imgsz=%d, opset=%d)", pt_path, imgsz, opset)
    model = YOLO(str(pt_path))
    exported = model.export(
        format="onnx",
        imgsz=imgsz,
        opset=opset,
        simplify=True,
        dynamic=False,
        nms=False,
    )
    exported = Path(str(exported))
    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    if exported.resolve() != onnx_path.resolve():
        onnx_path.write_bytes(exported.read_bytes())
        logger.info("Copied ONNX %s → %s", exported, onnx_path)
    else:
        logger.info("ONNX written to %s", onnx_path)
    return onnx_path


def collect_image_paths(dataset, video, calib_dir, calib_frames) -> list[str]:
    paths: list[str] = []
    if dataset:
        ds = Path(dataset)
        if not ds.is_absolute():
            ds = (_PROJECT_ROOT / ds) if not ds.exists() else ds
        if ds.is_file() and ds.suffix.lower() == ".txt":
            for line in ds.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    paths.append(line)
        elif ds.is_dir():
            for p in sorted(ds.iterdir()):
                if p.suffix.lower() in IMAGE_EXTS:
                    paths.append(str(p.resolve()))
        else:
            raise SystemExit(f"--dataset not found: {dataset}")

    if video:
        out_dir = Path(calib_dir)
        if not out_dir.is_absolute():
            out_dir = _PROJECT_ROOT / out_dir
        extracted = extract_video_frames(video, out_dir, calib_frames)
        paths.extend(extracted)

    # Deduplicate while keeping order.
    seen = set()
    uniq = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def extract_video_frames(video: str, out_dir: Path, max_frames: int) -> list[str]:
    src = video
    candidate = _PROJECT_ROOT / video
    if not Path(src).is_file() and candidate.is_file():
        src = str(candidate)
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise SystemExit(f"Could not open calibration video: {video}")

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    step = max(1, total // max_frames) if total > 0 else 15
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    idx = 0
    saved = 0
    logger.info("Extracting up to %d calib frames from %s (step=%d)", max_frames, src, step)
    while saved < max_frames:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        if idx % step == 0:
            dest = out_dir / f"calib_{saved:04d}.jpg"
            cv2.imwrite(str(dest), frame)
            written.append(str(dest.resolve()))
            saved += 1
        idx += 1
    cap.release()
    logger.info("Wrote %d calibration images to %s", len(written), out_dir)
    return written


def write_dataset_list(image_paths: list[str]) -> str:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix="_rknn_dataset.txt", delete=False, encoding="utf-8"
    )
    for p in image_paths:
        tmp.write(p + "\n")
    tmp.close()
    return tmp.name


def convert_rknn(onnx_path: Path, rknn_path: Path, target: str, do_quant: bool, dataset_txt):
    try:
        from rknn.api import RKNN
    except ImportError as exc:
        raise SystemExit(
            "rknn-toolkit2 is not installed, so ONNX was exported but .rknn was not built.\n"
            "RKNN conversion needs Linux (x86_64 PC or the Orange Pi), not macOS.\n\n"
            "On the Orange Pi:\n"
            "  pip3 install rknn-toolkit2\n"
            "  # or install the official wheel from https://github.com/airockchip/rknn-toolkit2\n"
            f"  python3 src/rknn_export.py --model {onnx_path} --target {target} "
            f"--dtype {'i8' if do_quant else 'fp'}\n"
        ) from exc

    platform = "rk3588" if target.lower() in RK3588_FAMILY else target
    rknn_path.parent.mkdir(parents=True, exist_ok=True)

    rknn = RKNN(verbose=True)
    logger.info("RKNN config: target=%s mean=0 std=255 quant=%s", platform, do_quant)
    rknn.config(
        mean_values=[[0, 0, 0]],
        std_values=[[255, 255, 255]],
        target_platform=platform,
    )

    logger.info("Loading ONNX %s", onnx_path)
    ret = rknn.load_onnx(model=str(onnx_path))
    if ret != 0:
        rknn.release()
        raise SystemExit(f"rknn.load_onnx failed ({ret})")

    logger.info("Building RKNN model (this can take several minutes)…")
    ret = rknn.build(do_quantization=do_quant, dataset=dataset_txt)
    if ret != 0:
        rknn.release()
        raise SystemExit(f"rknn.build failed ({ret})")

    logger.info("Exporting %s", rknn_path)
    ret = rknn.export_rknn(str(rknn_path))
    rknn.release()
    if ret != 0:
        raise SystemExit(f"rknn.export_rknn failed ({ret})")
    logger.info("RKNN model ready: %s", rknn_path)
    return rknn_path


def main() -> None:
    args = build_arg_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    src = _resolve_existing(args.model)
    suffix = src.suffix.lower()
    if suffix not in {".pt", ".onnx"} and not src.exists():
        # Ultralytics will download official names like yolov8n.pt
        src = Path(args.model)
        suffix = src.suffix.lower()
        if suffix != ".pt":
            raise SystemExit(f"Model not found: {args.model}")

    stem = src.stem
    models_dir = _PROJECT_ROOT / "models"
    onnx_path = Path(args.onnx) if args.onnx else models_dir / f"{stem}.onnx"
    if not onnx_path.is_absolute():
        onnx_path = _PROJECT_ROOT / onnx_path
    rknn_path = Path(args.output) if args.output else models_dir / f"{stem}.rknn"
    if not rknn_path.is_absolute():
        rknn_path = _PROJECT_ROOT / rknn_path

    if suffix == ".pt":
        onnx_path = export_onnx(src, onnx_path, args.imgsz, args.opset)
    elif suffix == ".onnx":
        onnx_path = src if src.is_file() else _resolve_existing(str(src))
        if not onnx_path.is_file():
            raise SystemExit(f"ONNX not found: {src}")
    else:
        raise SystemExit(f"Unsupported source '{src}' — use .pt or .onnx")

    do_quant = args.dtype in ("i8", "u8")
    dataset_txt = None
    if do_quant:
        images = collect_image_paths(args.dataset, args.video, args.calib_dir, args.calib_frames)
        if not images:
            raise SystemExit(
                "INT8/UINT8 quantization needs calibration images.\n"
                "  --dataset models/calib          (folder of jpg/png)\n"
                "  --video path/to/camera.mp4      (extracts frames)\n"
                "Or convert without calibration:\n"
                f"  python3 src/rknn_export.py --model {onnx_path} --target {args.target} --dtype fp"
            )
        dataset_txt = write_dataset_list(images)
        logger.info("Calibration list (%d images): %s", len(images), dataset_txt)

    convert_rknn(onnx_path, rknn_path, args.target, do_quant, dataset_txt)
    print()
    print(f"ONNX : {onnx_path}")
    print(f"RKNN : {rknn_path}")
    print()
    print("On the Orange Pi, point the detector at the .rknn file:")
    print(f"  python3 src/lane_detector.py --lane south_right --model {rknn_path} --rtsp '...'")
    print("A .rnn extension is also accepted by the loader.")


if __name__ == "__main__":
    main()
