# config.py
"""

[Unit]
Description=Smart Traffic Light Relay Server
After=network.target

[Service]
Type=simple
User=sigtwo
WorkingDirectory=/home/sigtwo/relaysrv
ExecStart=/usr/bin/python3 relay.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target

Shared configuration for the smart traffic light system. Used by coordinator,
simulator, dashboard, and relay clients. Paths are relative to this package.

sudo apt update
sudo apt install mosquitto mosquitto-clients
sudo mosquitto_passwd -c /etc/mosquitto/passwd <username>
sudo mosquitto_passwd /etc/mosquitto/passwd <additional_username>
sudo nano /etc/mosquitto/conf.d/default.conf
allow_anonymous false
password_file /etc/mosquitto/passwd
sudo systemctl restart mosquitto
mosquitto_sub -h localhost -t test -u "<username>" -P "<password>"

apt update
apt install python3-pip curl ffmpeg
pip3 install paho-mqtt requests cryptography flask waitress

/opt/homebrew/opt/mosquitto/sbin/mosquitto -c /opt/homebrew/etc/mosquitto/mosquitto.conf

"""

import os
from pathlib import Path

# -----------------------------------------------------------------------------
# Paths (certs and keys)
# -----------------------------------------------------------------------------
_BASE_DIR = Path(__file__).resolve().parent
CERTS_DIR = _BASE_DIR / 'certs'

# -----------------------------------------------------------------------------
# Dry run mode: if True, relay HTTP calls are skipped (logged only).
# Set to False in production to actually control relays.
# -----------------------------------------------------------------------------
DRY_RUN = False

FFMPEG_PATH = ""

if DRY_RUN == False:
    FFMPEG_PATH = "ffmpeg"
else:
    FFMPEG_PATH = "/Users/panther/Desktop/ffmpeg"

CAMERA_FEEDS = []

# Each feed can have "sequence": "seq1"|"seq2"|"seq3" to link the camera card to a phase (manual-mode trigger).
# Use sequence: None or "None" (or omit) for a camera that does not trigger any sequence.
if DRY_RUN == False:
    CAMERA_FEEDS = [
        {"id": "north_left", "label": "north_left", "sequence": "seq3", "recording" : False,
            "url": "rtsp://admin:SPLOIT4life@192.168.0.6:554/cam/realmonitor?channel=2&subtype=1&unicast=true&proto=Onvif"},
        {"id": "north_right", "label": "north_right", "sequence": "seq2", "recording" : False,
            "url": "rtsp://admin:SPLOIT4life@192.168.0.6:554/cam/realmonitor?channel=2&subtype=1&unicast=true&proto=Onvif"},
        {"id": "narrow_centre_1", "label": "narrow_centre_1", "sequence": None, "recording" : False,
            "url": "rtsp://admin:SPLOIT4life@192.168.0.6:554/cam/realmonitor?channel=2&subtype=1&unicast=true&proto=Onvif"},
        {"id": "narrow_centre_2", "label": "narrow_centre_2", "sequence": None, "recording" : False,
            "url": "rtsp://admin:SPLOIT4life@192.168.0.6:554/cam/realmonitor?channel=2&subtype=1&unicast=true&proto=Onvif"},
        {"id": "south_left", "label": "south_left", "sequence": "seq1", "recording" : False,
            "url": "rtsp://admin:SPLOIT4life@192.168.0.6:554/cam/realmonitor?channel=2&subtype=1&unicast=true&proto=Onvif"},
        {"id": "south_right", "label": "south_right", "sequence": "seq1", "recording" : False,
            "url": "rtsp://admin:SPLOIT4life@192.168.0.6:554/cam/realmonitor?channel=2&subtype=1&unicast=true&proto=Onvif"},
    ]
else:
    CAMERA_FEEDS = [
        {"id": "north_left", "label": "north_left", "sequence": "seq3", "recording": True, "record": True,
            "url": "rtsp://admin:SPLOIT4life@192.168.1.126:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"},
        {"id": "north_right", "label": "north_right", "sequence": "seq2","recording": True, "record": True,
            "url": "rtsp://admin:SPLOIT4life@192.168.1.73:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"},
        {"id": "narrow_centre_1", "label": "narrow_centre_1", "sequence": None,"recording": True, "record": True,
            "url": "rtsp://admin:SPLOIT4life@192.168.1.250:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"},
        {"id": "narrow_centre_2", "label": "narrow_centre_2", "sequence": None,"recording": True, "record": True,
            "url": "rtsp://admin:SPLOIT4life@192.168.1.201:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"},
        {"id": "south_left", "label": "south_left", "sequence": "seq1","recording": True, "record": True,
            "url": "rtsp://admin:SPLOIT4life@192.168.1.97:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"},
        {"id": "south_right", "label": "south_right", "sequence": "seq1","recording": True, "record": True,
            "url": "rtsp://admin:SPLOIT4life@192.168.1.130:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"},
    ]

# -----------------------------------------------------------------------------
# MQTT credentials (prefer env in production)
# -----------------------------------------------------------------------------
MQTT_USERNAME = os.environ.get('MQTT_USERNAME', 'sploit')
MQTT_PASSWORD = os.environ.get('MQTT_PASSWORD', '12345')

if DRY_RUN == False:
    MQTT_USERNAME = os.environ.get('MQTT_USERNAME', 'dison')
    MQTT_PASSWORD = os.environ.get('MQTT_PASSWORD', 'DISON4life@2026!')

# -----------------------------------------------------------------------------
# Relay server URLs (one per Pi; each Pi serves HTTP and controls its relays)
# -----------------------------------------------------------------------------
RELAY_SERVERS = {
    'pi1': 'https://192.168.0.176:8080',
    'pi2': 'https://192.168.0.202:8080',
    'pi3': 'https://192.168.0.113:8080',
    'pi4': 'https://192.168.0.112:8080',
}

# Relay logical id -> BCM GPIO pin number. Same mapping on each Pi.
RELAY_PINS = {
    1: 5, 2: 6, 3: 13, 4: 16, 5: 19, 6: 20, 7: 21, 8: 26,
}

IDLE_STANDBY_SECONDS = 10 * 60  # 30 minutes

# Each phase selects traffic light units that get green → then yellow → then red (same units for all three).
# unit_ids and trigger_keys are required; geofence is used by lane-status (GPS → which lane). Lamp keys derived from TRAFFIC_LIGHT_UNITS.
# Geofence: 4 points A, B, C, D (lat/lon). Boundary A → B → D → C → A. User is in lane if GPS inside polygon.
RED_KEYS = [
    "pole_2_right_green", "pole_1_left_red", "pole_1_centre_red", "pole_3_left_red", "pole_3_right_red",
    "pole_5_centre_red", "pole_5_right_red", "pole_4_left_red", "pole_4_right_a_red", "pole_4_right_b_red",
]

PHASES = {
    "seq1": {
        "name": "SEQUENCE 1",
        "green_keys": [
            "pole_2_right_green", "pole_1_left_green", "pole_1_centre_red", "pole_3_left_green", "pole_3_right_green", 
            "pole_5_centre_green", "pole_5_right_red", "pole_4_left_red", "pole_4_right_a_red", "pole_4_right_b_red",
        ],
        "yellow_keys": [
            "pole_2_right_green", "pole_1_left_yellow", "pole_1_centre_red", "pole_3_left_yellow", "pole_3_right_yellow", 
            "pole_5_centre_yellow", "pole_5_right_red", "pole_4_left_red", "pole_4_right_a_red", "pole_4_right_b_red",
        ],
        "red_keys": RED_KEYS,
        "trigger_keys": ["south_right_vehicle_count", "south_left_vehicle_count"],
        "geofence": {
            "name": "Lane 1 - School Exit",
            "A": [5.602540, -0.141943],
            "B": [5.602615, -0.142005],
            "C": [5.602905, -0.140880],
            "D": [5.602804, -0.140835],
        },
    },
    "seq2": {
        "name": "SEQUENCE 2",
        "green_keys": [
            "pole_2_right_green", "pole_5_right_green", "pole_5_centre_red", "pole_4_right_a_green", "pole_4_right_b_red", "pole_4_left_green", 
            "pole_3_right_red", "pole_3_left_red", "pole_1_centre_green", "pole_1_left_red"
        ],
        "yellow_keys": [
            "pole_2_right_yellow", "pole_5_right_yellow", "pole_5_centre_red", "pole_4_right_a_yellow", "pole_4_right_b_red", "pole_4_left_yellow", 
            "pole_3_right_red", "pole_3_left_red", "pole_1_centre_yellow", "pole_1_left_red"
        ],
        "red_keys": RED_KEYS,
        "trigger_keys": ["north_right_vehicle_count"],
        "geofence": {
            "name": "Lane 2 - Main Road Exit to School Entrance",
            "A": [5.603378, -0.141422],
            "B": [5.603499, -0.140981],
            "C": [5.603582, -0.141020],
            "D": [5.603451, -0.141455],
        },
    },
    "seq3": {
        "name": "SEQUENCE 3",
        "green_keys": [
            "pole_2_right_green", "pole_5_right_red", "pole_5_centre_red", "pole_4_right_a_green", "pole_4_right_b_green", "pole_4_left_red", 
            "pole_3_right_red", "pole_3_left_red", "pole_1_centre_green", "pole_1_left_red"
        ],
        "yellow_keys": [
            "pole_2_right_yellow", "pole_5_right_red", "pole_5_centre_red", "pole_4_right_a_yellow", "pole_4_right_b_yellow", "pole_4_left_red", 
            "pole_3_right_red", "pole_3_left_red", "pole_1_centre_yellow", "pole_1_left_red"
        ],
        "red_keys": RED_KEYS,
        "trigger_keys": ["north_left_vehicle_count"],
        "geofence": {
            "name": "Lane 3 - COMM RD TO MAIN ROAD EXIT",
            "A": [5.603352, -0.141513],
            "B": [5.603260, -0.141874],
            "C": [5.603362, -0.141897],
            "D": [5.603436, -0.141573],
        },
    },
}

STARTUP_RED_KEYS = [
    "pole_1_centre_yellow",
    "pole_1_left_yellow",
    "pole_2_right_yellow",
    "pole_3_left_yellow",
    "pole_3_right_yellow",
    "pole_5_centre_yellow",
    "pole_5_right_yellow",
    "pole_4_left_yellow",
    "pole_4_right_a_yellow",
    "pole_4_right_b_yellow",
]

# Lane geofences list derived from PHASES (for lane-status /api/geofences; browser resolves GPS client-side).
LANE_GEOFENCES = [
    {"sequence": pkey, "name": g.get("name", phase.get("name", pkey)), "A": g["A"], "B": g["B"], "C": g["C"], "D": g["D"]}
    for pkey, phase in PHASES.items()
    for g in [phase.get("geofence")]
    if g and "A" in g and "B" in g and "C" in g and "D" in g
]

# -----------------------------------------------------------------------------
# Traffic light units: one entry per physical head (pole + direction).
# Only one of red / yellow / green may be on at a time per unit.
# Used by coordinator to enforce "set unit to X" => other two colors off.
# -----------------------------------------------------------------------------
TRAFFIC_LIGHT_UNITS = [
    # Intersection 1
    {"id": "pole_1_centre", "red": "pole_1_centre_red", "yellow": "pole_1_centre_yellow", "green": "pole_1_centre_green"},
    {"id": "pole_1_left", "red": "pole_1_left_red", "yellow": "pole_1_left_yellow", "green": "pole_1_left_green"},
    {"id": "pole_2_right", "red": "pole_2_right_red", "yellow": "pole_2_right_yellow", "green": "pole_2_right_green"},
    {"id": "pole_3_left", "red": "pole_3_left_red", "yellow": "pole_3_left_yellow", "green": "pole_3_left_green"},
    {"id": "pole_3_right", "red": "pole_3_right_red", "yellow": "pole_3_right_yellow", "green": "pole_3_right_green"},
    # Intersection 2
    {"id": "pole_5_centre", "red": "pole_5_centre_red", "yellow": "pole_5_centre_yellow", "green": "pole_5_centre_green"},
    {"id": "pole_5_right", "red": "pole_5_right_red", "yellow": "pole_5_right_yellow", "green": "pole_5_right_green"},
    {"id": "pole_4_left", "red": "pole_4_left_red", "yellow": "pole_4_left_yellow", "green": "pole_4_left_green"},
    {"id": "pole_4_right_a", "red": "pole_4_right_a_red", "yellow": "pole_4_right_a_yellow", "green": "pole_4_right_a_green"},
    {"id": "pole_4_right_b", "red": "pole_4_right_b_red", "yellow": "pole_4_right_b_yellow", "green": "pole_4_right_b_green"},
    # Pedestrian (red + green only)
    {"id": "pedestrian", "red": "pedestrian_red", "yellow": None, "green": "pedestrian_green"},
]

# -----------------------------------------------------------------------------
# Trafficator dashboard (operator UI at /trafficator): auth and rate limit
# -----------------------------------------------------------------------------
TRAFFICATOR_USERNAME = "disonadmin"
TRAFFICATOR_PASSWORD = "Trafficator@2026!"
TRAFFICATOR_SECRET_KEY = "SF3JTN3KJXN3O4I3JODI3J4NDOX3ID4"
TRAFFICATOR_LOGIN_MAX_ATTEMPTS = 3
TRAFFICATOR_LOCKOUT_MINUTES = 30

# -----------------------------------------------------------------------------
# Paths to keys (for signing relay requests; coordinator holds private key)
# -----------------------------------------------------------------------------
PRIVATE_KEY_PATH = str(CERTS_DIR / 'private.pem')
PUBLIC_KEY_PATH = str(CERTS_DIR / 'public.pem')

# SSL for relay server HTTPS (cert/key for the Pi servers)
SSL_CERT_PATH = str(CERTS_DIR / 'cert.pem')
SSL_KEY_PATH = str(CERTS_DIR / 'key.pem')

# -----------------------------------------------------------------------------
# MQTT broker and topic
# -----------------------------------------------------------------------------
MQTT_BROKER = 'localhost'
MQTT_PORT = 1883
MQTT_BROKER_URL = f'mqtt://{MQTT_BROKER}:{MQTT_PORT}'
MQTT_BROKER_OVER_NETWORK = 'localhost'
MQTT_PORT_OVER_NETWORK = 1883
MQTT_BROKER_URL_OVER_NETWORK = f'mqtt://{MQTT_BROKER_OVER_NETWORK}:{MQTT_PORT_OVER_NETWORK}'
MQTT_TOPIC = 'vehicle_counts'

# Payload format expected by coordinator and simulator: "key:value:unix_timestamp"
MQTT_PAYLOAD_SEP = ':'
MQTT_PAYLOAD_PARTS = 3

# -----------------------------------------------------------------------------
# Coordinator timing (seconds)
# -----------------------------------------------------------------------------
# Messages older than this are treated as stale; counts decay to 0.
COORDINATOR_MESSAGE_MAX_AGE = 5.0
# Max seconds a green phase can be extended beyond its initial duration due to new arrivals.
COORDINATOR_GREEN_MAX_EXTENSION = 15
# Green phase duration bounds and density scaling.
COORDINATOR_GREEN_MIN = 12
COORDINATOR_GREEN_MAX = 55
COORDINATOR_GREEN_DENSITY_FACTOR = 2.5
# COORDINATOR_PED_DURATION unused — pedestrian is on/off only (handle_pedestrian)
COORDINATOR_YELLOW_DURATION = 3.0
COORDINATOR_ALL_OFF_GAP = 4.0
COORDINATOR_NARROW_BLOCK_SLEEP = 1.5
COORDINATOR_IDLE_SLEEP = 2.0
# Max seconds to wait for narrow_centre to clear after a phase (then proceed anyway).
COORDINATOR_NARROW_CENTRE_MAX_WAIT = 120.0
# Manual mode: initial green duration and each same-phase extend (dashboard trigger).
MANUAL_GREEN_DURATION = 90.0

# Relay HTTP: set True in production with proper server certs.
RELAY_VERIFY_SSL = False
RELAY_TIMEOUT = 5

# -----------------------------------------------------------------------------
# Dashboard (live visualization) — served at http://<host>:DASHBOARD_PORT
# -----------------------------------------------------------------------------
# Use 5001+ on macOS (Monterey+) to avoid AirPlay Receiver on 5000.
DASHBOARD_PORT = 5000
DASHBOARD_HOST = "192.168.0.11"
# If the dashboard HTTP port cannot be bound, run REBOOT_COMMAND (relays off first). Ignored when DRY_RUN.
REBOOT_ON_DASHBOARD_LISTEN_FAIL = True
REBOOT_DELAY_SECONDS = 5.0
# Command for reboot: list argv (preferred) or shell string, e.g. ["sudo", "reboot"] or "sudo reboot"
REBOOT_COMMAND = ["sudo", "reboot", "now"]
# Dashboard "Shutdown" button: relays off, then run SHUTDOWN_COMMAND. Ignored when DRY_RUN.
SHUTDOWN_DELAY_SECONDS = 5.0
SHUTDOWN_COMMAND = ["sudo", "shutdown", "now"]

# -----------------------------------------------------------------------------
# Camera streaming app — separate process or thread on its own port.
# Uses config.CAMERA_FEEDS and config.FFMPEG_PATH. Dashboard fetches streams from this URL.
# -----------------------------------------------------------------------------
CAMERA_APP_PORT = 5001
CAMERA_APP_HOST = "192.168.0.11"
# Base URL the browser uses to reach the camera app (same host as dashboard, different port).
CAMERA_APP_BASE_URL = "http://" + CAMERA_APP_HOST + ":" + str(CAMERA_APP_PORT)

# Disk recording (camera_app.py): 1× RTSP → MJPEG for live stream; second ffmpeg (stdin MJPEG→H.264) on a
# bounded queue — drops frames if encoder/disk lags so the browser stream is never blocked.
# Set CAMERA_RECORD_ENABLED = False to disable. Per-feed override: "record": False in a CAMERA_FEEDS entry.
CAMERA_RECORD_ENABLED = False
CAMERA_RECORD_DIR = str("/home/coordinator/camera_recordings")  # absolute path recommended on Pi
CAMERA_RECORD_QUEUE_MAX = 2  # recording only; full queue → drop oldest frame for disk, never block stream
CAMERA_RECORD_SEGMENT_MINUTES = 60  # new segment file every N minutes (ffmpeg -segment_time)
# Video output: "mp4" (H.264 + faststart) or "mkv" (H.264 in Matroska, robust on abrupt close)
CAMERA_RECORD_CONTAINER = "mp4"
CAMERA_RECORD_PRESET = "veryfast"  # x264 preset (ultrafast … veryslow)
CAMERA_RECORD_CRF = 23  # quality (lower = better, ~18–28 typical)
# Recording retention: see camera_retention.py (run once via cron or python -m src.camera_retention --daemon).
# Delete files older than this many days (mtime under CAMERA_RECORD_DIR/<cam_id>/). 0 = no-op in retention script.
CAMERA_RECORD_RETENTION_DAYS = 30
# Daemon mode only: seconds between cleanup passes.
CAMERA_RECORD_CLEANUP_INTERVAL_SECONDS = 6 * 3600

# -----------------------------------------------------------------------------
# License plate reader (src/plate_reader.py) — two-stage ANPR defaults.
# All are overridable via CLI flags; the script reads them with getattr fallbacks.
# -----------------------------------------------------------------------------
# Vehicle detector weights (shared with lane_detector.py).
# Use a .rknn / .rnn path to run on the Orange Pi RK3588 6 TOPS NPU.
# If YOLO_MODEL is still a .pt name and a sibling .rknn exists, the NPU file is preferred.
YOLO_MODEL = "yolov8n.pt"
# Dedicated YOLOv8 license-plate weights. None => auto-detect
# models/license_plate_detector.rknn (or .pt), else the classic OpenCV localizer.
PLATE_MODEL = None
# RKNN / NPU (Orange Pi 5 family — RK3588, 6 TOPS). Used by rknn_export.py and load_detector().
RKNN_TARGET = "rk3588"
RKNN_IMGSZ = 640
RKNN_NPU_CORES = "0_1_2"  # all 3 NPU cores; pin 0 / 1 / 2 if you run several detectors
PLATE_DETECT_CONF = 0.25          # plate-detection confidence threshold (model only)
PLATE_OCR_ENGINE = "easyocr"      # "easyocr" | "tesseract" | "none" (localize only)
PLATE_OCR_LANGS = ["en"]          # EasyOCR languages
PLATE_MIN_OCR_CONF = 0.3          # keep a read only above this confidence

# -----------------------------------------------------------------------------
# Lamp/signal id -> (server, relay_id). Used by coordinator and dashboard.
# -----------------------------------------------------------------------------
RELAY_MAPPINGS = {
    # Intersection 1
    #pole 1
    'pole_1_centre_red': ('pi1', 1),
    'pole_1_centre_yellow': ('pi1', 2), 
    'pole_1_centre_green': ('pi1', 3),
    'pole_1_left_red': ('pi1', 4),
    'pole_1_left_yellow': ('pi1', 5),
    'pole_1_left_green': ('pi1', 6),
    #pole 2
    'pole_2_right_red': ('pi1', 7),
    'pole_2_right_yellow': ('pi1', 8),
    'pole_2_right_green': ('pi2', 1),
    #pole 3
    'pole_3_left_red': ('pi2', 5),
    'pole_3_left_yellow': ('pi2', 6),
    'pole_3_left_green': ('pi2', 7),
    'pole_3_right_red': ('pi2', 2),
    'pole_3_right_yellow': ('pi2', 3),
    'pole_3_right_green': ('pi2', 4),
    # Intersection 2
    #pole 5
    'pole_5_centre_red': ('pi3', 1),
    'pole_5_centre_yellow': ('pi3', 2),
    'pole_5_centre_green': ('pi3', 3),
    'pole_5_right_red': ('pi3', 4),
    'pole_5_right_yellow': ('pi3', 5),
    'pole_5_right_green': ('pi3', 6),
    #pole 4
    'pole_4_left_red': ('pi3', 4),
    'pole_4_left_yellow': ('pi3', 5),
    'pole_4_left_green': ('pi3', 6),
    'pole_4_right_a_red': ('pi4', 4),
    'pole_4_right_a_yellow': ('pi4', 5),
    'pole_4_right_a_green': ('pi4', 6),
    'pole_4_right_b_red': ('pi4', 1),
    'pole_4_right_b_yellow': ('pi4', 2),
    'pole_4_right_b_green': ('pi4', 3),  # Fixed from 0 to 8
    #pedestrian red and green
    'pedestrian_red': ('pi3', 7),
    'pedestrian_green': ('pi3', 8),
    # Add more...
}