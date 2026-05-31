#!/usr/bin/env python3
"""
Interactive tool to get pixel (x, y) coordinates from a video for:
  LANE_ROI_POINTS, LANE_COUNT_LINE, STOP_LINE.

Sources: video file, RTSP URL, or webcam.

Examples (from project root):
  python vehicle/calibrate_roi.py                          # config.VIDEO_SOURCE
  python vehicle/calibrate_roi.py videos/foo.mp4
  python vehicle/calibrate_roi.py videos/foo.mp4 --frame 120    # start at frame 120 (1-based)
  python vehicle/calibrate_roi.py --rtsp rtsp://user:pass@host/stream
  python vehicle/calibrate_roi.py -w                       # default webcam (index 0)
  python vehicle/calibrate_roi.py -w 1                     # second camera
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    import config as cfg
    DEFAULT_VIDEO = getattr(cfg, "VIDEO_SOURCE", "0")
except Exception:
    DEFAULT_VIDEO = "0"

_STREAM_PREFIXES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")


def _is_stream_url(source: str) -> bool:
    return source.lower().startswith(_STREAM_PREFIXES)


def resolve_source(
    path_arg: str | None,
    webcam_index: int | None,
    rtsp_url: str | None,
) -> str | int:
    """Pick exactly one source from CLI flags / positional arg / config default."""
    chosen = sum(x is not None for x in (path_arg, webcam_index, rtsp_url))
    if chosen > 1:
        raise SystemExit("Use only one of: video file path, --webcam, or --rtsp.")

    if webcam_index is not None:
        return webcam_index
    if rtsp_url is not None:
        return rtsp_url.strip()
    if path_arg is not None:
        return path_arg.strip()
    return DEFAULT_VIDEO


def normalize_for_opencv(source: str | int) -> tuple[str | int, str, bool]:
    """
    Return (opencv_source, display_label, seekable).
    seekable is True only for local video files (frame scrubbing with n/p).
    """
    if isinstance(source, int):
        return source, f"webcam:{source}", False

    s = str(source).strip()
    if s == "0" or (s.isdigit() and not Path(s).exists()):
        idx = int(s)
        return idx, f"webcam:{idx}", False

    if _is_stream_url(s):
        return s, s, False

    if not s.startswith("/"):
        candidate = _PROJECT_ROOT / s
        if candidate.exists():
            s = str(candidate.resolve())

    if Path(s).is_file():
        return s, s, True

    return s, s, False


def open_capture(source: str | int) -> tuple[cv2.VideoCapture, str, bool]:
    opencv_src, label, seekable = normalize_for_opencv(source)
    cap = cv2.VideoCapture(opencv_src)
    if isinstance(opencv_src, str) and _is_stream_url(opencv_src):
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        raise SystemExit(f"Could not open video source: {label}")
    return cap, label, seekable


def clamp_frame_index(index: int, total_frames: int) -> int:
    """Clamp 0-based frame index to [0, total_frames - 1]."""
    if total_frames < 1:
        return 0
    return max(0, min(index, total_frames - 1))


def display_to_index(display_frame: int, total_frames: int) -> int:
    """Convert 1-based frame number (UI) to clamped 0-based index."""
    return clamp_frame_index(display_frame - 1, total_frames)


def index_to_display(index: int) -> int:
    return index + 1


def prompt_goto_frame(current_index: int, total_frames: int) -> int | None:
    """Ask in the terminal for a 1-based frame number; None if cancelled."""
    lo, hi = 1, total_frames
    cur = index_to_display(current_index)
    try:
        raw = input(f"\nGo to frame ({lo}–{hi}, current {cur}). Enter number or blank to cancel: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        print("Cancelled.")
        return None
    if not raw.isdigit():
        print(f"Invalid input {raw!r} — need a whole number between {lo} and {hi}.")
        return None
    n = int(raw)
    if n < lo or n > hi:
        print(f"Out of range: {n} (use {lo}–{hi}).")
        return None
    return display_to_index(n, total_frames)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Calibrate lane ROI, count line, and stop line on a video frame.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "source",
        nargs="?",
        help="Video file path (relative to project root ok). Omit to use config.VIDEO_SOURCE.",
    )
    p.add_argument(
        "-w",
        "--webcam",
        nargs="?",
        const=0,
        type=int,
        metavar="INDEX",
        help="Use webcam; optional index (default 0). Example: -w or -w 1",
    )
    p.add_argument(
        "--rtsp",
        metavar="URL",
        help="RTSP/HTTP stream URL (e.g. rtsp://user:pass@host/...)",
    )
    p.add_argument(
        "-f",
        "--frame",
        type=int,
        metavar="N",
        help="Start at frame N (1-based; seekable video files only)",
    )
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        raw = resolve_source(args.source, args.webcam, args.rtsp)
    except SystemExit as e:
        print(e, file=sys.stderr)
        sys.exit(2)

    cap, label, seekable = open_capture(raw)
    print(f"Opened: {label}  (seekable={'yes' if seekable else 'no — live stream'})")
    if args.frame is not None and not seekable:
        print("Warning: --frame ignored (only works with seekable video files).", file=sys.stderr)

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if seekable else 0
    if seekable and total_frames < 1:
        total_frames = 1

    frame_idx = [0]
    if seekable and args.frame is not None:
        frame_idx[0] = display_to_index(args.frame, total_frames)
        print(f"Start at frame {index_to_display(frame_idx[0])} / {total_frames}")

    live_frame = [None]
    trackbar_sync = [True]  # skip trackbar callback when we set position from code

    def read_frame_at(index: int) -> tuple[bool, np.ndarray | None]:
        if not seekable:
            ret, f = cap.read()
            if ret and f is not None:
                live_frame[0] = f
            return ret, live_frame[0]

        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ret, f = cap.read()
        return ret, f

    ret, frame = read_frame_at(0)
    if not ret or frame is None:
        print("Could not read first frame")
        cap.release()
        return

    roi_points: list[tuple[int, int]] = []
    count_line: list[tuple[int, int]] = []
    stop_line: list[tuple[int, int]] = []
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

    win = "Calibrate ROI – 1:ROI 2:Count 3:Stop g:goto n/p:frame c:clear r:reset Enter:print q:quit"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)

    def sync_trackbar() -> None:
        if not seekable or total_frames < 2:
            return
        trackbar_sync[0] = False
        cv2.setTrackbarPos("Frame", win, frame_idx[0])
        trackbar_sync[0] = True

    def on_trackbar(pos: int) -> None:
        if trackbar_sync[0]:
            frame_idx[0] = clamp_frame_index(pos, total_frames)

    if seekable and total_frames > 1:
        cv2.createTrackbar("Frame", win, frame_idx[0], total_frames - 1, on_trackbar)
        print("Seek: trackbar, g=go to frame #, n/p=step, -f N on CLI")

    while True:
        if seekable:
            ret, img = read_frame_at(frame_idx[0])
        else:
            ret, img = read_frame_at(0)

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
        if seekable:
            cv2.putText(
                img,
                f"Frame: {frame_idx[0] + 1}/{total_frames}",
                (10, 56),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 0),
                2,
            )
        else:
            cv2.putText(img, "Live (n = next frame)", (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
        if seekable:
            cv2.putText(img, "g = go to frame #", (10, 112), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 180, 180), 1)
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
        if key == ord("g") and seekable:
            cv2.imshow(win, img)
            new_idx = prompt_goto_frame(frame_idx[0], total_frames)
            if new_idx is not None:
                frame_idx[0] = new_idx
                sync_trackbar()
                print("Frame:", index_to_display(frame_idx[0]))
        if key == ord("n"):
            if seekable:
                frame_idx[0] = min(frame_idx[0] + 1, total_frames - 1)
                sync_trackbar()
                print("Frame:", index_to_display(frame_idx[0]))
            else:
                read_frame_at(0)
        if key == ord("p") and seekable:
            frame_idx[0] = max(0, frame_idx[0] - 1)
            sync_trackbar()
            print("Frame:", index_to_display(frame_idx[0]))
        if key == ord("0") and seekable:
            frame_idx[0] = 0
            sync_trackbar()
            print("Frame: 1 (first)")
        if key == ord("$") and seekable:
            frame_idx[0] = total_frames - 1
            sync_trackbar()
            print(f"Frame: {total_frames} (last)")
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
