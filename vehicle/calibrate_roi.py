#!/usr/bin/env python3
"""
Interactive tool to get pixel (x, y) coordinates from your video for:
  LANE_ROI_POINTS, LANE_COUNT_LINE, STOP_LINE.
Run from project root: python vehicle/calibrate_roi.py [video_path]
Uses config.VIDEO_SOURCE if no path given.
"""

import sys
from pathlib import Path

import cv2
import numpy as np

try:
    import config as cfg
    DEFAULT_VIDEO = getattr(cfg, "VIDEO_SOURCE", "0")
except Exception:
    DEFAULT_VIDEO = "0"


def main():
    video_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VIDEO
    if isinstance(video_path, str) and not video_path.startswith(("rtsp://", "/")) and video_path != "0":
        root = Path(__file__).resolve().parent.parent
        candidate = root / video_path
        if candidate.exists():
            video_path = str(candidate)
    if video_path == "0" or (isinstance(video_path, str) and video_path.isdigit()):
        cap = cv2.VideoCapture(int(video_path))
    else:
        cap = cv2.VideoCapture(str(video_path))

    if not cap.isOpened():
        print("Could not open:", video_path)
        return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    frame_idx = [0]

    def read_frame(i):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, f = cap.read()
        return ret, f

    ret, frame = read_frame(0)
    if not ret or frame is None:
        print("Could not read first frame")
        return

    roi_points = []
    count_line = []
    stop_line = []
    mode = "roi"
    mouse_xy = [0, 0]

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_MOUSEMOVE:
            mouse_xy[0], mouse_xy[1] = x, y
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if mode == "roi":
            roi_points.append((x, y))
            print(f"  ROI point {len(roi_points)}: ({x}, {y})")
        elif mode == "count":
            if len(count_line) < 2:
                count_line.append((x, y))
                print(f"  Count line point {len(count_line)}: ({x}, {y})")
        elif mode == "stop":
            if len(stop_line) < 2:
                stop_line.append((x, y))
                print(f"  Stop line point {len(stop_line)}: ({x}, {y})")

    win = "Calibrate ROI – 1:ROI 2:Count 3:Stop n/p:frame c:clear r:reset Enter:print q:quit"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)

    while True:
        ret, img = read_frame(frame_idx[0])
        if not ret or img is None:
            img = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(img, "No frame", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        else:
            img = img.copy()

        if len(roi_points) >= 2:
            pts = np.array(roi_points, dtype=np.int32)
            cv2.polylines(img, [pts], len(roi_points) >= 3, (0, 255, 255), 2)
            for i, (px, py) in enumerate(roi_points):
                cv2.circle(img, (px, py), 6, (0, 255, 255), -1)
                cv2.putText(img, str(i + 1), (px + 8, py), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        if len(count_line) >= 1:
            cv2.circle(img, count_line[0], 6, (0, 255, 0), -1)
            cv2.putText(img, "C1", (count_line[0][0] + 8, count_line[0][1]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        if len(count_line) >= 2:
            cv2.line(img, count_line[0], count_line[1], (0, 255, 0), 2)
            cv2.circle(img, count_line[1], 6, (0, 255, 0), -1)
            cv2.putText(img, "C2", (count_line[1][0] + 8, count_line[1][1]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        if len(stop_line) >= 1:
            cv2.circle(img, stop_line[0], 6, (0, 0, 255), -1)
            cv2.putText(img, "S1", (stop_line[0][0] + 8, stop_line[0][1]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        if len(stop_line) >= 2:
            cv2.line(img, stop_line[0], stop_line[1], (0, 0, 255), 2)
            cv2.circle(img, stop_line[1], 6, (0, 0, 255), -1)
            cv2.putText(img, "S2", (stop_line[1][0] + 8, stop_line[1][1]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

        cv2.putText(img, f"x,y: ({mouse_xy[0]}, {mouse_xy[1]})", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
        cv2.putText(img, f"Frame: {frame_idx[0] + 1}/{total_frames}", (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
        cv2.putText(img, f"Mode: {mode} (1=ROI 2=Count 3=Stop)", (10, 84), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 2)

        cv2.imshow(win, img)
        key = cv2.waitKey(1) & 0xFF

        if key == ord("q"):
            break
        if key == ord("1"):
            mode = "roi"
            print("Mode: ROI polygon (click points in order)")
        if key == ord("2"):
            mode = "count"
            print("Mode: Count line (click 2 points)")
        if key == ord("3"):
            mode = "stop"
            print("Mode: Stop line (click 2 points)")
        if key == ord("n"):
            frame_idx[0] = min(frame_idx[0] + 1, total_frames - 1)
            print("Frame:", frame_idx[0] + 1)
        if key == ord("p"):
            frame_idx[0] = max(0, frame_idx[0] - 1)
            print("Frame:", frame_idx[0] + 1)
        if key == ord("c"):
            if mode == "roi":
                roi_points.clear()
                print("Cleared ROI points")
            elif mode == "count":
                count_line.clear()
                print("Cleared count line")
            elif mode == "stop":
                stop_line.clear()
                print("Cleared stop line")
        if key == ord("r"):
            roi_points.clear()
            count_line.clear()
            stop_line.clear()
            print("Reset all points")
        if key in (13, 10):
            print("\n" + "=" * 60)
            print("Copy the following into vehicle/config.py:\n")
            if roi_points:
                print("LANE_ROI_POINTS = [")
                for p in roi_points:
                    print(f"    ({p[0]}, {p[1]}),")
                print("]")
            if count_line:
                print("\nLANE_COUNT_LINE = [")
                for p in count_line:
                    print(f"    ({p[0]}, {p[1]}),")
                print("]")
            if stop_line:
                print("\nSTOP_LINE = [")
                for p in stop_line:
                    print(f"    ({p[0]}, {p[1]}),")
                print("]")
            print("=" * 60 + "\n")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
