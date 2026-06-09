#!/usr/bin/env python3
"""
plate_reader.py — two-stage ANPR: detect a vehicle, localize its number-plate
pixels, then OCR the plate text.

Pipeline (per processed frame):
  1. Vehicle detection  — YOLOv8 (COCO car/bus/truck/motorcycle) + optional SORT tracking.
  2. Plate localization  — for each vehicle crop, find the plate region. Uses a dedicated
     YOLOv8 plate model when available (--plate-model), otherwise a classic OpenCV
     edge/aspect-ratio localizer (less reliable; a model is strongly recommended).
  3. OCR                — read the plate crop with EasyOCR (default) or Tesseract.

The best (highest-confidence) read is cached per tracked vehicle so the displayed
text is stable and doesn't flicker. Reads can be logged to CSV and/or saved as crops.

This is a standalone analysis/enforcement tool — it does NOT publish to MQTT
(lane demand is handled by lane_detector.py).

Examples (run from the repo root):
  # Video file, dedicated plate model, live window
  python3 src/plate_reader.py videos/real_footage/video-1.mp4 \
      --plate-model models/license_plate_detector.pt --show

  # RTSP camera, log every read to CSV and save plate crops
  python3 src/plate_reader.py --rtsp "rtsp://admin:pass@192.168.0.5:554/Streaming/Channels/101" \
      --plate-model models/license_plate_detector.pt --csv plates.csv --save-crops plate_crops

  # Webcam 0, no plate model (classic localizer fallback), English OCR
  python3 src/plate_reader.py -w 0 --show

  # Just localize plates without reading them (skip OCR)
  python3 src/plate_reader.py videos/real_footage/video-1.mp4 --ocr none --show

Window controls (with --show):
  space = pause/resume,  n = step forward,  p = step back (files),
  g = go to frame # (files),  0 = first,  $ = last,  q = quit.
  A "Frame" trackbar appears for seekable video files.

A dedicated plate detector model (single class "license_plate") gives far better
results than the classic fallback. Drop one at models/license_plate_detector.pt
or pass --plate-model PATH.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import signal
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# Resolve imports whether run as `python3 src/plate_reader.py` (repo root) or from inside src/.
_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import config  # noqa: E402  (after sys.path setup)

# Reuse the stable source/seek/geometry/detection helpers from lane_detector.
from lane_detector import (  # noqa: E402
    Sort,
    detections_in_frame,
    load_points_file,
    open_capture,
    parse_points,
    point_inside_polygon,
    prompt_goto_frame,
    resolve_source,
)

try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None

logger = logging.getLogger("plate_reader")

# COCO class ids for vehicles: 1=bicycle 2=car 3=motorcycle 5=bus 7=truck
DEFAULT_VEHICLE_CLASS_IDS = [2, 3, 5, 7]

# BGR colors chosen for visibility on road footage.
_GREEN = (0, 255, 0)
_BLUE = (255, 128, 0)
_CYAN = (255, 255, 0)
_RED = (0, 0, 255)
_AMBER = (0, 200, 255)
_YELLOW = (0, 255, 255)

_PLATE_CLEAN_RE = re.compile(r"[^A-Z0-9]")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _label(frame, text, org, color, scale=0.7, thickness=2):
    """Draw text with a dark background box so labels stay readable on any frame."""
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    x, y = org
    cv2.rectangle(frame, (x - 3, y - th - 5), (x + tw + 3, y + base + 2), (20, 20, 20), -1)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def _clamp_box(x1, y1, x2, y2, w, h):
    x1 = max(0, min(int(x1), w - 1))
    y1 = max(0, min(int(y1), h - 1))
    x2 = max(0, min(int(x2), w))
    y2 = max(0, min(int(y2), h))
    return x1, y1, x2, y2


def normalize_plate(text: str) -> str:
    """Uppercase and keep only A-Z/0-9 (plates are matched without spaces/punctuation)."""
    return _PLATE_CLEAN_RE.sub("", (text or "").upper())


def resolve_plate_model_default(explicit: str | None) -> str | None:
    """CLI flag > config.PLATE_MODEL > a conventional file in the repo, else None (classic fallback)."""
    if explicit:
        return explicit
    cfg_model = getattr(config, "PLATE_MODEL", None)
    if cfg_model:
        return cfg_model
    for cand in ("models/license_plate_detector.pt", "license_plate_detector.pt"):
        p = _PROJECT_ROOT / cand
        if p.exists():
            return str(p.resolve())
    return None


# ---------------------------------------------------------------------------
# Plate localization (stage 2): model-based, with a classic OpenCV fallback
# ---------------------------------------------------------------------------

def locate_plate_classic(crop: np.ndarray):
    """Heuristic plate localizer: blackhat + gradient + aspect-ratio contour filtering.

    Returns (x1, y1, x2, y2) in crop coordinates, or None. Far less reliable than a
    dedicated model — used only when no --plate-model is available.
    """
    h, w = crop.shape[:2]
    if h < 12 or w < 24:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 11, 17, 17)
    rect_kern = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 5))
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, rect_kern)
    grad = np.absolute(cv2.Sobel(blackhat, cv2.CV_32F, 1, 0, ksize=-1))
    gmin, gmax = float(np.min(grad)), float(np.max(grad))
    if gmax - gmin < 1e-3:
        return None
    grad = (255 * ((grad - gmin) / (gmax - gmin))).astype("uint8")
    grad = cv2.morphologyEx(grad, cv2.MORPH_CLOSE, rect_kern)
    thresh = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    thresh = cv2.erode(thresh, None, iterations=2)
    thresh = cv2.dilate(thresh, None, iterations=2)
    cnts, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    best_score = 0.0
    for c in cnts:
        x, y, ww, hh = cv2.boundingRect(c)
        if hh < 8 or ww < 16:
            continue
        ar = ww / float(hh)
        area = ww * hh
        # Plates: wide rectangles, usually on the lower half of the vehicle.
        if 2.0 <= ar <= 6.5 and area > 0.01 * w * h and y > 0.20 * h:
            score = area * (1.0 + (y / float(h)))  # bias toward larger, lower boxes
            if score > best_score:
                best_score = score
                best = (x, y, x + ww, y + hh)
    return best


def plate_boxes_in_crop(crop: np.ndarray, plate_model, plate_conf: float):
    """Return [(x1, y1, x2, y2, conf), ...] in CROP coordinates."""
    if crop is None or crop.size == 0:
        return []
    if plate_model is not None:
        res = plate_model(crop, verbose=False)[0]
        boxes = res.boxes
        out = []
        if boxes is not None:
            for i in range(len(boxes)):
                conf = float(boxes.conf[i].item())
                if conf < plate_conf:
                    continue
                x1, y1, x2, y2 = boxes.xyxy[i].cpu().numpy()
                out.append([float(x1), float(y1), float(x2), float(y2), conf])
        return out
    box = locate_plate_classic(crop)
    return [[float(box[0]), float(box[1]), float(box[2]), float(box[3]), 0.0]] if box else []


# ---------------------------------------------------------------------------
# OCR (stage 3)
# ---------------------------------------------------------------------------

def _prep_for_ocr(plate_bgr: np.ndarray, min_h: int = 64) -> np.ndarray:
    """Upscale small plates, grayscale, denoise and boost contrast for OCR."""
    h, w = plate_bgr.shape[:2]
    scale = max(1.0, min_h / max(1, h))
    if scale > 1.0:
        plate_bgr = cv2.resize(plate_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(plate_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 11, 17, 17)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


class PlateOCR:
    """Reads plate text from a BGR crop. Engine: easyocr (default), tesseract, or none."""

    def __init__(self, engine: str, languages, gpu: bool):
        self.engine = engine
        self._reader = None
        self._pyt = None
        if engine == "easyocr":
            try:
                import easyocr
            except ImportError:
                raise SystemExit(
                    "easyocr not installed. Run: pip install easyocr  (or use --ocr tesseract / --ocr none)"
                )
            logger.info("Loading EasyOCR (languages=%s, gpu=%s) — first run downloads models...", languages, gpu)
            self._reader = easyocr.Reader(list(languages), gpu=gpu)
        elif engine == "tesseract":
            try:
                import pytesseract
            except ImportError:
                raise SystemExit(
                    "pytesseract not installed. Run: pip install pytesseract and install the tesseract binary "
                    "(brew install tesseract / apt install tesseract-ocr), or use --ocr easyocr."
                )
            self._pyt = pytesseract
        elif engine != "none":
            raise SystemExit(f"Unknown --ocr engine: {engine}")

    def read(self, plate_bgr):
        """Return (normalized_text, confidence 0..1)."""
        if self.engine == "none" or plate_bgr is None or plate_bgr.size == 0:
            return "", 0.0
        proc = _prep_for_ocr(plate_bgr)
        if self.engine == "easyocr":
            return self._read_easyocr(proc)
        return self._read_tesseract(proc)

    def _read_easyocr(self, proc):
        try:
            results = self._reader.readtext(proc, detail=1, paragraph=False)
        except Exception as e:  # pragma: no cover - runtime/IO guard
            logger.debug("easyocr error: %s", e)
            return "", 0.0
        if not results:
            return "", 0.0
        results.sort(key=lambda r: r[0][0][0])  # left-to-right by first corner x
        texts, confs = [], []
        for bbox, txt, conf in results:
            t = normalize_plate(txt)
            if t:
                texts.append(t)
                confs.append(float(conf))
        return "".join(texts), (float(np.mean(confs)) if confs else 0.0)

    def _read_tesseract(self, proc):
        th = cv2.threshold(proc, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
        cfg = "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        try:
            data = self._pyt.image_to_data(th, config=cfg, output_type=self._pyt.Output.DICT)
        except Exception as e:  # pragma: no cover - runtime/IO guard
            logger.debug("tesseract error: %s", e)
            return "", 0.0
        texts, confs = [], []
        for i, txt in enumerate(data.get("text", [])):
            t = normalize_plate(txt)
            try:
                c = float(data["conf"][i])
            except (ValueError, KeyError, IndexError):
                c = -1.0
            if t and c >= 0:
                texts.append(t)
                confs.append(c / 100.0)
        return "".join(texts), (float(np.mean(confs)) if confs else 0.0)


# ---------------------------------------------------------------------------
# CSV / crop sinks
# ---------------------------------------------------------------------------

class ReadLog:
    """Appends accepted plate reads to a CSV and/or saves plate crops to a folder."""

    def __init__(self, csv_path: str | None, crops_dir: str | None):
        self._csv_file = None
        self._writer = None
        self._crops_dir = None
        if csv_path:
            path = Path(csv_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists() or path.stat().st_size == 0
            self._csv_file = path.open("a", newline="")
            self._writer = csv.writer(self._csv_file)
            if new:
                self._writer.writerow(["timestamp_iso", "unix_ts", "track_id", "plate", "ocr_conf", "frame"])
            logger.info("Logging reads to %s", path.resolve())
        if crops_dir:
            self._crops_dir = Path(crops_dir)
            self._crops_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Saving plate crops to %s", self._crops_dir.resolve())

    def record(self, track_id, text, conf, frame_idx, plate_crop):
        now = time.time()
        if self._writer is not None:
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
            self._writer.writerow([iso, int(now), track_id, text, f"{conf:.3f}", frame_idx])
            self._csv_file.flush()
        if self._crops_dir is not None and plate_crop is not None and plate_crop.size > 0:
            safe = text or "unknown"
            fname = f"{int(now)}_id{track_id}_{safe}_{conf:.2f}.jpg"
            cv2.imwrite(str(self._crops_dir / fname), plate_crop)

    def close(self):
        if self._csv_file is not None:
            try:
                self._csv_file.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Detect vehicles, localize their number plates, and read the text (ANPR).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Source
    p.add_argument("source", nargs="?", help="Video file path (relative to repo root ok) or webcam index.")
    p.add_argument("--source", dest="source_opt", help="Same as positional source.")
    p.add_argument("--rtsp", metavar="URL", help="RTSP/HTTP stream URL.")
    p.add_argument("-w", "--webcam", nargs="?", const=0, type=int, metavar="INDEX", help="Webcam index (default 0).")

    # Optional detection ROI (ignore vehicles outside it)
    p.add_argument("--roi", help='Restrict detection to a polygon, e.g. "x1,y1 x2,y2 x3,y3".')
    p.add_argument("--points-file", help="JSON with LANE_ROI_POINTS (reuses lane_points/*.json).")

    # Vehicle detector
    p.add_argument("--model", default=getattr(config, "YOLO_MODEL", "yolov8n.pt"), help="Vehicle YOLOv8 weights.")
    p.add_argument("--conf", type=float, default=0.4, help="Vehicle confidence threshold.")
    p.add_argument("--classes", default=",".join(map(str, DEFAULT_VEHICLE_CLASS_IDS)),
                   help="Comma-separated COCO class ids (default vehicles).")
    p.add_argument("--detect-every-n", type=int, default=2, help="Run vehicle YOLO every N frames (SORT fills gaps).")
    p.add_argument("--no-track", action="store_true", help="Disable SORT; use raw per-frame detections.")

    # Plate detector
    p.add_argument("--plate-model", help="YOLOv8 license-plate weights. Default: config.PLATE_MODEL or "
                                         "models/license_plate_detector.pt if present, else classic fallback.")
    p.add_argument("--plate-conf", type=float, default=getattr(config, "PLATE_DETECT_CONF", 0.25),
                   help="Plate detection confidence threshold (model only).")

    # OCR
    p.add_argument("--ocr", default=getattr(config, "PLATE_OCR_ENGINE", "easyocr"),
                   choices=["easyocr", "tesseract", "none"], help="OCR engine (none = localize only).")
    p.add_argument("--lang", default=",".join(getattr(config, "PLATE_OCR_LANGS", ["en"])),
                   help="Comma-separated OCR languages (EasyOCR), e.g. en.")
    p.add_argument("--gpu", action="store_true", help="Use GPU for EasyOCR (needs CUDA build of torch).")
    p.add_argument("--min-ocr-conf", type=float, default=getattr(config, "PLATE_MIN_OCR_CONF", 0.3),
                   help="Keep a read only above this confidence; tracks below it are re-read.")
    p.add_argument("--min-plate-len", type=int, default=3, help="Ignore reads shorter than this many characters.")
    p.add_argument("--read-every-n", type=int, default=5, help="Attempt plate detect+OCR every N processed frames.")
    p.add_argument("--always-reread", action="store_true",
                   help="Keep re-reading every track (don't stop after a confident read).")
    p.add_argument("--forget-frames", type=int, default=90,
                   help="Drop a track's cached plate after it's unseen for this many frames.")

    # Output
    p.add_argument("--show", action="store_true", help="Show an OpenCV window (needs a display).")
    p.add_argument("--csv", help="Append accepted reads to this CSV file.")
    p.add_argument("--save-crops", help="Save plate crops (on each improved read) to this folder.")
    p.add_argument("--save-video", help="Write the annotated video to this path (e.g. out.mp4).")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return p


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

def _draw_overlay(frame, tracks, track_best, roi_points, vehicles, plates_read,
                  paused=False, frame_idx=-1, total_frames=0, seekable=False):
    if roi_points and len(roi_points) >= 2:
        cv2.polylines(frame, [np.array(roi_points, dtype=np.int32)], len(roi_points) >= 3, _CYAN, 2)

    for tr in tracks:
        x1, y1, x2, y2 = int(tr[0]), int(tr[1]), int(tr[2]), int(tr[3])
        tid = int(tr[4])
        cv2.rectangle(frame, (x1, y1), (x2, y2), _GREEN, 2)
        _label(frame, f"ID:{tid}", (x1, max(16, y1 - 6)), _GREEN, scale=0.5, thickness=1)
        best = track_best.get(tid)
        if best and best.get("box"):
            px1, py1, px2, py2 = best["box"]
            cv2.rectangle(frame, (px1, py1), (px2, py2), _AMBER, 2)
            if best.get("text"):
                txt = f"{best['text']} ({best['conf']:.2f})"
                _label(frame, txt, (px1, max(18, py1 - 6)), _YELLOW, scale=0.6, thickness=2)

    _label(frame, f"vehicles: {vehicles}", (10, 34), _GREEN, scale=0.7, thickness=2)
    _label(frame, f"plates read: {plates_read}", (10, 64), _AMBER, scale=0.7, thickness=2)
    if seekable and frame_idx >= 0:
        total = total_frames if total_frames > 0 else "?"
        _label(frame, f"frame {frame_idx + 1}/{total}", (10, 92), _CYAN, scale=0.55, thickness=1)
    if paused:
        _label(frame, "PAUSED  (space=resume  n/p=step  g=goto)", (10, 118), _RED, scale=0.55, thickness=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = build_arg_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if YOLO is None:
        raise SystemExit("ultralytics not installed. Run: pip install ultralytics")

    # Optional ROI to restrict where we look for vehicles.
    roi_points: list = []
    if args.points_file:
        roi_points = load_points_file(args.points_file).get("LANE_ROI_POINTS", [])
    if args.roi is not None:
        roi_points = parse_points(args.roi)

    class_ids = {int(c) for c in str(args.classes).split(",") if c.strip().isdigit()}
    langs = [s.strip() for s in str(args.lang).split(",") if s.strip()]

    cv_src, label, kind = resolve_source(args.source or args.source_opt, args.webcam, args.rtsp)
    logger.info("Source: %s (%s)", label, kind)

    # Stage 1: vehicle model.
    model = YOLO(args.model)
    use_track = (not args.no_track) and (Sort is not None)
    tracker = Sort(max_age=10, min_hits=2, iou_threshold=0.3) if use_track else None
    if not use_track:
        logger.warning("SORT tracking disabled — per-vehicle plate caching is keyed on unstable ids.")

    # Stage 2: plate model (optional).
    plate_model_path = resolve_plate_model_default(args.plate_model)
    plate_model = None
    if plate_model_path:
        logger.info("Plate detector: %s", plate_model_path)
        plate_model = YOLO(plate_model_path)
    else:
        logger.warning("No plate model — using the classic OpenCV localizer (less reliable). "
                       "Pass --plate-model PATH or drop models/license_plate_detector.pt for best results.")

    # Stage 3: OCR.
    ocr = PlateOCR(args.ocr, langs, args.gpu)

    read_log = ReadLog(args.csv, args.save_crops)

    running = {"on": True}

    def _stop(signum, frame):
        running["on"] = False
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    cap = open_capture(cv_src, kind)
    if not cap.isOpened():
        read_log.close()
        raise SystemExit(f"Could not open source: {label}")

    seekable = args.show and kind == "file"
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if seekable else 0
    if total_frames < 0:
        total_frames = 0
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if not (1.0 <= src_fps <= 120.0):
        src_fps = 25.0

    win = "plate_reader"
    trackbar_guard = {"sync": True}
    pending_seek = {"idx": None}
    if args.show:
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)

        def _on_trackbar(pos):
            if trackbar_guard["sync"]:
                pending_seek["idx"] = pos

        if seekable and total_frames > 1:
            cv2.createTrackbar("Frame", win, 0, total_frames - 1, _on_trackbar)
            logger.info("Seek enabled: trackbar, g=goto, n/p=step, 0/$=first/last, space=pause")

    writer = None  # lazily created for --save-video

    track_best: dict = {}      # tid -> {text, conf, box, ts, frame}
    track_last_seen: dict = {}  # tid -> frame_idx
    plates_read = 0
    frame_idx = 0
    displayed_idx = -1
    last_dets = np.empty((0, 5))
    read_fail = 0
    paused = False
    step_once = False
    frame = None
    tracks = np.empty((0, 5))

    try:
        while running["on"]:
            do_read = (not paused) or step_once or (pending_seek["idx"] is not None)

            if do_read:
                if pending_seek["idx"] is not None and seekable:
                    target = max(0, min(pending_seek["idx"], (total_frames - 1) if total_frames else pending_seek["idx"]))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                    pending_seek["idx"] = None

                ret, new_frame = cap.read()
                if not ret or new_frame is None:
                    if kind == "file":
                        logger.info("End of file — exiting.")
                        break
                    read_fail += 1
                    logger.warning("Frame read failed (%d) — reopening %s in 2s", read_fail, label)
                    cap.release()
                    time.sleep(2.0)
                    cap = open_capture(cv_src, kind)
                    continue

                read_fail = 0
                frame = new_frame
                frame_idx += 1
                displayed_idx = (int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1) if seekable else displayed_idx + 1
                fh, fw = frame.shape[:2]

                # Stage 1: vehicle detection (+ tracking).
                run_yolo = (args.detect_every_n <= 1) or (frame_idx % args.detect_every_n == 1)
                if run_yolo:
                    last_dets = detections_in_frame(frame, model, class_ids, args.conf, roi_points)
                if tracker is not None:
                    tracks = tracker.update(last_dets[:, :5] if len(last_dets) else np.empty((0, 5)))
                else:
                    tracks = np.array([[*d[:4], i] for i, d in enumerate(last_dets)]) if len(last_dets) else np.empty((0, 5))

                # Stages 2+3: localize + read plates (throttled).
                run_read = (args.read_every_n <= 1) or (frame_idx % args.read_every_n == 0)
                for tr in tracks:
                    tid = int(tr[4])
                    track_last_seen[tid] = frame_idx
                    if not run_read:
                        continue
                    best = track_best.get(tid)
                    confident = best is not None and best["conf"] >= args.min_ocr_conf
                    if confident and not args.always_reread:
                        continue

                    vx1, vy1, vx2, vy2 = _clamp_box(tr[0], tr[1], tr[2], tr[3], fw, fh)
                    if vx2 - vx1 < 16 or vy2 - vy1 < 16:
                        continue
                    crop = frame[vy1:vy2, vx1:vx2]
                    pboxes = plate_boxes_in_crop(crop, plate_model, args.plate_conf)
                    if not pboxes:
                        continue
                    pb = max(pboxes, key=lambda b: b[4])  # highest-confidence plate
                    px1, py1, px2, py2 = _clamp_box(pb[0] + vx1, pb[1] + vy1, pb[2] + vx1, pb[3] + vy1, fw, fh)
                    if px2 - px1 < 8 or py2 - py1 < 6:
                        continue
                    plate_crop = frame[py1:py2, px1:px2]

                    text, oconf = ocr.read(plate_crop)
                    if args.ocr == "none":
                        # Localization-only: still cache the box so it's drawn.
                        if best is None or pb[4] > best.get("conf", -1.0):
                            track_best[tid] = {"text": "", "conf": pb[4], "box": (px1, py1, px2, py2),
                                               "ts": time.time(), "frame": frame_idx}
                        continue
                    if not text or len(text) < args.min_plate_len:
                        continue
                    if best is None or oconf > best["conf"]:
                        improved = best is None or text != best.get("text")
                        track_best[tid] = {"text": text, "conf": oconf, "box": (px1, py1, px2, py2),
                                           "ts": time.time(), "frame": frame_idx}
                        if oconf >= args.min_ocr_conf:
                            plates_read += 1
                            read_log.record(tid, text, oconf, frame_idx, plate_crop)
                            if improved:
                                logger.info("plate id=%d -> %s (conf=%.2f)", tid, text, oconf)

                # Forget stale tracks so the cache doesn't grow without bound.
                if frame_idx % 30 == 0 and args.forget_frames > 0:
                    stale = [t for t, seen in track_last_seen.items() if frame_idx - seen > args.forget_frames]
                    for t in stale:
                        track_best.pop(t, None)
                        track_last_seen.pop(t, None)

                step_once = False

            if args.show:
                if frame is not None:
                    disp = frame.copy()
                    _draw_overlay(disp, tracks, track_best, roi_points, len(tracks), plates_read,
                                  paused=paused, frame_idx=displayed_idx, total_frames=total_frames,
                                  seekable=seekable)
                    if seekable and total_frames > 1 and displayed_idx >= 0:
                        trackbar_guard["sync"] = False
                        cv2.setTrackbarPos("Frame", win, max(0, min(displayed_idx, total_frames - 1)))
                        trackbar_guard["sync"] = True
                    if args.save_video:
                        if writer is None:
                            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                            writer = cv2.VideoWriter(args.save_video, fourcc, src_fps,
                                                     (disp.shape[1], disp.shape[0]))
                        writer.write(disp)
                    cv2.imshow(win, disp)

                key = cv2.waitKey(30 if paused else 1) & 0xFF
                if key == ord("q"):
                    break
                elif key == ord(" "):
                    paused = not paused
                    logger.info("%s", "Paused" if paused else "Resumed")
                elif key == ord("n"):
                    step_once = True
                elif key == ord("p") and seekable:
                    pending_seek["idx"] = max(0, displayed_idx - 1)
                    step_once = True
                elif key == ord("g") and seekable:
                    cv2.imshow(win, disp)
                    target = prompt_goto_frame(displayed_idx, total_frames)
                    if target is not None:
                        pending_seek["idx"] = target
                        step_once = True
                elif key == ord("0") and seekable:
                    pending_seek["idx"] = 0
                    step_once = True
                elif key == ord("$") and seekable and total_frames > 0:
                    pending_seek["idx"] = total_frames - 1
                    step_once = True
            elif frame is not None and args.save_video:
                if writer is None:
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    writer = cv2.VideoWriter(args.save_video, fourcc, src_fps, (frame.shape[1], frame.shape[0]))
                disp = frame.copy()
                _draw_overlay(disp, tracks, track_best, roi_points, len(tracks), plates_read)
                writer.write(disp)
    finally:
        cap.release()
        if writer is not None:
            writer.release()
        if args.show:
            cv2.destroyAllWindows()
        read_log.close()
        logger.info("Stopped. unique plates cached=%d, accepted reads=%d", len(track_best), plates_read)


if __name__ == "__main__":
    main()
