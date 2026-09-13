# Smart Traffic Light

A distributed adaptive traffic-light system: cameras detect vehicles, MQTT carries live lane demand, a coordinator picks phases, and Raspberry Pi relay servers drive the lamps. Optional pieces include a live camera proxy, an operator dashboard, a mobile lane-status app, license-plate reading, and YOLOv8 inference on the Orange Pi RK3588 6 TOPS NPU.

**License:** [MIT](#license) — free for any use, including commercial. See [`LICENSE`](LICENSE).

Python 3.8+. Run commands from the **repository root** unless a section says otherwise.

---

## Contents

1. [How it fits together](#how-it-fits-together)
2. [Components](#components)
3. [Install](#install)
4. [Use](#use)
5. [Orange Pi NPU (RKNN)](#orange-pi-npu-rknn)
6. [Configuration reference](#configuration-reference)
7. [Project layout](#project-layout)
8. [License](#license)

Architecture notes (phase selection, relay signing, dashboard routes) also live in [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## How it fits together

```text
lane_detector.py  (×6 cameras, typically 3× Orange Pi 5 Plus)
        │  MQTT  topic vehicle_counts
        │  payload  key:value:unix_timestamp
        ▼
   Mosquitto ──► cordinator.py ── signed HTTPS POST /relay ──► 4× Raspberry Pi
                     │                                           relay_server.py
                     ├── dashboard.py     :5000   operator + public UI
                     └── camera_app.py    :5001   RTSP → MJPEG (optional)

trafficator-app  ── GET /api/geofences, /api/state ──► dashboard
simulator.py     ── same MQTT topic (dev / soak test)
```

**Typical hardware split**

| Machine | Role |
|---------|------|
| Coordinator host | Mosquitto, `cordinator.py`, `dashboard.py`, optional `camera_app.py` |
| 4× Raspberry Pi | `relay_server.py` — GPIO lamps |
| 3× Orange Pi 5 Plus | Lane detection: south L+R, north L+R, narrow centre ×2 |
| Phones | `trafficator-app` lane-status (GPS → geofence) |

---

## Components

| Component | Path | What it does |
|-----------|------|----------------|
| **Shared config** | `src/config.py` | MQTT, phases, relays, cameras, timing, dashboard, RKNN defaults |
| **Coordinator** | `src/cordinator.py` | Subscribes to counts, runs seq1/seq2/seq3, signs relay commands, hosts the dashboard thread |
| **Dashboard** | `src/dashboard.py`, `src/static/` | Public live view at `/`, operator UI at `/trafficator` |
| **Relay server** | `src/relay_server.py` | HTTPS GPIO API on each Pi; verifies `X-Signature` |
| **Camera proxy** | `src/camera_app.py` | RTSP → MJPEG; optional disk recording |
| **Recording cleanup** | `src/camera_retention.py` | Deletes old files under `CAMERA_RECORD_DIR` |
| **Lane detector** | `src/lane_detector.py` | YOLOv8 + SORT → MQTT occupancy for one lane |
| **Plate reader** | `src/plate_reader.py` | Vehicle detect → plate box → OCR (does **not** publish MQTT) |
| **RKNN loader** | `src/rknn_detector.py` | Loads `.rknn` / `.rnn` on the NPU, or `.pt` / `.onnx` via Ultralytics |
| **RKNN export** | `src/rknn_export.py` | `.pt` → ONNX → `.rknn` for RK3588 |
| **Simulator** | `src/simulator.py` | Fake MQTT counts for testing |
| **MQTT helpers** | `src/pub.py`, `src/sub.py`, `src/mqtt_setup.py` | Connectivity tests / optional embedded broker |
| **Relay smoke test** | `src/test_relay.py` | Manual relay HTTP check |
| **CV prototype** | `vehicle/` | Offline detect / count / speed / violations (no MQTT) |
| **Mobile app** | `trafficator-app/` | Expo app: GPS lane + live signal state |
| **ROI JSON** | `src/lane_points/*.json` | Polygons / lines for `lane_detector --points-file` |
| **Weights** | `models/` | `.pt` / `.onnx` / `.rknn` (plate detector ships as `.pt`) |

The coordinator filename is spelled `cordinator.py` (historical). Import and systemd units must use that name.

---

## Install

### 1. Coordinator host (Linux or macOS)

System packages:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv mosquitto mosquitto-clients ffmpeg
# macOS: brew install mosquitto ffmpeg
```

Python env:

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install requests cryptography flask waitress
```

`requirements.txt` covers detection (ultralytics, OpenCV, SORT, MQTT, EasyOCR). Flask / Waitress / cryptography are only needed on the coordinator and relay Pis.

Mosquitto (username/password; do not leave the broker open):

```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd "$MQTT_USERNAME"
sudo tee /etc/mosquitto/conf.d/default.conf >/dev/null <<'EOF'
allow_anonymous false
password_file /etc/mosquitto/passwd
EOF
sudo systemctl restart mosquitto
```

On macOS a local broker is often:

```bash
/opt/homebrew/opt/mosquitto/sbin/mosquitto -c /opt/homebrew/etc/mosquitto/mosquitto.conf
```

TLS + signing keys (already expected under `src/certs/`):

| File | Used by |
|------|---------|
| `src/certs/private.pem` | Coordinator — signs `POST /relay` |
| `src/certs/public.pem` | Each relay Pi — verifies the signature |
| `src/certs/cert.pem`, `key.pem` | Relay HTTPS |

Generate replacements with `openssl` if you rotate keys. Keep the private key only on the coordinator.

### 2. Raspberry Pi (relay)

```bash
sudo apt install -y python3-pip
pip3 install flask waitress cryptography
# copy src/relay_server.py, src/config.py, and src/certs/{public,cert,key}.pem
python3 relay_server.py
```

GPIO uses BCM numbering from `RELAY_PINS`. The process needs permission for those pins (`gpio` group or root).

### 3. Orange Pi 5 Plus (lane detection on the NPU)

```bash
sudo apt install -y python3-pip python3-opencv
pip3 install -r requirements.txt
pip3 install rknn-toolkit-lite2     # inference
# conversion (once, Linux only — not macOS):
pip3 install rknn-toolkit2          # or the official aarch64 wheel
```

You do **not** need Ultralytics / torch on the board if you only load `.rknn` files.

### 4. Trafficator mobile app

```bash
cd trafficator-app
npm install
npx expo start
```

Point `expo.extra.apiBaseUrl` in `trafficator-app/app.json` at the dashboard host (a phone’s `localhost` is the phone, not the coordinator).

---

## Use

Set `DRY_RUN` in `src/config.py` first:

- `True` — no relay HTTP; log-only; home/dev camera URLs and MQTT defaults
- `False` — live Pis, production broker user, site cameras

Prefer `MQTT_USERNAME` / `MQTT_PASSWORD` environment variables over hard-coded secrets.

### Development loop (no hardware)

```bash
# terminal 1 — broker already running
python3 src/simulator.py

# terminal 2
python3 src/cordinator.py
```

Open `http://<DASHBOARD_HOST>:<DASHBOARD_PORT>/` (default port `5000`; on macOS AirPlay often owns 5000 — change `DASHBOARD_PORT`).

Operator UI: `http://<host>:<port>/trafficator`.

### Production coordinator

```bash
python3 src/cordinator.py
python3 -m src.camera_app          # optional live MJPEG
python3 -m src.camera_retention --daemon   # optional recording cleanup
```

### Edge lane detectors (one process per camera)

MQTT keys the coordinator already understands:

| `--lane` | Payload key | Phase |
|----------|-------------|--------|
| `south_left` | `south_left_vehicle_count` | seq1 |
| `south_right` | `south_right_vehicle_count` | seq1 |
| `north_right` | `north_right_vehicle_count` | seq2 |
| `north_left` | `north_left_vehicle_count` | seq3 |
| `narrow_centre` | `narrow_centre_vehicle_count` | blocks new phases until clear |
| `pedestrian_narrow_passage` | `pedestrian_narrow_passage_count` | pedestrian green when > 0 |

```bash
# South board — pin one NPU core per stream
python3 src/lane_detector.py --lane south_left  --rtsp "rtsp://..." \
    --points-file src/lane_points/pole_1_center.json \
    --model models/yolov8n.rknn --npu-cores 0

python3 src/lane_detector.py --lane south_right --rtsp "rtsp://..." \
    --points-file src/lane_points/pole_1_right.json \
    --model models/yolov8n.rknn --npu-cores 1
```

Same pattern on the north board and the narrow-centre board. Add `--dry-run --show` to test a file without MQTT.

Window keys when `--show`: space pause, `n`/`p` step, `g` goto frame, `0` / `$` first/last, `q` quit.

### Suggested 3× Orange Pi 5 Plus split (6 streams)

| Board | Streams | `--npu-cores` |
|-------|---------|----------------|
| South | `south_left`, `south_right` | `0`, `1` |
| North | `north_left`, `north_right` | `0`, `1` |
| Centre | `narrow_centre` × two cameras (same key or two publishers) | `0`, `1` |

Keep the coordinator, dashboard, and `camera_app` off these boards. Expected ~12–15 detect FPS per stream with INT8 `.rknn` and `--detect-every-n 2` on the camera substream (`subtype=1`).

### License plates (analysis only)

```bash
python3 src/plate_reader.py videos/clip.mp4 \
    --plate-model models/license_plate_detector.pt --show --csv plates.csv
```

### Offline CV prototype

```bash
python3 vehicle/main.py
python3 vehicle/calibrate_roi.py path/to/video.mp4
```

Calibrator: `1` ROI polygon, `2` count line, `3` stop line; Enter prints a snippet for `vehicle/config.py`.

### Relay Pi

```bash
python3 src/relay_server.py
```

Listens on HTTPS `:8080`. `GET /status`, `POST /relay` with `X-Signature`. Relays **not** listed in a batch are turned **off**.

---

## Orange Pi NPU (RKNN)

1. Export ONNX anywhere (including macOS):

   ```bash
   python3 src/rknn_export.py --model yolov8n.pt --target rk3588 --dtype fp
   python3 src/rknn_export.py --model models/license_plate_detector.pt --target rk3588 --dtype fp
   ```

   On macOS this writes `models/*.onnx`. RKNN conversion needs **Linux** (the board or an x86_64 PC) and `rknn-toolkit2`.

2. On the Orange Pi:

   ```bash
   python3 src/rknn_export.py --model models/yolov8n.onnx --target rk3588 --dtype fp
   # INT8 (faster): add --dtype i8 --video /path/to/intersection.mp4
   ```

3. Point detectors at `models/yolov8n.rknn`. `.rnn` is accepted as the same format. If `YOLO_MODEL` is still `yolov8n.pt` but a sibling `.rknn` exists and Lite2 is installed, the NPU file is used automatically.

**Pipeline:** CPU letterbox 640 + BGR→RGB → NPU inference (mean 0, std 255) → CPU decode + NMS → SORT.

---

## Configuration reference

### `src/config.py` (production)

Used by the coordinator, dashboard, camera app, relay clients, simulator, lane detector, plate reader, and RKNN export.

#### Run mode and cameras

| Name | Meaning |
|------|---------|
| `DRY_RUN` | `True`: skip relay HTTP, use the “else” camera list and default MQTT user. `False`: live site. |
| `FFMPEG_PATH` | ffmpeg binary. `"ffmpeg"` on the site; a full path is used when `DRY_RUN`. |
| `CAMERA_FEEDS` | List of `{id, label, url, sequence, recording?, record?}`. `sequence` is `seq1`/`seq2`/`seq3` or `None` (manual-mode camera card). `record` overrides disk recording per feed. |
| `CERTS_DIR` | `src/certs/` — signing and TLS files. |

`camera.txt` at the repo root is a human scratchpad. The running apps read **`CAMERA_FEEDS` only**.

#### MQTT

| Name | Meaning |
|------|---------|
| `MQTT_USERNAME` / `MQTT_PASSWORD` | From the environment if set; otherwise the `DRY_RUN` defaults. |
| `MQTT_BROKER` / `MQTT_PORT` | Coordinator and local publishers (`localhost:1883`). |
| `MQTT_BROKER_OVER_NETWORK` / `MQTT_PORT_OVER_NETWORK` | Same shape for LAN clients; currently also `localhost`. Edge Pis should pass `--broker <coordinator-ip>`. |
| `MQTT_BROKER_URL` / `MQTT_BROKER_URL_OVER_NETWORK` | Derived `mqtt://host:port` strings. |
| `MQTT_TOPIC` | Default `vehicle_counts`. |
| `MQTT_PAYLOAD_SEP` | `:` |
| `MQTT_PAYLOAD_PARTS` | `3` — `key:value:unix_timestamp`. |

#### Relays and lamps

| Name | Meaning |
|------|---------|
| `RELAY_SERVERS` | `pi1`–`pi4` → `https://<ip>:8080`. |
| `RELAY_PINS` | Logical relay `1`–`8` → BCM GPIO (`5, 6, 13, 16, 19, 20, 21, 26`). Same map on every Pi. |
| `RELAY_MAPPINGS` | Lamp id (e.g. `pole_1_left_green`) → `(server, relay_id)`. |
| `RELAY_VERIFY_SSL` | `False` accepts the self-signed Pi certs. Set `True` with a real CA. |
| `RELAY_TIMEOUT` | HTTP timeout seconds (default `5`). |
| `TRAFFIC_LIGHT_UNITS` | Physical heads. At most one of red/yellow/green on. Pedestrian has no yellow. |
| `RED_KEYS` | Shared “all vehicle red” lamp list reused as each phase’s `red_keys`. |
| `STARTUP_RED_KEYS` | Lamps applied at coordinator start (currently the yellow set). |
| `IDLE_STANDBY_SECONDS` | After this idle time (default `10 * 60`), all relays off. |

#### Phases and geofences

`PHASES` keys: `seq1`, `seq2`, `seq3`. Each phase:

| Field | Meaning |
|-------|---------|
| `name` | Dashboard label. |
| `green_keys` / `yellow_keys` / `red_keys` | Lamp ids on in that sub-state. Batch-off turns everything else off. |
| `trigger_keys` | MQTT keys that create demand for this phase. |
| `geofence` | `name` plus points `A`–`D` as `[lat, lon]`. Polygon A→B→D→C→A. |

`LANE_GEOFENCES` is derived from `PHASES` for `/api/geofences` (web + mobile).

| Phase | Triggers | Typical cameras |
|-------|----------|-----------------|
| seq1 | south left + south right counts | `south_left`, `south_right` |
| seq2 | north right | `north_right` |
| seq3 | north left | `north_left` |
| (block) | `narrow_centre_vehicle_count` | `narrow_centre_1`, `_2` |
| (ped) | `pedestrian_narrow_passage_count` | independent |

#### Coordinator timing

| Name | Default | Meaning |
|------|---------|---------|
| `COORDINATOR_MESSAGE_MAX_AGE` | `5.0` | Older MQTT samples ignored; counts decay to 0. |
| `COORDINATOR_GREEN_MIN` / `_MAX` | `12` / `55` | Auto green bounds (seconds). |
| `COORDINATOR_GREEN_DENSITY_FACTOR` | `2.5` | Extra green seconds from density. |
| `COORDINATOR_GREEN_MAX_EXTENSION` | `15` | Cap on extra green while cars keep arriving. |
| `COORDINATOR_YELLOW_DURATION` | `3.0` | Yellow. |
| `COORDINATOR_ALL_OFF_GAP` | `4.0` | All-red gap after yellow. |
| `COORDINATOR_NARROW_BLOCK_SLEEP` | `1.5` | Sleep while waiting for the narrow to clear before a new phase. |
| `COORDINATOR_IDLE_SLEEP` | `2.0` | Idle loop pause. |
| `COORDINATOR_NARROW_CENTRE_MAX_WAIT` | `120` | Give up waiting for a clear narrow. |
| `MANUAL_GREEN_DURATION` | `90` | Manual-mode green / same-phase extend. |

Auto mode: pick a phase with demand, least-recently-served, then FCFS, then highest density. Manual mode: only dashboard `trigger_phase`. Pedestrian is on/off from its count, not a timed sequence.

#### Dashboard and host power

| Name | Meaning |
|------|---------|
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | Bind address for the Flask/Waitress UI. |
| `REBOOT_ON_DASHBOARD_LISTEN_FAIL` | If the port cannot bind, turn relays off then reboot. Ignored in `DRY_RUN`. |
| `REBOOT_DELAY_SECONDS` / `REBOOT_COMMAND` | Delay and argv for that reboot. |
| `SHUTDOWN_DELAY_SECONDS` / `SHUTDOWN_COMMAND` | Dashboard Shutdown button. |
| `TRAFFICATOR_USERNAME` / `TRAFFICATOR_PASSWORD` | Operator login at `/trafficator`. |
| `TRAFFICATOR_SECRET_KEY` | Flask session secret — change it. |
| `TRAFFICATOR_LOGIN_MAX_ATTEMPTS` | `3` |
| `TRAFFICATOR_LOCKOUT_MINUTES` | `30` |

#### Camera app and recording

| Name | Meaning |
|------|---------|
| `CAMERA_APP_HOST` / `CAMERA_APP_PORT` | MJPEG server (default `5001`). |
| `CAMERA_APP_BASE_URL` | URL the browser uses (`http://host:port`). |
| `CAMERA_RECORD_ENABLED` | Master switch for disk recording. |
| `CAMERA_RECORD_DIR` | Root folder; files land in `<dir>/<cam_id>/`. |
| `CAMERA_RECORD_QUEUE_MAX` | Recording queue; overflow drops **record** frames only. |
| `CAMERA_RECORD_SEGMENT_MINUTES` | ffmpeg segment length. |
| `CAMERA_RECORD_CONTAINER` | `mp4` or `mkv`. |
| `CAMERA_RECORD_PRESET` | x264 preset (`veryfast`, …). |
| `CAMERA_RECORD_CRF` | Quality (~18–28). |
| `CAMERA_RECORD_RETENTION_DAYS` | Age cutoff for `camera_retention.py`. `0` = do nothing. |
| `CAMERA_RECORD_CLEANUP_INTERVAL_SECONDS` | Daemon sleep between cleanups (default 6 hours). |

#### Detection / ANPR / NPU defaults

Used as CLI defaults by `lane_detector.py`, `plate_reader.py`, and `rknn_export.py`.

| Name | Meaning |
|------|---------|
| `YOLO_MODEL` | `yolov8n.pt` or `models/yolov8n.rknn`. |
| `PLATE_MODEL` | Dedicated plate weights, or `None` to auto-find `models/license_plate_detector.rknn` / `.pt`. |
| `RKNN_TARGET` | `rk3588` (Orange Pi 5 / 5 Plus / 5 Pro). |
| `RKNN_IMGSZ` | Letterbox size, default `640`. |
| `RKNN_NPU_CORES` | `0_1_2` (all cores) or `0` / `1` / `2` / `0_1` / `auto`. Pin cores when several detectors share a board. |
| `PLATE_DETECT_CONF` | Plate-box confidence. |
| `PLATE_OCR_ENGINE` | `easyocr` / `tesseract` / `none`. |
| `PLATE_OCR_LANGS` | e.g. `["en"]`. |
| `PLATE_MIN_OCR_CONF` | Keep a read only above this. |

#### Signing / TLS paths

| Name | File |
|------|------|
| `PRIVATE_KEY_PATH` | `src/certs/private.pem` |
| `PUBLIC_KEY_PATH` | `src/certs/public.pem` |
| `SSL_CERT_PATH` | `src/certs/cert.pem` |
| `SSL_KEY_PATH` | `src/certs/key.pem` |

---

### `lane_detector.py` CLI

| Flag | Default | Meaning |
|------|---------|---------|
| `--lane` | required | Lane name → `<lane>_vehicle_count` |
| `--count-key` | derived | Override the MQTT key |
| `--topic` | `MQTT_TOPIC` | MQTT topic |
| `--rtsp` / `-w` / `source` | — | Exactly one video source |
| `--roi` / `--count-line` / `--stop-line` | — | Geometry on the CLI |
| `--points-file` | — | JSON with `LANE_ROI_POINTS`, `LANE_COUNT_LINE`, `STOP_LINE` |
| `--count-direction` | `both` | Crossing direction (display metric) |
| `--broker` `--port` `--username` `--password` | from config | MQTT |
| `--model` | `YOLO_MODEL` | `.pt` / `.onnx` / `.rknn` / `.rnn` |
| `--npu-cores` | `RKNN_NPU_CORES` | NPU core mask |
| `--conf` | `0.5` | Score threshold |
| `--classes` | `1,2,3,5,7` | COCO ids: bicycle, car, moto, bus, truck |
| `--detect-every-n` | `2` | Run YOLO every N frames; SORT fills gaps |
| `--no-track` | off | Raw boxes, no SORT |
| `--interval` | `1.0` | Seconds between publishes |
| `--aggregate` | `max` | `max` / `mean` / `last` over the interval |
| `--zero-hold` | `3.0` | Hold last positive count this many seconds when the raw value drops to 0 |
| `--show` | off | OpenCV window |
| `--dry-run` | off | Log payloads, no MQTT |
| `-v` | off | Debug logs |

---

### `plate_reader.py` CLI (extra)

Same source / ROI / model / NPU flags as the lane detector, plus:

| Flag | Default | Meaning |
|------|---------|---------|
| `--plate-model` | auto | Plate weights |
| `--plate-conf` | `PLATE_DETECT_CONF` | Plate score |
| `--ocr` | `easyocr` | `easyocr` / `tesseract` / `none` |
| `--lang` | `en` | EasyOCR languages |
| `--gpu` | off | EasyOCR CUDA |
| `--min-ocr-conf` | `0.3` | Accept a read |
| `--min-plate-len` | `3` | Ignore shorter strings |
| `--read-every-n` | `5` | Plate+OCR period |
| `--csv` / `--save-crops` / `--save-video` | — | Outputs |

---

### `rknn_export.py` CLI

| Flag | Default | Meaning |
|------|---------|---------|
| `--model` | `YOLO_MODEL` | `.pt` or `.onnx` |
| `--target` | `rk3588` | NPU platform |
| `--dtype` | `fp` | `fp` = FP16; `i8`/`u8` = quantized (needs `--dataset` or `--video`) |
| `--imgsz` | `640` | Export / infer size |
| `--output` | `models/<stem>.rknn` | Destination |
| `--onnx` | `models/<stem>.onnx` | Intermediate ONNX |
| `--dataset` | — | Calib folder or `.txt` list |
| `--video` | — | Extract calib frames |
| `--calib-frames` | `50` | Max frames from `--video` |
| `--calib-dir` | `models/calib` | Where extracted JPGs go |
| `--opset` | `12` | ONNX opset |

---

### `simulator.py` CLI

| Flag | Default | Meaning |
|------|---------|---------|
| `--broker` `--port` `--topic` | from config | MQTT |
| `--interval-min` / `--interval-max` | `1.6` / `4.0` | Delay between batches |
| `--seed` | — | Reproducible random counts |
| `--once` | off | One batch and exit |

---

### `camera_retention.py` CLI

| Flag | Meaning |
|------|---------|
| *(no flag)* | One cleanup pass |
| `--daemon` | Loop every `CAMERA_RECORD_CLEANUP_INTERVAL_SECONDS` |

---

### `vehicle/config.py` (offline prototype)

| Name | Meaning |
|------|---------|
| `ENABLE_VEHICLE_DETECTION` | Run YOLO. |
| `ENABLE_LICENSE_PLATE_RECOGNITION` | EasyOCR on the vehicle crop (slow). |
| `ENABLE_SORT_TRACKING` | Multi-object tracks. |
| `ENABLE_LANE_ROI` | Ignore boxes whose centroid is outside `LANE_ROI_POINTS`. |
| `ENABLE_LANE_COUNTING` | Count crossings of `LANE_COUNT_LINE`. |
| `ENABLE_SPEED_ESTIMATION` | Pixel speed → km/h via `PIXELS_PER_METER`. |
| `ENABLE_STOP_LINE_DETECTION` | Watch `STOP_LINE`. |
| `ENABLE_RED_LIGHT_VIOLATION` | Flag a crossing while `RED_LIGHT_IS_ON`. |
| `VIDEO_SOURCE` | File (relative to repo root), `0` (webcam), or RTSP URL. |
| `YOLO_MODEL` | `.pt` or `.rknn`. |
| `RKNN_IMGSZ` / `RKNN_NPU_CORES` | Same meaning as `src/config.py`. |
| `DETECT_EVERY_N_FRAMES` | `1` = every frame. |
| `VEHICLE_CLASS_IDS` | COCO ids `[1, 2, 3, 5, 7]`. |
| `CONFIDENCE_THRESHOLD` | `0.5`. |
| `LANE_ROI_POINTS` | Polygon `(x, y)` in image pixels. |
| `LANE_COUNT_LINE` | Two points. |
| `LANE_COUNT_DIRECTION` | `both` / `up` / `down` / `left` / `right`. |
| `PIXELS_PER_METER` | Scale at the count line. |
| `VIDEO_FPS` | `0` = read from the file. |
| `SPEED_SMOOTHING_FRAMES` | Moving average length. |
| `STOP_LINE` | Two points. |
| `RED_LIGHT_IS_ON` | Virtual signal for violation logic. |
| `TREAT_ALL_STOP_LINE_CROSSINGS_AS_VIOLATION` | Ignore the virtual light. |
| `LICENSE_PLATE_LANGUAGES` | EasyOCR langs. |
| `LICENSE_PLATE_CROP_MARGIN` | Box expand factor (e.g. `1.2`). |
| `DRAW_*` | Overlay toggles: ROI, lines, IDs, speed, plate text, violations. |
| `OUTPUT_VIDEO_PATH` | `None` = display only; otherwise write `mp4`. |

Points-file JSON (also used by `lane_detector --points-file`):

```json
{
  "LANE_ROI_POINTS": [[x, y], ...],
  "LANE_COUNT_LINE": [[x1, y1], [x2, y2]],
  "STOP_LINE": [[x1, y1], [x2, y2]]
}
```

---

### `trafficator-app` config

| Location | Name | Meaning |
|----------|------|---------|
| `trafficator-app/app.json` → `expo.extra.apiBaseUrl` | Dashboard base URL (no trailing slash). Must be reachable from the phone. |
| `trafficator-app/constants/config.ts` | `FALLBACK_API_BASE_URL` if `extra.apiBaseUrl` is missing. |
| `trafficator-app/constants/config.ts` | `STATE_POLL_INTERVAL_MS` | How often `/api/state` is polled (`1000`). |

The app calls the same APIs as the web lane-status page: `GET /api/geofences`, `GET /api/state`.

---

## Project layout

```text
smart-traffic-light/
├── LICENSE
├── README.md
├── ARCHITECTURE.md
├── requirements.txt
├── camera.txt                 # RTSP notes; not loaded by code
├── models/                    # yolov8n / license_plate .pt .onnx .rknn
├── videos/                    # sample footage for vehicle/
├── src/
│   ├── config.py
│   ├── cordinator.py
│   ├── dashboard.py
│   ├── camera_app.py
│   ├── camera_retention.py
│   ├── relay_server.py
│   ├── lane_detector.py
│   ├── plate_reader.py
│   ├── rknn_detector.py
│   ├── rknn_export.py
│   ├── simulator.py
│   ├── pub.py  sub.py  mqtt_setup.py  test_relay.py
│   ├── lane_points/
│   ├── certs/
│   └── static/
├── vehicle/
│   ├── config.py
│   ├── main.py
│   ├── calibrate_roi.py
│   └── sort_tracker.py
└── trafficator-app/           # Expo / React Native
```

---

## License

This project is released under the **MIT License**. You may use, copy, modify, merge, publish, distribute, sublicense, and sell copies of the Software, free of charge, provided the copyright notice and permission notice are included. The Software is provided “as is”, without warranty.

```
MIT License

Copyright (c) 2026 sergeantexploiter

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The same text is in [`LICENSE`](LICENSE).
