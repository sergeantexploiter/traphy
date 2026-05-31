# coordinator.py — production ready (early green termination + wait for narrow centre clear)
"""
Traffic light coordinator: subscribes to MQTT vehicle/pedestrian counts, selects
phases (FCFS + density tie-breaker), and controls relay servers (Pis) to turn
green/yellow/red. Waits for narrow centre to clear before starting next phase.
Runs a dashboard thread for live visualization.
/home/ubuntu/hdstorage
Flow:
  - Main loop: when idle and narrow centre clear, select_next_phase() or run manual phase.
  - run_phase(): current sequence green → yellow → red (other sequences stay red); then wait narrow clear.
  - Relay state is driven by PHASES green_keys/yellow_keys/red_keys and RELAY_MAPPINGS (one batch per server).
"""
import subprocess
import time
import logging
import signal
import sys
import threading
from paho.mqtt import client as mqtt
import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
import base64
import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Relay control: one batch per server (relays in list = ON, all others OFF)
# -----------------------------------------------------------------------------

def force_all_relays_off():
    """Turn off every relay on every server (one batch per server). Only call in standby mode or on shutdown."""
    for srv in config.RELAY_SERVERS:
        _relay_batch_one(srv, [])

# -----------------------------------------------------------------------------
# Phase definitions: traffic sequences (seq1/2/3) with green/yellow lamps and
# trigger keys. Intersection layout:
#   a < > b < > c     (seq3 from a, seq2 from c)
#             |
#             |
#   d < > e < f       (seq1 from f)
# -----------------------------------------------------------------------------
PHASES = config.PHASES

# -----------------------------------------------------------------------------
# Resolve config keys to relay IDs using RELAY_MAPPINGS only. No pre-cached state.
# -----------------------------------------------------------------------------
def _keys_to_by_server(keys):
    """Resolve lamp keys (from config) to per-server set of relay IDs via RELAY_MAPPINGS."""
    by_server = {}
    for key in keys or []:
        if key in config.RELAY_MAPPINGS:
            srv, rid = config.RELAY_MAPPINGS[key]
            by_server.setdefault(srv, set()).add(rid)
    return by_server

# Pedestrian: single relay for red or green (from RELAY_MAPPINGS).
pedestrian_relay_by_color = {}
for key in ("pedestrian_red", "pedestrian_green"):
    if key in config.RELAY_MAPPINGS:
        pedestrian_relay_by_color["red" if key == "pedestrian_red" else "green"] = config.RELAY_MAPPINGS[key]

# Desired pedestrian state; apply_relay_state() includes it in the batch.
pedestrian_desired_color = "red"


def apply_relay_state(force=False):
    """Send one batch per server: relay IDs that should be ON (current phase color; when idle, union of every phase's red_keys + pedestrian).
    Pass force=True on phase sub-state transitions (green/yellow/red) to always fire HTTP regardless of last state."""
    by_server = {}
    if current_phase and phase_sub_state:
        # Phase running: use only the selected sequence's green_keys / yellow_keys / red_keys from config.
        phase = PHASES.get(current_phase)
        if phase and phase_sub_state in ("green", "yellow", "red"):
            key_list = phase.get(phase_sub_state + "_keys") or []
            for srv, rids in _keys_to_by_server(key_list).items():
                by_server.setdefault(srv, set()).update(rids)
    else:
        # Idle: union of every phase's red_keys (all sequences show red), plus pedestrian.
        for pkey, phase in PHASES.items():
            key_list = phase.get("red_keys") or []
            for srv, rids in _keys_to_by_server(key_list).items():
                by_server.setdefault(srv, set()).update(rids)
    if pedestrian_desired_color and pedestrian_desired_color in pedestrian_relay_by_color:
        srv, rid = pedestrian_relay_by_color[pedestrian_desired_color]
        by_server.setdefault(srv, set()).add(rid)
    for srv in config.RELAY_SERVERS:
        rids_on = sorted(by_server.get(srv, set()))
        _relay_batch_one(srv, rids_on, force=force)


def set_all_units_to_red():
    """All sequences to red, pedestrian to red (startup / safe initial state). One batch per server."""
    global current_phase, phase_sub_state, pedestrian_desired_color
    current_phase = None
    phase_sub_state = None
    pedestrian_desired_color = "red"
    startup_keys = getattr(config, "STARTUP_RED_KEYS", None)
    if startup_keys:
        # Manually specified startup lamps (keys in RELAY_MAPPINGS) + pedestrian red.
        by_server = _keys_to_by_server(startup_keys)
        if "red" in pedestrian_relay_by_color:
            srv, rid = pedestrian_relay_by_color["red"]
            by_server.setdefault(srv, set()).add(rid)
        for srv in config.RELAY_SERVERS:
            _relay_batch_one(srv, sorted(by_server.get(srv, set())))
    else:
        apply_relay_state()

# -----------------------------------------------------------------------------
# Live count data from MQTT (updated in on_message)
# Keys must match simulator/detectors. first_seen used for FCFS phase selection.
# -----------------------------------------------------------------------------
data = {
    'north_right_vehicle_count': {'value': 0, 'first_seen': 0.0},
    'north_left_vehicle_count':  {'value': 0, 'first_seen': 0.0},
    'south_left_vehicle_count':  {'value': 0, 'first_seen': 0.0},
    'south_right_vehicle_count': {'value': 0, 'first_seen': 0.0},
    'pedestrian_narrow_passage_count': {'value': 0, 'last_update': 0.0},
    'narrow_centre_vehicle_count': {'value': 0, 'first_seen': 0.0},
}

# Validate phase config at startup: trigger_keys in data; green_keys, yellow_keys, red_keys in RELAY_MAPPINGS.
for pkey, phase in PHASES.items():
    for tk in phase["trigger_keys"]:
        if tk not in data:
            raise KeyError(f"PHASES['{pkey}']['trigger_keys'] references unknown key '{tk}' (not in data)")
    for key in phase.get("green_keys", []) + phase.get("yellow_keys", []) + phase.get("red_keys", []):
        if key not in config.RELAY_MAPPINGS:
            raise KeyError(f"PHASES['{pkey}'] lamp key '{key}' not in RELAY_MAPPINGS")

LAST_UPDATE = {k: 0.0 for k in data}

MAX_AGE = config.COORDINATOR_MESSAGE_MAX_AGE

# -----------------------------------------------------------------------------
# Coordinator state (phase, relay state, mode, standby)
# -----------------------------------------------------------------------------
_data_lock = threading.Lock()  # Protects data dict + in_phase (MQTT callback vs main thread).
current_phase = None       # Phase key currently running (seq1/seq2/seq3) or None when idle.
last_served = {k: 0.0 for k in PHASES}  # Last time each phase was served (for fair round-robin).
last_relay_state = {}      # server -> set of relay IDs we consider ON (for dashboard only — not used for dedup).
idle_all_off_sent = False  # True after we have sent all-off while idle (avoid repeated sends).
control_mode = "auto"      # "auto" or "manual"; when manual, only trigger_phase() runs phases.
manual_phase_request = None  # When manual: phase_key to run next (seq1/seq2/seq3), or None.
cancel_phase_request = False  # When True, run_phase aborts and applies all-red (dashboard "Cancel" button).
pending_sleep_standby = False  # When True, enter standby (all relays off) once idle (dashboard "Sleep" button).
wake_standby_request = False  # When True, exit standby and resume normal relay control (dashboard "Wake").
_control_lock = threading.Lock()
_manual_wake_event = threading.Event()   # Set by trigger_phase() so idle manual loop wakes quickly.
_standby_wake_event = threading.Event()   # Set by MQTT on_message when data changes; standby loop wakes.
_shutdown = False
in_phase = False
phase_start_time = 0.0
assigned_green_duration = 0.0
current_phase_trigger_count = 0  # Snapshot of trigger count when phase started (for dashboard).
phase_sub_state = None     # "green" | "yellow" | "red" | None (for dashboard and lane-status).
yellow_start_time = 0.0    # When we entered yellow (for Yellow time display).

# Standby: after IDLE_STANDBY_SECONDS with no activity, all relays off until activity or manual trigger.
IDLE_STANDBY_SECONDS = config.IDLE_STANDBY_SECONDS
last_activity_time = time.time()  # Updated on data change (MQTT), run_phase start, or trigger_phase().
standby_mode = False
standby_data_snapshot = {}  # Snapshot of data when we entered standby (to detect change on wake).

# -----------------------------------------------------------------------------
# Signing: relay servers verify requests with coordinator's public key
# -----------------------------------------------------------------------------
# Load private key for signing relay requests (relay servers verify with public key).
try:
    with open(config.PRIVATE_KEY_PATH, "rb") as f:
        PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)
except FileNotFoundError:
    raise SystemExit(f"Private key not found at {config.PRIVATE_KEY_PATH}. Generate with: openssl genrsa -out private.pem 2048")
except Exception as e:
    raise SystemExit(f"Invalid private key at {config.PRIVATE_KEY_PATH}: {e}")

def sign(msg: bytes) -> str:
    """Sign message with PRIVATE_KEY (PSS-SHA256); return base64 signature for X-Signature header."""
    sig = PRIVATE_KEY.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()

def _relay_batch_one(server, rids_on, force=False):
    """Send one batch to a server: these relay IDs ON, all others OFF.
    Skips HTTP if relay state is unchanged, unless force=True (used on phase sub-state transitions)."""
    rids_on = sorted(set(rids_on))
    if not force and last_relay_state.get(server) == set(rids_on):
        return
    url = f"{config.RELAY_SERVERS[server]}/relay"
    logger.info(f"Relay command → {url}  relays_on={rids_on}")
    body = {"relays": rids_on, "action": "on"}
    rids_str = ",".join(map(str, rids_on))
    message = f"relays={rids_str}&action=on".encode()
    sig = sign(message)
    headers = {"X-Signature": sig, "Content-Type": "application/json"}

    if config.DRY_RUN:
        logger.info(f"[DRY RUN] batch {body} → {server}")
    else:
        try:
            r = requests.post(url, json=body, headers=headers, verify=config.RELAY_VERIFY_SSL, timeout=config.RELAY_TIMEOUT)
            logger.info(f"BATCH {rids_str} → {server} → {r.status_code}")
        except Exception as e:
            try:
                r = requests.post(url, json=body, headers=headers, verify=config.RELAY_VERIFY_SSL, timeout=config.RELAY_TIMEOUT)
                logger.info(f"BATCH {rids_str} → {server} → {r.status_code} (retry ok)")
            except Exception as e2:
                logger.error(f"Relay fail {server}: {e2}")

    last_relay_state[server] = set(rids_on)

# -----------------------------------------------------------------------------
# Helpers: count decay, pedestrian, manual phase abort
# -----------------------------------------------------------------------------

def decay_old_counts():
    """Set to 0 any count that has not been updated for longer than MAX_AGE (stale MQTT)."""
    now = time.time()
    for key in data:
        if now - LAST_UPDATE[key] > MAX_AGE and data[key]['value'] != 0:
            data[key]['value'] = 0
            if 'first_seen' in data[key]:
                data[key]['first_seen'] = 0.0
            logger.debug(f"Decay → {key} → 0")

def handle_pedestrian():
    """Set pedestrian to green when pedestrian_narrow_passage_count > 0, else red; then apply relay state."""
    global pedestrian_desired_color
    ped_count = data['pedestrian_narrow_passage_count']['value']
    if "green" not in pedestrian_relay_by_color and "red" not in pedestrian_relay_by_color:
        return
    pedestrian_desired_color = "green" if ped_count > 0 else "red"
    apply_relay_state()
    if ped_count > 0:
        logger.info(f"Pedestrian GREEN ON (count {ped_count})")
    else:
        logger.debug("Pedestrian RED (green off)")

def _manual_phase_switch_requested(current_phase_key):
    """True if in manual mode and user selected a different phase (terminate current, run selected)."""
    with _control_lock:
        return (
            control_mode == "manual"
            and manual_phase_request is not None
            and manual_phase_request != current_phase_key
        )


def _cancel_phase_requested():
    """True if dashboard requested cancel phase (all-red)."""
    with _control_lock:
        return cancel_phase_request


def _abort_phase_go_all_red(clear_manual_request=False):
    """Clear current phase and apply idle all-red relay state (used on manual switch or cancel).
    If clear_manual_request=True (cancel button), also clear manual_phase_request so no phase starts next."""
    global current_phase, phase_sub_state, cancel_phase_request, manual_phase_request
    with _control_lock:
        current_phase = None
        cancel_phase_request = False
        if clear_manual_request:
            manual_phase_request = None
    phase_sub_state = None
    apply_relay_state(force=True)

def _sleep_with_abort(seconds, phase_key):
    """Sleep in small steps so manual phase switch or cancel can abort (e.g. during yellow or red gap)."""
    step = 0.2
    end = time.time() + seconds
    while time.time() < end and not _shutdown:
        if _cancel_phase_requested():
            return True
        if _manual_phase_switch_requested(phase_key):
            return True
        _standby_wake_event.clear()
        _standby_wake_event.wait(timeout=min(step, end - time.time()))
    return _cancel_phase_requested() or _manual_phase_switch_requested(phase_key)

# -----------------------------------------------------------------------------
# run_phase: execute one full sequence (green → yellow → red → wait narrow clear)
# In manual mode, aborts early if user selects another phase.
# -----------------------------------------------------------------------------

def run_phase(phase_key):
    """Run one full phase: green -> yellow -> red -> wait narrow centre clear. In manual mode, aborts if another phase is selected."""
    global current_phase, idle_all_off_sent, in_phase, phase_start_time, assigned_green_duration, current_phase_trigger_count
    global phase_sub_state, yellow_start_time, last_activity_time, manual_phase_request
    last_activity_time = time.time()
    with _data_lock:
        in_phase = True
    phase_sub_state = "green"
    try:
        phase = PHASES[phase_key]
        trigger_keys = phase["trigger_keys"]
        current_phase_trigger_count = sum(data[tk]["value"] for tk in trigger_keys)

        # Step 1: Current phase green, all other phases red (one batch per server).
        phase_sub_state = "green"
        apply_relay_state(force=True)
        phase_start_time = time.time()

        # Step 2: Green duration — manual: MANUAL_GREEN_DURATION; auto: from density. Manual: same-phase click extends.
        MANUAL_GREEN_DURATION = getattr(config, "MANUAL_GREEN_DURATION", 90.0)
        with _control_lock:
            is_manual = control_mode == "manual"
        if is_manual:
            assigned_green_duration = MANUAL_GREEN_DURATION
        else:
            def _compute_green_duration():
                """Recalculate green duration from live counts. Capped to time already elapsed
                so the phase never extends beyond COORDINATOR_GREEN_MAX from its start."""
                density = sum(
                    d['value'] for k, d in data.items()
                    if 'vehicle' in k and k != 'narrow_centre_vehicle_count'
                )
                raw = config.COORDINATOR_GREEN_MIN + int(density * config.COORDINATOR_GREEN_DENSITY_FACTOR)
                return max(config.COORDINATOR_GREEN_MIN, min(config.COORDINATOR_GREEN_MAX, raw))
            assigned_green_duration = _compute_green_duration()

        logger.info(f"→ {phase['name']} GREEN started (initial duration {assigned_green_duration:.1f}s)")

        # Wait green duration (tick every 0.2s; abort if manual phase switch or cancel requested; same-phase click extends green).
        while not _shutdown:
            if _cancel_phase_requested():
                logger.info(f"→ {phase['name']} cancelled (all-red)")
                _abort_phase_go_all_red(clear_manual_request=True)
                return
            if _manual_phase_switch_requested(phase_key):
                logger.info(f"→ {phase['name']} terminated (manual phase switch)")
                _abort_phase_go_all_red(clear_manual_request=False)
                return
            if is_manual:
                with _control_lock:
                    if manual_phase_request == phase_key:
                        manual_phase_request = None
                        assigned_green_duration += MANUAL_GREEN_DURATION
                        logger.info(f"→ {phase['name']} GREEN extended by {MANUAL_GREEN_DURATION:.0f}s (total {assigned_green_duration:.0f}s)")
            elapsed = time.time() - phase_start_time

            if not is_manual:
                new_duration = _compute_green_duration()
                if new_duration > assigned_green_duration:
                    logger.info(
                        f"Green extended: {assigned_green_duration:.1f}s → {new_duration:.1f}s "
                        f"(elapsed {elapsed:.1f}s)"
                    )
                assigned_green_duration = max(assigned_green_duration, new_duration)

            if elapsed >= assigned_green_duration:
                break

            _standby_wake_event.clear()
            _standby_wake_event.wait(timeout=0.2)  # Short tick to react quickly to shutdown/data
            decay_old_counts()
            handle_pedestrian()

        logger.info(f"→ {phase['name']} GREEN ended (served {time.time() - phase_start_time:.1f}s)")

        # Step 3: Yellow — phase to yellow, then sleep (abort if manual switch or cancel).
        phase_sub_state = "yellow"
        yellow_start_time = time.time()
        apply_relay_state(force=True)
        if _cancel_phase_requested():
            logger.info(f"→ {phase['name']} cancelled (all-red)")
            _abort_phase_go_all_red(clear_manual_request=True)
            return
        if _sleep_with_abort(config.COORDINATOR_YELLOW_DURATION, phase_key):
            if _cancel_phase_requested():
                logger.info(f"→ {phase['name']} cancelled (all-red)")
                _abort_phase_go_all_red(clear_manual_request=True)
            else:
                logger.info(f"→ {phase['name']} terminated (manual phase switch)")
                _abort_phase_go_all_red(clear_manual_request=False)
            return

        # Step 4: Red — phase to red, then short gap (abort if manual switch or cancel).
        phase_sub_state = "red"
        apply_relay_state(force=True)
        if _cancel_phase_requested():
            logger.info(f"→ {phase['name']} cancelled (all-red)")
            _abort_phase_go_all_red(clear_manual_request=True)
            return
        if _sleep_with_abort(config.COORDINATOR_ALL_OFF_GAP, phase_key):
            if _cancel_phase_requested():
                logger.info(f"→ {phase['name']} cancelled (all-red)")
                _abort_phase_go_all_red(clear_manual_request=True)
            else:
                logger.info(f"→ {phase['name']} terminated (manual phase switch)")
                _abort_phase_go_all_red(clear_manual_request=False)
            return

        # Step 5: Wait for narrow centre to clear (or timeout); then phase complete.
        logger.info("Cycle finished → waiting for narrow centre to clear...")
        narrow_wait_start = time.time()
        max_wait = config.COORDINATOR_NARROW_CENTRE_MAX_WAIT
        while data['narrow_centre_vehicle_count']['value'] > 0 and not _shutdown:
            if _cancel_phase_requested():
                logger.info(f"→ {phase['name']} cancelled (all-red)")
                _abort_phase_go_all_red(clear_manual_request=True)
                return
            if _manual_phase_switch_requested(phase_key):
                logger.info(f"→ {phase['name']} terminated (manual phase switch)")
                _abort_phase_go_all_red(clear_manual_request=False)
                return
            if time.time() - narrow_wait_start >= max_wait:
                logger.warning(f"Narrow centre wait timeout ({max_wait:.0f}s) — proceeding anyway")
                break
            _standby_wake_event.clear()
            _standby_wake_event.wait(timeout=0.2)  # Re-check narrow count quickly when MQTT updates
            decay_old_counts()
            handle_pedestrian()
        logger.info("Narrow centre clear → ready for next sequence")

        last_served[phase_key] = time.time()
        current_phase = None
        idle_all_off_sent = False
        apply_relay_state(force=True)  # All sequences red after phase ends
    finally:
        with _data_lock:
            in_phase = False
        phase_start_time = 0.0
        assigned_green_duration = 0.0
        phase_sub_state = None
        yellow_start_time = 0.0
        # Refresh LAST_UPDATE so decay_old_counts() doesn't immediately zero counts that were frozen during the phase.
        now = time.time()
        for key in data:
            if data[key].get("value", 0) > 0:
                LAST_UPDATE[key] = now

# -----------------------------------------------------------------------------
# MQTT: receive vehicle/pedestrian counts; update data and first_seen
# Updates run even during a phase so select_next_phase() sees fresh demand when phase ends.
# -----------------------------------------------------------------------------

def on_message(client, userdata, msg):
    """Handle MQTT message: parse key:value:timestamp, update data and LAST_UPDATE; set first_seen on 0->1 transition."""
    global last_activity_time
    try:
        payload = msg.payload.decode().strip()
        parts = payload.split(config.MQTT_PAYLOAD_SEP)
        if len(parts) != config.MQTT_PAYLOAD_PARTS:
            return
        key, val_str, ts_str = [x.strip() for x in parts]
        if key not in data:
            return

        value = int(val_str)
        ts = float(ts_str)
        now = time.time()

        if now - ts > MAX_AGE:
            return

        with _data_lock:
            old_value = data[key]['value']
            data[key]['value'] = value
            LAST_UPDATE[key] = now
            if value > 0 and old_value == 0:
                data[key]['first_seen'] = ts
            last_activity_time = now  # Any count change resets idle timer / exits standby
            _standby_wake_event.set()  # Wake standby loop so it checks for activity immediately
        if value > 0 and old_value == 0:
            logger.info(f"→ First arrival: {key} at {ts} (count {value})")
        logger.debug(f"Update: {key} = {value} (ts {ts})")
    except Exception as e:
        logger.warning(f"MQTT parse fail: {payload} → {e}")

def select_next_phase():
    """Among phases with demand, pick the one served least recently (fairness). Tie-break: FCFS then density."""
    candidates = []
    for k in ["seq1", "seq2", "seq3"]:
        trigger_keys = PHASES[k]["trigger_keys"]
        has_demand = any(data[tk]['value'] > 0 for tk in trigger_keys)
        if has_demand:
            arrivals = [data[tk]['first_seen'] for tk in trigger_keys if data[tk]['first_seen'] > 0]
            earliest = min(arrivals) if arrivals else 0.0
            density = sum(data[tk]['value'] for tk in trigger_keys)
            # Sort key: (last_served asc, earliest arrival asc, density desc) → fairest then FCFS then busiest.
            candidates.append((last_served[k], earliest, density, k))

    if candidates:
        candidates.sort(key=lambda x: (x[0], x[1], -x[2]))
        phase_key = candidates[0][3]
        reason = "fair (least recently served)"
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            reason += " + FCFS/density"
        logger.info(f"{reason} → {PHASES[phase_key]['name']} (arrival {candidates[0][1]})")
        return phase_key

    return None

def _execute_system_command(cmd) -> None:
    """Run configured host command (list argv or shell string)."""
    logger.critical("Executing system command: %s", cmd)
    try:
        if isinstance(cmd, str):
            subprocess.run(cmd, shell=True, check=False)
        else:
            subprocess.run(list(cmd), check=False)
    except Exception as e:
        logger.error("System command failed: %s", e)


def _run_power_off_sequence(command, delay_seconds: float, label: str) -> None:
    """Turn relays off, disconnect MQTT, run command, exit (for reboot/shutdown)."""
    global _shutdown
    _shutdown = True
    logger.info("Turning off all relays before %s", label)
    force_all_relays_off()
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    try:
        client.disconnect()
        client.loop_stop()
    except Exception:
        pass
    _execute_system_command(command)
    sys.exit(0 if label == "shutdown" else 1)


def _handle_dashboard_listen_fail(host: str, port: int, exc: BaseException) -> None:
    """Dashboard HTTP bind failed — optionally turn relays off and reboot the host."""
    global _shutdown
    logger.critical(
        "Dashboard cannot listen on %s:%s — %s: %s",
        host,
        port,
        type(exc).__name__,
        exc,
    )
    if getattr(config, "DRY_RUN", False):
        logger.warning("DRY_RUN: not rebooting after dashboard listen failure")
        return
    if not getattr(config, "REBOOT_ON_DASHBOARD_LISTEN_FAIL", False):
        logger.error(
            "Set REBOOT_ON_DASHBOARD_LISTEN_FAIL = True in config to reboot on port bind failure"
        )
        return
    cmd = getattr(config, "REBOOT_COMMAND", ["sudo", "reboot"])
    delay = float(getattr(config, "REBOOT_DELAY_SECONDS", 5.0))
    _run_power_off_sequence(cmd, delay, "reboot")


def _shutdown_handler(signum, frame):
    """On SIGINT/SIGTERM: set _shutdown, all relays off, disconnect MQTT, exit."""
    global _shutdown
    _shutdown = True
    logger.info("Shutdown → all relays off")
    force_all_relays_off()
    time.sleep(0.2)  # Brief moment for relay batch to complete before disconnect
    try:
        client.disconnect()
        client.loop_stop()
    except Exception:
        pass
    logger.info("Coordinator stopped.")
    sys.exit(0)

signal.signal(signal.SIGINT, _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)

# -----------------------------------------------------------------------------
# MQTT client: subscribe to vehicle_counts; process messages in background thread
# -----------------------------------------------------------------------------
client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
client.on_message = on_message

def on_disconnect(c, ud, flags, rc, props=None):
    """On unexpected disconnect, log and let paho auto-reconnect (loop_start handles this)."""
    if rc != 0:
        logger.warning(f"MQTT unexpected disconnect (rc={rc}) — will auto-reconnect")
    else:
        logger.info("MQTT disconnected cleanly")

def on_connect(c, ud, flags, rc, props=None):
    """On connect/reconnect: log result and re-subscribe (re-subscribe is needed after reconnect)."""
    if rc == 0:
        logger.info("MQTT connected — subscribing to %s", config.MQTT_TOPIC)
        c.subscribe(config.MQTT_TOPIC)  # moved here from module level so reconnects re-subscribe
    else:
        logger.warning(f"MQTT connect failed: {rc}")

client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
client.reconnect_delay_set(min_delay=1, max_delay=30)  # exponential backoff up to 30s
client.connect(config.MQTT_BROKER, config.MQTT_PORT, 60)
client.loop_start()

# Startup: set all units to red (one batch per server), then enter main loop.
logger.info("Starting → set all units to red")
set_all_units_to_red()
time.sleep(0.5)  # Brief settle before main loop

logger.info("Coordinator running – FCFS + density tie-breaker (pedestrian independent)")

# -----------------------------------------------------------------------------
# Dashboard API: control mode and manual phase trigger (called by dashboard Flask app)
# -----------------------------------------------------------------------------

def set_control_mode(mode):
    """Set control mode to 'auto' or 'manual'. When manual, coordinator does not auto-select phases."""
    global control_mode
    if mode not in ("auto", "manual"):
        return
    with _control_lock:
        control_mode = mode
    logger.info(">>> Control mode set → %s <<<", mode)


def trigger_phase(phase_key):
    """Request running a phase (seq1/seq2/seq3). Only runs when control_mode is 'manual'."""
    global manual_phase_request, last_activity_time
    last_activity_time = time.time()
    if phase_key not in PHASES:
        logger.warning("trigger_phase: unknown phase key '%s'", phase_key)
        return
    with _control_lock:
        if control_mode != "manual":
            logger.warning(">>> trigger_phase('%s') IGNORED — control_mode=%s (switch to manual first) <<<", phase_key, control_mode)
            return
        manual_phase_request = phase_key
    _manual_wake_event.set()
    logger.info(">>> Manual phase trigger accepted → %s <<<", phase_key)


def cancel_phase():
    """Request cancelling the current phase and going to all-red (dashboard 'Cancel' button)."""
    global cancel_phase_request
    with _control_lock:
        cancel_phase_request = True
    _standby_wake_event.set()
    logger.info(">>> Cancel phase (all-red) requested <<<")


def request_sleep_standby():
    """Enter power-save standby: abort any phase, clear manual request, all relays off (dashboard 'Sleep')."""
    global pending_sleep_standby, cancel_phase_request, manual_phase_request, last_activity_time
    last_activity_time = time.time()
    with _control_lock:
        pending_sleep_standby = True
        cancel_phase_request = True
        manual_phase_request = None
    _standby_wake_event.set()
    _manual_wake_event.set()
    logger.info(">>> Sleep — standby (all relays off) requested <<<")


def request_wake_standby():
    """Exit standby: resume idle relay pattern (all-red + pedestrian) (dashboard 'Wake')."""
    global wake_standby_request, last_activity_time
    last_activity_time = time.time()
    with _control_lock:
        wake_standby_request = True
    _standby_wake_event.set()
    logger.info(">>> Wake — exit standby requested <<<")


def request_system_shutdown():
    """Power off the coordinator host (dashboard 'Shutdown' after confirmation)."""
    if getattr(config, "DRY_RUN", False):
        logger.warning(">>> Shutdown requested — DRY_RUN: not executing SHUTDOWN_COMMAND <<<")
        return
    cmd = getattr(config, "SHUTDOWN_COMMAND", ["sudo", "shutdown", "-h", "now"])
    delay = float(getattr(config, "SHUTDOWN_DELAY_SECONDS", 5.0))
    logger.critical(">>> Shutdown requested from dashboard — relays off, then power off <<<")

    def _do_shutdown():
        _run_power_off_sequence(cmd, delay, "shutdown")

    threading.Thread(target=_do_shutdown, name="system-shutdown", daemon=False).start()


def _dashboard_get_state():
    """Return a JSON-serializable state snapshot for the live dashboard (relay state, phases, data, timing)."""
    now = time.time()
    green_elapsed = 0.0
    yellow_elapsed = 0.0
    if phase_sub_state == "green" and phase_start_time > 0:
        green_elapsed = max(0.0, now - phase_start_time)
    elif phase_sub_state == "yellow" and yellow_start_time > 0:
        yellow_elapsed = max(0.0, now - yellow_start_time)
    return {
        "last_relay_state": {s: list(ids) for s, ids in last_relay_state.items()},
        "relay_mappings": {k: [s, r] for k, (s, r) in config.RELAY_MAPPINGS.items()},
        "current_phase": current_phase,
        "in_phase": in_phase,
        "phases": {
            k: {
                "name": v["name"],
                "green_keys": v.get("green_keys", []),
                "yellow_keys": v.get("yellow_keys", []),
                "red_keys": v.get("red_keys", []),
                "trigger_keys": v["trigger_keys"],
            }
            for k, v in PHASES.items()
        },
        "data": {k: {"value": v["value"], "first_seen": v.get("first_seen", 0)} for k, v in data.items()},
        "last_served": dict(last_served),
        "phase_start_time": phase_start_time,
        "assigned_green_duration": assigned_green_duration,
        "green_elapsed_seconds": green_elapsed,
        "yellow_elapsed_seconds": yellow_elapsed,
        "server_time": now,
        "current_phase_trigger_count": current_phase_trigger_count,
        "control_mode": control_mode,
        "phase_sub_state": phase_sub_state,
        "yellow_start_time": yellow_start_time,
        "yellow_duration": getattr(config, "COORDINATOR_YELLOW_DURATION", 3.0),
        "manual_green_duration": getattr(config, "MANUAL_GREEN_DURATION", 90.0),
        "standby_mode": standby_mode,
    }

# -----------------------------------------------------------------------------
# Start dashboard in a daemon thread (serves /, /trafficator, /api/state, etc.)
# -----------------------------------------------------------------------------
try:
    import dashboard
    _dashboard_port = config.DASHBOARD_PORT
    _dashboard_host = config.DASHBOARD_HOST
    _dashboard_thread = threading.Thread(
        target=dashboard.run_dashboard,
        args=(_dashboard_get_state,),
        kwargs={
            "host": _dashboard_host,
            "port": _dashboard_port,
            "set_mode_fn": set_control_mode,
            "trigger_phase_fn": trigger_phase,
            "cancel_phase_fn": cancel_phase,
            "sleep_standby_fn": request_sleep_standby,
            "wake_standby_fn": request_wake_standby,
            "shutdown_fn": request_system_shutdown,
            "on_listen_fail": _handle_dashboard_listen_fail,
        },
        daemon=True,
    )
    _dashboard_thread.start()
    _url_host = "127.0.0.1" if _dashboard_host == "127.0.0.1" else _dashboard_host
    logger.info("Live dashboard: http://%s:%s/ (use 5001+ on macOS to avoid AirPlay on 5000)", _url_host, _dashboard_port)
except Exception as e:
    logger.warning("Dashboard not started: %s", e)

# -----------------------------------------------------------------------------
# Main loop: narrow centre check → standby → idle (select phase or wait) → run_phase
# -----------------------------------------------------------------------------
idle_all_off_sent = False
last_status_log_time = 0.0
STATUS_LOG_INTERVAL = 60.0

while not _shutdown:
    decay_old_counts()

    if wake_standby_request:
        with _control_lock:
            wake_standby_request = False
        standby_mode = False
        standby_data_snapshot = {}
        last_activity_time = time.time()
        logger.info("Wake: operator cleared standby — resuming relays")

    if not standby_mode:
        handle_pedestrian()  # Update pedestrian unit; in standby we leave all relays off.

    # Periodic status log (every STATUS_LOG_INTERVAL seconds)
    now = time.time()
    if now - last_status_log_time >= STATUS_LOG_INTERVAL:
        last_status_log_time = now
        phase_str = current_phase if current_phase else "idle"
        narrow = data['narrow_centre_vehicle_count']['value']
        logger.info(f"Status: phase={phase_str} in_phase={in_phase} narrow_centre={narrow} last_served={last_served}")

    # Dashboard Sleep: when idle, force standby (all relays off). If a phase is running, cancel_phase_request
    # aborts it first; this block runs on the next iteration once in_phase is False.
    if pending_sleep_standby and not in_phase:
        with _control_lock:
            pending_sleep_standby = False
            cancel_phase_request = False
        with _data_lock:
            standby_data_snapshot = {k: data[k].get("value", 0) for k in data}
        standby_mode = True
        force_all_relays_off()
        idle_all_off_sent = True
        logger.info("Sleep: operator standby — all relays off until counts change or manual trigger")
        _standby_wake_event.clear()
        _standby_wake_event.wait(timeout=0.2)
        continue

    # Block: do not start a new phase while narrow centre has vehicles; wait and re-check.
    if data['narrow_centre_vehicle_count']['value'] > 0:
        _standby_wake_event.clear()
        _standby_wake_event.wait(timeout=0.2)
        idle_all_off_sent = False
        continue

    # Idle (no phase running): handle standby, or run next/requested phase, or wait.
    if not in_phase:
        now = time.time()

        # Already in standby: re-apply all-off, or exit standby if activity detected.
        if standby_mode:
            with _control_lock:
                has_manual_request = manual_phase_request is not None
            with _data_lock:
                data_changed = any(data[k].get("value") != standby_data_snapshot.get(k) for k in data)
            if has_manual_request or data_changed:
                standby_mode = False
                standby_data_snapshot = {}
                logger.info("Standby cleared — activity detected, relays resuming")
            else:
                force_all_relays_off()
                _standby_wake_event.clear()
                _standby_wake_event.wait(timeout=0.2)  # Wake quickly when MQTT updates data
                continue

        # Enter standby if idle long enough (no phase running, no recent MQTT or manual activity).
        if (now - last_activity_time) >= IDLE_STANDBY_SECONDS:
            with _data_lock:
                standby_data_snapshot = {k: data[k].get("value", 0) for k in data}
            standby_mode = True
            force_all_relays_off()
            idle_all_off_sent = True
            logger.info("Standby: idle 30 min — all relays off until count change or sequence trigger")
            _standby_wake_event.clear()
            _standby_wake_event.wait(timeout=0.2)
            continue

        with _control_lock:
            mode = control_mode
            requested = manual_phase_request
            if requested is not None:
                manual_phase_request = None

        # Run phase: manual (requested) or auto (select_next_phase); otherwise wait.
        if mode == "manual" and requested is not None:
            current_phase = requested
            run_phase(requested)
            idle_all_off_sent = False
        elif mode == "auto":
            next_phase = select_next_phase()
            if next_phase and next_phase != current_phase:
                current_phase = next_phase
                run_phase(next_phase)
                idle_all_off_sent = False
            else:
                # Auto idle: no demand or same phase; leave relays as-is, wait for MQTT.
                _standby_wake_event.clear()
                _standby_wake_event.wait(timeout=0.2)
        else:
            # Manual idle: no request yet; wait so manual trigger is picked up quickly.
            _manual_wake_event.clear()
            _manual_wake_event.wait(timeout=0.3)
    else:
        # Phase is running: short tick so we stay responsive to shutdown and MQTT.
        _standby_wake_event.clear()
        _standby_wake_event.wait(timeout=0.2)

if _shutdown:
    logger.info("Main loop exited (shutdown).")