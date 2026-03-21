# Smart Traffic Light

Python3 project with **vehicle detection and tracking** (in `vehicle/`). Run from the **project root**.

---

## Vehicle detection (`vehicle/`)

YOLOv8, SORT tracking, lane ROI, counting, speed, stop line / red light violations. Config in `vehicle/config.py`.

### Setup

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Run

```bash
python vehicle/main.py
```

- **q** in the OpenCV window to quit.
- Edit `vehicle/config.py` for video path, ROI, count line, stop line, and feature flags.

### Calibrate ROI / lines

```bash
python vehicle/calibrate_roi.py
# or: python vehicle/calibrate_roi.py path/to/video.mp4
```

Use keys 1/2/3 for ROI polygon, count line, stop line; **Enter** prints a snippet to paste into `vehicle/config.py`.

### Config

See `vehicle/config.py`: `VIDEO_SOURCE`, `LANE_ROI_POINTS`, `LANE_COUNT_LINE`, `STOP_LINE`, `RED_LIGHT_IS_ON`, and all `ENABLE_*` / `DRAW_*` flags.

---

## Project layout

```
smart-traffic-light/
  vehicle/
    config.py       # vehicle detection config
    main.py         # run pipeline
    calibrate_roi.py
    sort_tracker.py
  videos/           # put videos here (or set path in config)
  requirements.txt
  README.md
```

Videos: use `videos/` at project root and set `VIDEO_SOURCE = "videos/your.mp4"` in `vehicle/config.py`, or use an absolute path.
