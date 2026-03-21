#!/usr/bin/env python3
"""
Smart Traffic Light: vehicle detection, license plate recognition, lane ROI,
vehicle counting, speed estimation, and stop line / red light violation detection.
Configure all behavior in config.py.
Run from project root: python vehicle/main.py
"""

from pathlib import Path

import cv2
import numpy as np

import config as cfg
from sort_tracker import Sort, iou_batch

# Optional: YOLOv8
try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None

# Optional: EasyOCR (only when license plate recognition is enabled)
EasyOCR = None
if cfg.ENABLE_LICENSE_PLATE_RECOGNITION:
    try:
        import easyocr
        EasyOCR = easyocr
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def point_inside_polygon(px: float, py: float, polygon: list) -> bool:
    """Check if point (px, py) is inside polygon. polygon = [(x,y), ...]."""
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


def centroid_inside_roi(bbox_xyxy: np.ndarray, roi_points: list) -> bool:
    """bbox_xyxy: (x1,y1,x2,y2). Return True if centroid is inside ROI polygon."""
    cx = (bbox_xyxy[0] + bbox_xyxy[2]) / 2
    cy = (bbox_xyxy[1] + bbox_xyxy[3]) / 2
    return point_inside_polygon(cx, cy, roi_points)


def side_of_line(px: float, py: float, line: list) -> float:
    """
    Line from (x1,y1) to (x2,y2). Returns signed value: >0 one side, <0 other side, 0 on line.
    Uses cross product (x2-x1)*(py-y1) - (y2-y1)*(px-x1).
    """
    x1, y1 = line[0]
    x2, y2 = line[1]
    return (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)


def crossed_line(prev_side: float, curr_side: float, direction: str) -> bool:
    """
    Did we cross the line from prev to curr? direction: "both", "up", "down", "left", "right".
    """
    if prev_side == 0 or curr_side == 0:
        return False
    if direction == "both":
        return (prev_side > 0) != (curr_side > 0)
    if direction == "up" or direction == "down":
        crossed_forward = prev_side > 0 and curr_side < 0
        crossed_back = prev_side < 0 and curr_side > 0
        return (direction == "up" and crossed_forward) or (direction == "down" and crossed_back)
    if direction == "left" or direction == "right":
        crossed_forward = prev_side > 0 and curr_side < 0
        crossed_back = prev_side < 0 and curr_side > 0
        return (direction == "right" and crossed_forward) or (direction == "left" and crossed_back)
    return False


def pixel_distance(x1: float, y1: float, x2: float, y2: float) -> float:
    return np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

# COCO class id -> display name for count labels
VEHICLE_CLASS_NAMES = {1: "Bicycle", 2: "Car", 3: "Motorcycle", 5: "Bus", 7: "Truck"}


def get_vehicle_detections(frame: np.ndarray, model) -> np.ndarray:
    """Run YOLO and return (N, 6) array (x1, y1, x2, y2, conf, class_id) for vehicles only."""
    if model is None:
        return np.empty((0, 6))
    results = model(frame, verbose=False)[0]
    boxes = results.boxes
    if boxes is None:
        return np.empty((0, 6))
    out = []
    for i in range(len(boxes)):
        cls_id = int(boxes.cls[i].item())
        if cls_id not in cfg.VEHICLE_CLASS_IDS:
            continue
        conf = float(boxes.conf[i].item())
        if conf < cfg.CONFIDENCE_THRESHOLD:
            continue
        x1, y1, x2, y2 = boxes.xyxy[i].cpu().numpy()
        if cfg.ENABLE_LANE_ROI and not centroid_inside_roi(np.array([x1, y1, x2, y2]), cfg.LANE_ROI_POINTS):
            continue
        out.append([x1, y1, x2, y2, conf, cls_id])
    return np.array(out) if out else np.empty((0, 6))


def run_ocr_on_crop(frame: np.ndarray, bbox_xyxy: np.ndarray, reader) -> str:
    """Crop region (optionally expanded for plate), run EasyOCR, return text."""
    if reader is None:
        return ""
    x1, y1, x2, y2 = bbox_xyxy.astype(int)
    w, h = x2 - x1, y2 - y1
    margin = cfg.LICENSE_PLATE_CROP_MARGIN
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    nw, nh = w * margin, h * margin
    x1 = max(0, int(cx - nw / 2))
    y1 = max(0, int(cy - nh / 2))
    x2 = min(frame.shape[1], int(cx + nw / 2))
    y2 = min(frame.shape[0], int(cy + nh / 2))
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return ""
    result = reader.readtext(crop)
    if not result:
        return ""
    best = max(result, key=lambda r: r[2])
    return best[1].strip()


def main():
    if YOLO is None:
        print("Install ultralytics: pip install ultralytics")
        return

    model = YOLO(cfg.YOLO_MODEL)
    tracker = Sort(max_age=5, min_hits=2, iou_threshold=0.3) if cfg.ENABLE_SORT_TRACKING else None

    ocr_reader = None
    if cfg.ENABLE_LICENSE_PLATE_RECOGNITION and EasyOCR is not None:
        ocr_reader = EasyOCR.Reader(cfg.LICENSE_PLATE_LANGUAGES, gpu=False)

    # Resolve video path relative to project root (parent of vehicle/)
    src = cfg.VIDEO_SOURCE
    if isinstance(src, str) and not src.startswith(("rtsp://", "/")) and src != "0":
        root = Path(__file__).resolve().parent.parent
        candidate = root / src
        if candidate.exists():
            src = str(candidate)
    if isinstance(src, str) and (src == "0" or not Path(src).exists()):
        src = int(src) if src == "0" else 0
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        print("Could not open video source:", cfg.VIDEO_SOURCE)
        return

    fps = cfg.VIDEO_FPS or cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    writer = None
    if cfg.OUTPUT_VIDEO_PATH:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(cfg.OUTPUT_VIDEO_PATH, fourcc, fps, (w, h))

    track_prev_side_count = {}
    track_prev_side_stop = {}
    track_prev_pos = {}
    track_speed_buffer = {}
    track_plate_text = {}
    track_class = {}
    vehicle_count = 0
    count_by_class = {cls_id: 0 for cls_id in VEHICLE_CLASS_NAMES}
    violations = set()
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1
        red_light_on = cfg.RED_LIGHT_IS_ON

        n_detect = getattr(cfg, "DETECT_EVERY_N_FRAMES", 1)
        detect_this_frame = cfg.ENABLE_VEHICLE_DETECTION and (
            n_detect == 1 or (frame_idx % n_detect == 1)
        )
        dets = get_vehicle_detections(frame, model) if detect_this_frame else np.empty((0, 6))

        if cfg.ENABLE_SORT_TRACKING and tracker is not None:
            tracks = tracker.update(dets[:, :5] if len(dets) > 0 else np.empty((0, 5)))
        else:
            tracks = np.empty((0, 5))

        if len(tracks) > 0 and len(dets) > 0:
            for tr in tracks:
                tid = int(tr[4])
                iou_row = iou_batch(tr[:4].reshape(1, 4), dets[:, :4])[0]
                best_idx = int(np.argmax(iou_row))
                if iou_row[best_idx] > 0.1:
                    track_class[tid] = int(dets[best_idx, 5])

        dt = 1.0 / fps
        current_lane_detection = 0

        for tr in tracks:
            x1, y1, x2, y2, tid = int(tr[0]), int(tr[1]), int(tr[2]), int(tr[3]), int(tr[4])
            cx = (tr[0] + tr[2]) / 2
            cy = (tr[1] + tr[3]) / 2

            if cfg.ENABLE_LANE_ROI and cfg.LANE_ROI_POINTS:
                if point_inside_polygon(cx, cy, cfg.LANE_ROI_POINTS):
                    current_lane_detection += 1
            else:
                current_lane_detection += 1

            if cfg.ENABLE_LANE_COUNTING and cfg.LANE_COUNT_LINE:
                curr_side = side_of_line(cx, cy, cfg.LANE_COUNT_LINE)
                prev_side = track_prev_side_count.get(tid)
                if prev_side is not None and crossed_line(prev_side, curr_side, cfg.LANE_COUNT_DIRECTION):
                    vehicle_count += 1
                    cls_id = track_class.get(tid)
                    if cls_id is not None and cls_id in count_by_class:
                        count_by_class[cls_id] += 1
                track_prev_side_count[tid] = curr_side

            speed_kmh = None
            if cfg.ENABLE_SPEED_ESTIMATION and cfg.PIXELS_PER_METER > 0:
                prev = track_prev_pos.get(tid)
                if prev is not None:
                    px, py, pframe = prev
                    dframe = frame_idx - pframe
                    if dframe > 0:
                        dist_px = pixel_distance(px, py, cx, cy)
                        dist_m = dist_px / cfg.PIXELS_PER_METER
                        time_sec = dframe / fps
                        if time_sec > 0:
                            speed_ms = dist_m / time_sec
                            speed_kmh = speed_ms * 3.6
                            if tid not in track_speed_buffer:
                                track_speed_buffer[tid] = []
                            track_speed_buffer[tid].append((speed_kmh, frame_idx))
                            n_smooth = cfg.SPEED_SMOOTHING_FRAMES
                            track_speed_buffer[tid] = [x for x in track_speed_buffer[tid] if frame_idx - x[1] <= n_smooth]
                track_prev_pos[tid] = (cx, cy, frame_idx)

            if tid in track_speed_buffer and track_speed_buffer[tid]:
                speed_kmh = np.mean([x[0] for x in track_speed_buffer[tid]])

            curr_stop_side = side_of_line(cx, cy, cfg.STOP_LINE) if cfg.ENABLE_STOP_LINE_DETECTION and cfg.STOP_LINE else None
            if curr_stop_side is not None:
                prev_stop = track_prev_side_stop.get(tid)
                if prev_stop is not None and (prev_stop > 0) != (curr_stop_side > 0):
                    is_violation = cfg.TREAT_ALL_STOP_LINE_CROSSINGS_AS_VIOLATION or (cfg.ENABLE_RED_LIGHT_VIOLATION and red_light_on)
                    if is_violation:
                        violations.add(tid)
                track_prev_side_stop[tid] = curr_stop_side

            if cfg.ENABLE_LICENSE_PLATE_RECOGNITION and ocr_reader is not None and frame_idx % 30 == 0:
                plate_text = run_ocr_on_crop(frame, np.array([x1, y1, x2, y2]), ocr_reader)
                if plate_text:
                    track_plate_text[tid] = plate_text
            plate_text = track_plate_text.get(tid, "")

            color = (0, 0, 255) if tid in violations else (0, 255, 0)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            if cfg.DRAW_TRACK_IDS:
                cv2.putText(frame, f"ID:{tid}", (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            if cfg.DRAW_SPEED and speed_kmh is not None:
                cv2.putText(frame, f"{speed_kmh:.1f} km/h", (x1, y2 + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
            if getattr(cfg, "DRAW_LICENSE_PLATE", True) and plate_text:
                label = plate_text[:20]
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
                by, bx = y1, x1
                cv2.rectangle(frame, (bx, by), (bx + tw + 8, by + th + 6), (40, 40, 40), -1)
                cv2.rectangle(frame, (bx, by), (bx + tw + 8, by + th + 6), color, 1)
                cv2.putText(frame, label, (bx + 4, by + th + 2), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
            if cfg.DRAW_VIOLATIONS and tid in violations:
                cv2.putText(frame, "VIOLATION", (x1, y1 - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        active_ids = {int(t[4]) for t in tracks}
        for tid in list(track_prev_side_count.keys()):
            if tid not in active_ids:
                if track_prev_pos.get(tid) and frame_idx - track_prev_pos[tid][2] > 30:
                    track_prev_side_count.pop(tid, None)
                    track_prev_side_stop.pop(tid, None)
                    track_prev_pos.pop(tid, None)
                    track_speed_buffer.pop(tid, None)
                    track_plate_text.pop(tid, None)
                    track_class.pop(tid, None)

        if cfg.DRAW_ROI and cfg.ENABLE_LANE_ROI and cfg.LANE_ROI_POINTS:
            pts = np.array(cfg.LANE_ROI_POINTS, dtype=np.int32)
            cv2.polylines(frame, [pts], True, (255, 255, 0), 2)
        if cfg.DRAW_COUNT_LINE and cfg.LANE_COUNT_LINE:
            pt1, pt2 = tuple(map(int, cfg.LANE_COUNT_LINE[0])), tuple(map(int, cfg.LANE_COUNT_LINE[1]))
            cv2.line(frame, pt1, pt2, (0, 255, 255), 2)
        if cfg.DRAW_STOP_LINE and cfg.STOP_LINE:
            pt1, pt2 = tuple(map(int, cfg.STOP_LINE[0])), tuple(map(int, cfg.STOP_LINE[1]))
            cv2.line(frame, pt1, pt2, (0, 0, 255), 2)

        cv2.putText(frame, f"Count: {vehicle_count}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)
        cv2.putText(frame, f"In lane: {current_lane_detection}", (10, 58), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)
        y_line = 86
        for cls_id in [1, 3, 7, 2, 5]:
            name = VEHICLE_CLASS_NAMES.get(cls_id, str(cls_id))
            cv2.putText(frame, f"{name}: {count_by_class.get(cls_id, 0)}", (10, y_line), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)
            y_line += 24
        if cfg.ENABLE_RED_LIGHT_VIOLATION:
            cv2.putText(frame, f"Violations: {len(violations)}", (10, y_line), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
            y_line += 32
        cv2.putText(frame, f"Red: {'ON' if red_light_on else 'OFF'}", (10, y_line), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255) if red_light_on else (0, 255, 0), 2)

        if writer is not None:
            writer.write(frame)
        cv2.imshow("Smart Traffic Light", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    if writer is not None:
        writer.release()
    cv2.destroyAllWindows()
    print("Vehicle count:", vehicle_count)
    print("Current in lane (last frame):", current_lane_detection)
    for cls_id in [1, 3, 7, 2, 5]:
        print(f"  {VEHICLE_CLASS_NAMES.get(cls_id, cls_id)}:", count_by_class.get(cls_id, 0))
    print("Violations:", len(violations))


if __name__ == "__main__":
    main()
