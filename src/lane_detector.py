#!/usr/bin/env python3
"""
lane_detector.py — edge vehicle detector that publishes live lane demand to the coordinator over MQTT.

Runs YOLOv8 (+ SORT tracking) on a video file, RTSP stream, or webcam, counts the vehicles
currently inside a lane ROI, and publishes that count to the coordinator as:

    <count_key>:<value>:<unix_timestamp>      (topic: config.MQTT_TOPIC, default "vehicle_counts")

The coordinator treats the value as live demand for the lane (it decays to 0 after a few seconds
with no update), so this script publishes the current ROI occupancy on a fixed interval.

The lane name picks the payload key the coordinator listens for. Coordinator trigger keys:
    south_left  south_right  north_left  north_right  narrow_centre   (+ pedestrian_narrow_passage)
e.g. `--lane south_right` publishes key `south_right_vehicle_count`.

Examples (run from src/ or the repo root):
  # RTSP camera, ROI inline, publish south_right_vehicle_count
  python3 src/lane_detector.py --lane south_right \
      --rtsp "rtsp://admin:pass@192.168.0.5:554/cam/realmonitor?channel=3&subtype=1" \
      --roi "9,476 1386,216 1689,326 1432,1278"

  # Webcam 0, ROI/lines from a JSON file (same keys as vehicle/config.py)
  python3 src/lane_detector.py --lane north_left -w 0 --points-file lane_points/pole_1_right.json --show

  # Video file, just test what would be published (no MQTT)
  python3 src/lane_detector.py --lane narrow_centre \
      --source videos/real_footage/pole_1_right.mp4 --dry-run --show

Points files are JSON, e.g.:
  { "LANE_ROI_POINTS": [[9,476],[1386,216],[1689,326]],
    "LANE_COUNT_LINE": [[1298,235],[1679,364]],
    "STOP_LINE":       [[21,575],[1423,1291]] }

Window controls (with --show):
  space = pause/resume,  n = step forward,  p = step back (files),
  g = go to frame # (files),  0 = first,  $ = last,  q = quit.
  A "Frame" trackbar appears for seekable video files.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# Resolve imports whether run as `python src/lane_detector.py` (repo root) or from inside src/.
_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import config  # noqa: E402  (after sys.path setup)

logger = logging.getLogger("lane_detector")

# YOLOv8 (required for detection)
try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None

# SORT tracker lives in the sibling vehicle/ package; import without shadowing src/config.py.
Sort = None
iou_batch = None
_VEHICLE_DIR = _PROJECT_ROOT / "vehicle"
if _VEHICLE_DIR.is_dir() and str(_VEHICLE_DIR) not in sys.path:
    sys.path.append(str(_VEHICLE_DIR))
try:
    from sort_tracker import Sort, iou_batch  # type: ignore
except Exception:  # tracker optional — fall back to per-frame detections
    Sort = None
    iou_batch = None

# Coordinator's known demand keys (for a friendly warning if --lane doesn't match).
KNOWN_COUNT_KEYS = {
    "north_right_vehicle_count",
    "north_left_vehicle_count",
    "south_left_vehicle_count",
    "south_right_vehicle_count",
    "narrow_centre_vehicle_count",
    "pedestrian_narrow_passage_count",
}

# COCO class ids for vehicles: 1=bicycle 2=car 3=motorcycle 5=bus 7=truck
DEFAULT_VEHICLE_CLASS_IDS = [1, 2, 3, 5, 7]
_STREAM_PREFIXES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def point_inside_polygon(px: float, py: float, polygon: list) -> bool:
    """Ray-casting point-in-polygon test. polygon = [(x, y), ...]."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def side_of_line(px: float, py: float, line: list) -> float:
    """Signed side of a point relative to a 2-point line (>0 / <0 / 0 on line)."""
    (x1, y1), (x2, y2) = line[0], line[1]
    return (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)


def crossed_line(prev_side: float, curr_side: float, direction: str) -> bool:
    """Did the point cross the line between prev and curr? direction: both/up/down/left/right."""
    if prev_side == 0 or curr_side == 0:
        return False
    if direction == "both":
        return (prev_side > 0) != (curr_side > 0)
    crossed_forward = prev_side > 0 and curr_side < 0
    crossed_back = prev_side < 0 and curr_side > 0
    if direction in ("up", "down"):
        return (direction == "up" and crossed_forward) or (direction == "down" and crossed_back)
    if direction in ("left", "right"):
        return (direction == "right" and crossed_forward) or (direction == "left" and crossed_back)
    return False


# ---------------------------------------------------------------------------
# CLI parsing for points
# ---------------------------------------------------------------------------

def parse_points(text: str | None) -> list:
    """Parse "x1,y1 x2,y2; x3,y3" (whitespace and/or ';' separated) into [(x, y), ...]."""
    if not text:
        return []
    tokens = text.replace(";", " ").split()
    pts = []
    for tok in tokens:
        if "," not in tok:
            raise ValueError(f"Bad point '{tok}' — expected 'x,y'")
        xs, ys = tok.split(",", 1)
        pts.append((int(float(xs)), int(float(ys))))
    return pts


def load_points_file(path: str) -> dict:
    """Load LANE_ROI_POINTS / LANE_COUNT_LINE / STOP_LINE from a JSON file."""
    data = json.loads(Path(path).read_text())
    out = {}
    for key in ("LANE_ROI_POINTS", "LANE_COUNT_LINE", "STOP_LINE"):
        val = data.get(key)
        if val:
            out[key] = [(int(p[0]), int(p[1])) for p in val]
    return out


# ---------------------------------------------------------------------------
# Source resolution (file / rtsp / webcam)
# ---------------------------------------------------------------------------

def _is_stream_url(s: str) -> bool:
    return s.lower().startswith(_STREAM_PREFIXES)


def resolve_source(path_arg, webcam_index, rtsp_url):
    """Pick one source from the CLI flags. Returns (opencv_source, label, kind)."""
    chosen = sum(x is not None for x in (path_arg, webcam_index, rtsp_url))
    if chosen > 1:
        raise SystemExit("Use only one of: source path, --webcam, or --rtsp.")

    if webcam_index is not None:
        return int(webcam_index), f"webcam:{webcam_index}", "webcam"
    if rtsp_url is not None:
        return rtsp_url.strip(), rtsp_url.strip(), "stream"
    if path_arg is None:
        raise SystemExit("No source given. Provide a file path, --rtsp URL, or --webcam INDEX.")

    s = str(path_arg).strip()
    if s.isdigit() and not Path(s).exists():
        return int(s), f"webcam:{s}", "webcam"
    if _is_stream_url(s):
        return s, s, "stream"
    if not s.startswith("/"):
        candidate = _PROJECT_ROOT / s
        if candidate.exists():
            s = str(candidate.resolve())
    return s, s, "file"


def open_capture(cv_src, kind):
    cap = cv2.VideoCapture(cv_src)
    if kind == "stream":
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
    return cap


# ---------------------------------------------------------------------------
# MQTT publisher
# ---------------------------------------------------------------------------

class CountPublisher:
    """Publishes '<key>:<value>:<ts>' to the coordinator's MQTT topic (or logs only in dry-run)."""

    def __init__(self, broker, port, username, password, topic, count_key, dry_run=False, client_suffix=""):
        self.topic = topic
        self.count_key = count_key
        self.dry_run = dry_run
        self.sep = getattr(config, "MQTT_PAYLOAD_SEP", ":")
        self._client = None
        if dry_run:
            logger.info("DRY RUN — payloads will be logged, not published")
            return

        from paho.mqtt import client as mqtt  # imported lazily so --dry-run needs no paho

        client_id = f"lane_detector_{count_key}{client_suffix}"
        try:
            self._client = mqtt.Client(client_id=client_id, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):
            self._client = mqtt.Client(client_id=client_id)  # older paho

        if username:
            self._client.username_pw_set(username, password)

        def _on_connect(client, userdata, flags, rc, properties=None):
            ok = (getattr(rc, "value", rc) == 0)
            if ok:
                logger.info("MQTT connected to %s:%s (topic=%s, key=%s)", broker, port, topic, count_key)
            else:
                logger.warning("MQTT connect failed: rc=%s", rc)

        self._client.on_connect = _on_connect
        try:
            self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        except Exception:
            pass
        self._client.connect(broker, port, keepalive=60)
        self._client.loop_start()

    def publish(self, value: int):
        ts = int(time.time())
        payload = f"{self.count_key}{self.sep}{int(value)}{self.sep}{ts}"
        if self.dry_run or self._client is None:
            logger.info("[dry-run] %s -> %s", self.topic, payload)
            return
        self._client.publish(self.topic, payload, qos=0)
        logger.debug("published %s -> %s", self.topic, payload)

    def close(self):
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detections_in_frame(frame, model, class_ids, conf_thr, roi_points):
    """Run YOLO and return (N, 5) array (x1, y1, x2, y2, conf) for vehicles (optionally inside ROI)."""
    results = model(frame, verbose=False)[0]
    boxes = results.boxes
    if boxes is None:
        return np.empty((0, 5))
    out = []
    for i in range(len(boxes)):
        cls_id = int(boxes.cls[i].item())
        if cls_id not in class_ids:
            continue
        conf = float(boxes.conf[i].item())
        if conf < conf_thr:
            continue
        x1, y1, x2, y2 = boxes.xyxy[i].cpu().numpy()
        if roi_points:
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            if not point_inside_polygon(cx, cy, roi_points):
                continue
        out.append([x1, y1, x2, y2, conf])
    return np.array(out) if out else np.empty((0, 5))


def aggregate(samples: list, how: str) -> int:
    if not samples:
        return 0
    if how == "mean":
        return int(round(sum(samples) / len(samples)))
    if how == "last":
        return int(samples[-1])
    return int(max(samples))  # default: peak demand in the window


def prompt_goto_frame(current_idx: int, total_frames: int):
    """Ask in the terminal for a 1-based frame number; returns 0-based index or None if cancelled."""
    hi = total_frames if total_frames > 0 else "?"
    try:
        raw = input(f"\nGo to frame (1-{hi}, current {current_idx + 1}). Number or blank to cancel: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw or not raw.lstrip("-").isdigit():
        return None
    n = int(raw)
    idx = n - 1
    if total_frames > 0:
        idx = max(0, min(idx, total_frames - 1))
    return max(0, idx)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Detect vehicles in a lane and publish live counts to the coordinator over MQTT.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Source
    p.add_argument("source", nargs="?", help="Video file path (relative to repo root ok) or webcam index.")
    p.add_argument("--source", dest="source_opt", help="Same as positional source.")
    p.add_argument("--rtsp", metavar="URL", help="RTSP/HTTP stream URL.")
    p.add_argument("-w", "--webcam", nargs="?", const=0, type=int, metavar="INDEX", help="Webcam index (default 0).")

    # Lane / MQTT routing
    p.add_argument("--lane", required=True, help="Lane name, e.g. south_right (-> south_right_vehicle_count).")
    p.add_argument("--count-key", help="Exact MQTT payload key. Default '<lane>_vehicle_count'.")
    p.add_argument("--topic", default=getattr(config, "MQTT_TOPIC", "vehicle_counts"), help="MQTT topic.")

    # ROI / lines
    p.add_argument("--roi", help='Lane ROI polygon, e.g. "x1,y1 x2,y2 x3,y3".')
    p.add_argument("--count-line", help='Count line, two points "x1,y1 x2,y2".')
    p.add_argument("--stop-line", help='Stop line, two points "x1,y1 x2,y2".')
    p.add_argument("--count-direction", default="both", choices=["both", "up", "down", "left", "right"],
                   help="Count-line crossing direction (display metric only).")
    p.add_argument("--points-file", help="JSON with LANE_ROI_POINTS / LANE_COUNT_LINE / STOP_LINE.")

    # MQTT connection
    p.add_argument("--broker", default=getattr(config, "MQTT_BROKER", "localhost"))
    p.add_argument("--port", type=int, default=getattr(config, "MQTT_PORT", 1883))
    p.add_argument("--username", default=getattr(config, "MQTT_USERNAME", None))
    p.add_argument("--password", default=getattr(config, "MQTT_PASSWORD", None))

    # Detection / model
    p.add_argument("--model", default=getattr(config, "YOLO_MODEL", "yolov8n.pt"), help="YOLOv8 weights.")
    p.add_argument("--conf", type=float, default=0.5, help="Confidence threshold.")
    p.add_argument("--classes", default=",".join(map(str, DEFAULT_VEHICLE_CLASS_IDS)),
                   help="Comma-separated COCO class ids (default vehicles).")
    p.add_argument("--detect-every-n", type=int, default=2, help="Run YOLO every N frames (SORT fills gaps).")
    p.add_argument("--no-track", action="store_true", help="Disable SORT; use raw per-frame detections.")

    # Publishing
    p.add_argument("--interval", type=float, default=1.0, help="Seconds between MQTT publishes.")
    p.add_argument("--aggregate", default="max", choices=["max", "mean", "last"],
                   help="How to aggregate occupancy over the interval (default max = peak demand).")

    # Misc
    p.add_argument("--show", action="store_true", help="Show an OpenCV window (needs a display).")
    p.add_argument("--dry-run", action="store_true", help="Log payloads instead of publishing to MQTT.")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return p


def resolve_count_key(lane: str, explicit_key: str | None) -> str:
    if explicit_key:
        return explicit_key
    lane = lane.strip()
    if lane.endswith("_count"):
        return lane
    return f"{lane}_vehicle_count"


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

    # ROI / lines: points file first, CLI overrides individual lists.
    roi_points: list = []
    count_line: list = []
    stop_line: list = []
    if args.points_file:
        pf = load_points_file(args.points_file)
        roi_points = pf.get("LANE_ROI_POINTS", [])
        count_line = pf.get("LANE_COUNT_LINE", [])
        stop_line = pf.get("STOP_LINE", [])
    if args.roi is not None:
        roi_points = parse_points(args.roi)
    if args.count_line is not None:
        count_line = parse_points(args.count_line)
    if args.stop_line is not None:
        stop_line = parse_points(args.stop_line)

    if count_line and len(count_line) != 2:
        raise SystemExit("--count-line needs exactly 2 points")
    if stop_line and len(stop_line) != 2:
        raise SystemExit("--stop-line needs exactly 2 points")
    if not roi_points:
        logger.warning("No ROI given — counting ALL detected vehicles in the full frame.")

    class_ids = {int(c) for c in str(args.classes).split(",") if c.strip().isdigit()}
    count_key = resolve_count_key(args.lane, args.count_key)
    if count_key not in KNOWN_COUNT_KEYS:
        logger.warning("count key '%s' is not a coordinator trigger key %s — it will be ignored unless added to the coordinator.",
                       count_key, sorted(KNOWN_COUNT_KEYS))

    cv_src, label, kind = resolve_source(args.source or args.source_opt, args.webcam, args.rtsp)
    logger.info("Source: %s (%s) | lane=%s key=%s topic=%s", label, kind, args.lane, count_key, args.topic)

    model = YOLO(args.model)
    use_track = (not args.no_track) and (Sort is not None)
    tracker = Sort(max_age=8, min_hits=2, iou_threshold=0.3) if use_track else None
    if not use_track:
        logger.warning("SORT tracking disabled — counts come from raw detections (less stable).")

    publisher = CountPublisher(
        broker=args.broker, port=args.port, username=args.username, password=args.password,
        topic=args.topic, count_key=count_key, dry_run=args.dry_run, client_suffix=f"_{int(time.time())}",
    )

    running = {"on": True}

    def _stop(signum, frame):
        running["on"] = False
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    cap = open_capture(cv_src, kind)
    if not cap.isOpened():
        publisher.close()
        raise SystemExit(f"Could not open source: {label}")

    # Seeking is only meaningful for a local video file shown in a window.
    seekable = args.show and kind == "file"
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if seekable else 0
    if total_frames < 0:
        total_frames = 0

    win = f"lane_detector — {args.lane}"
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

    samples: list = []
    track_prev_side = {}
    track_prev_side_stop = {}
    crossing_count = 0
    stop_crossing_count = 0
    frame_idx = 0
    displayed_idx = -1
    last_publish = time.time()
    last_dets = np.empty((0, 5))
    read_fail = 0
    paused = False
    step_once = False
    frame = None
    tracks = np.empty((0, 5))
    occupancy = 0

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
                        logger.info("End of file — publishing final 0 and exiting.")
                        publisher.publish(0)
                        break
                    read_fail += 1
                    logger.warning("Frame read failed (%d) — reopening %s in 2s", read_fail, label)
                    cap.release()
                    time.sleep(2.0)
                    cap = open_capture(cv_src, kind)
                    if read_fail % 5 == 0:
                        publisher.publish(0)  # keep coordinator demand fresh while source is down
                    continue

                read_fail = 0
                frame = new_frame
                frame_idx += 1
                displayed_idx = (int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1) if seekable else displayed_idx + 1

                run_yolo = (args.detect_every_n <= 1) or (frame_idx % args.detect_every_n == 1)
                if run_yolo:
                    last_dets = detections_in_frame(frame, model, class_ids, args.conf, roi_points)

                if tracker is not None:
                    tracks = tracker.update(last_dets[:, :5] if len(last_dets) else np.empty((0, 5)))
                else:
                    tracks = np.array([[*d[:4], i] for i, d in enumerate(last_dets)]) if len(last_dets) else np.empty((0, 5))

                # Occupancy = tracked vehicles whose centroid is inside the ROI (or all if no ROI).
                occupancy = 0
                for tr in tracks:
                    cx = (tr[0] + tr[2]) / 2.0
                    cy = (tr[1] + tr[3]) / 2.0
                    if not roi_points or point_inside_polygon(cx, cy, roi_points):
                        occupancy += 1
                    tid = int(tr[4])
                    if count_line:
                        curr = side_of_line(cx, cy, count_line)
                        prev = track_prev_side.get(tid)
                        if prev is not None and crossed_line(prev, curr, args.count_direction):
                            crossing_count += 1
                        track_prev_side[tid] = curr
                    if stop_line:
                        curr_s = side_of_line(cx, cy, stop_line)
                        prev_s = track_prev_side_stop.get(tid)
                        if prev_s is not None and crossed_line(prev_s, curr_s, "both"):
                            stop_crossing_count += 1
                        track_prev_side_stop[tid] = curr_s
                samples.append(occupancy)

                now = time.time()
                if now - last_publish >= args.interval:
                    value = aggregate(samples, args.aggregate)
                    publisher.publish(value)
                    logger.info("lane=%s occupancy=%d (window n=%d, count_line=%d, stop_line=%d)",
                                args.lane, value, len(samples), crossing_count, stop_crossing_count)
                    samples.clear()
                    last_publish = now

                step_once = False

            if args.show:
                if frame is not None:
                    disp = frame.copy()
                    _draw_overlay(disp, tracks, roi_points, count_line, stop_line,
                                  occupancy, count_key, crossing_count, stop_crossing_count,
                                  paused=paused, frame_idx=displayed_idx, total_frames=total_frames,
                                  seekable=seekable)
                    if seekable and total_frames > 1 and displayed_idx >= 0:
                        trackbar_guard["sync"] = False
                        cv2.setTrackbarPos("Frame", win, max(0, min(displayed_idx, total_frames - 1)))
                        trackbar_guard["sync"] = True
                    cv2.imshow(win, disp)

                key = cv2.waitKey(30 if paused else 1) & 0xFF
                if key == ord("q"):
                    break
                elif key == ord(" "):
                    paused = not paused
                    if not paused:
                        last_publish = time.time()  # avoid a burst publish after a long pause
                    logger.info("%s", "Paused" if paused else "Resumed")
                elif key == ord("n"):
                    step_once = True  # advance exactly one frame (also useful while paused)
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
    finally:
        cap.release()
        if args.show:
            cv2.destroyAllWindows()
        publisher.close()
        logger.info("Stopped. count_line crossings=%d, stop_line crossings=%d", crossing_count, stop_crossing_count)


# BGR colors chosen for visibility on road footage.
_GREEN = (0, 255, 0)
_BLUE = (255, 128, 0)
_CYAN = (255, 255, 0)
_RED = (0, 0, 255)


def _label(frame, text, org, color, scale=0.7, thickness=2):
    """Draw text with a dark background box so green/blue labels stay readable on any frame."""
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    x, y = org
    cv2.rectangle(frame, (x - 3, y - th - 5), (x + tw + 3, y + base + 2), (20, 20, 20), -1)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def _draw_overlay(frame, tracks, roi_points, count_line, stop_line, occupancy, count_key,
                  crossings, stop_crossings, paused=False, frame_idx=-1, total_frames=0, seekable=False):
    for tr in tracks:
        x1, y1, x2, y2 = int(tr[0]), int(tr[1]), int(tr[2]), int(tr[3])
        cv2.rectangle(frame, (x1, y1), (x2, y2), _GREEN, 2)
        _label(frame, f"ID:{int(tr[4])}", (x1, max(16, y1 - 6)), _GREEN, scale=0.5, thickness=1)
    if roi_points and len(roi_points) >= 2:
        cv2.polylines(frame, [np.array(roi_points, dtype=np.int32)], len(roi_points) >= 3, _CYAN, 2)
    if count_line and len(count_line) == 2:
        cv2.line(frame, tuple(map(int, count_line[0])), tuple(map(int, count_line[1])), _BLUE, 2)
    if stop_line and len(stop_line) == 2:
        cv2.line(frame, tuple(map(int, stop_line[0])), tuple(map(int, stop_line[1])), _RED, 2)

    _label(frame, f"{count_key}: {occupancy}", (10, 34), _GREEN, scale=0.8, thickness=2)
    _label(frame, f"count line crossings: {crossings}", (10, 66), _BLUE, scale=0.6, thickness=2)
    _label(frame, f"stop line crossings: {stop_crossings}", (10, 92), _GREEN, scale=0.6, thickness=2)

    if seekable and frame_idx >= 0:
        total = total_frames if total_frames > 0 else "?"
        _label(frame, f"frame {frame_idx + 1}/{total}", (10, 118), _CYAN, scale=0.55, thickness=1)
    if paused:
        _label(frame, "PAUSED  (space=resume  n/p=step  g=goto)", (10, 144), _RED, scale=0.55, thickness=2)


if __name__ == "__main__":
    main()
