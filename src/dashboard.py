# dashboard.py — production-ready live visualization server for coordinator state.
# Serves HTML dashboard, /api/state, /api/config (camera app URL), mode and trigger_phase.
# Camera streaming runs in a separate app (camera_app.py) on CAMERA_APP_PORT.
# Run via DashboardServer in a daemon thread from coordinator.py.

import logging
import time
import threading
import sys
from urllib.parse import quote

import config
from pathlib import Path
from typing import Dict, Optional

from flask import Flask, jsonify, request, send_from_directory, Response, session, redirect, url_for

logger = logging.getLogger(__name__)

# Rate limit: IP -> list of failed login timestamps; prune older than lockout window
_login_failures: Dict[str, list] = {}
_login_failures_lock = threading.Lock()

# Optional: fingerprint -> last POST /api/lane-location sample (ops / debugging). Lane-status uses /api/geofences + client-side GPS.
_lane_tracking: Dict[str, dict] = {}
_lane_tracking_lock = threading.Lock()


def _point_in_polygon(lat: float, lon: float, polygon: list) -> bool:
    """Ray casting: polygon is list of [lat, lon] in order A → B → D → C → A."""
    if not polygon or len(polygon) < 3:
        return False
    x, y = lon, lat
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][1], polygon[i][0]
        xj, yj = polygon[j][1], polygon[j][0]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

STATIC_DIR = Path(__file__).resolve().parent / "static"

# -----------------------------------------------------------------------------
# Lamp color logic
# -----------------------------------------------------------------------------

def _build_lamp_state(state):
    """Build list of lamps with on/off and display color from a coordinator state snapshot."""
    if not state:
        return []
    last_relay_state = state.get("last_relay_state") or {}
    by_server = {s: set(rids) for s, rids in last_relay_state.items()}
    lamps = []
    relay_mappings = state.get("relay_mappings") or {}
    for lamp_id, sr in relay_mappings.items():
        server, rid = sr[0], sr[1]
        on = rid in by_server.get(server, set())
        if "green" in lamp_id:
            color = "green" if on else "off"
        elif "yellow" in lamp_id:
            color = "yellow" if on else "off"
        else:
            color = "red_active" if on else "red"
        lamps.append({"id": lamp_id, "on": on, "color": color})
    return lamps


# -----------------------------------------------------------------------------
# Decision calculator
# -----------------------------------------------------------------------------

GREEN_MIN     = 12
GREEN_MAX     = 55
DENSITY_FACTOR = 2.5


def _fmt_ago(ts):
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 60:
        return f"{delta:.1f}s ago"
    return f"{delta / 60:.1f}m ago"


def _build_decision(state):
    phases_cfg    = state.get("phases", {})
    counts        = state.get("data", {})
    last_served   = state.get("last_served", {})
    current_phase = state.get("current_phase")
    in_phase      = state.get("in_phase", False)

    narrow_count   = counts.get("narrow_centre_vehicle_count", {}).get("value", 0)
    narrow_blocked = narrow_count > 0

    total_density = sum(
        v.get("value", 0) for k, v in counts.items()
        if "vehicle" in k and k != "narrow_centre_vehicle_count"
    )
    est_green = max(GREEN_MIN, min(GREEN_MAX, GREEN_MIN + int(total_density * DENSITY_FACTOR)))

    analysis   = []
    candidates = []

    for pkey in ["seq1", "seq2", "seq3"]:
        pcfg         = phases_cfg.get(pkey, {})
        trigger_keys = pcfg.get("trigger_keys", [])
        trigger_details = []
        for tk in trigger_keys:
            entry = counts.get(tk, {})
            trigger_details.append({
                "key":        tk,
                "value":      entry.get("value", 0),
                "first_seen": entry.get("first_seen", 0.0),
                "active":     entry.get("value", 0) > 0,
            })

        has_demand = any(t["active"] for t in trigger_details)
        density    = sum(t["value"] for t in trigger_details)
        arrivals   = [t["first_seen"] for t in trigger_details if t["first_seen"] > 0]
        earliest   = min(arrivals) if arrivals else 0.0
        served_at  = last_served.get(pkey, 0.0)

        elimination_reason = None
        if not has_demand:
            elimination_reason = "No demand — all trigger counts are 0"
        elif narrow_blocked:
            elimination_reason = f"Narrow centre blocked ({narrow_count} vehicle(s))"

        entry = {
            "phase_key":               pkey,
            "name":                    pcfg.get("name", pkey),
            "trigger_details":         trigger_details,
            "has_demand":              has_demand,
            "density":                 density,
            "earliest_arrival":        earliest,
            "earliest_arrival_ago":    _fmt_ago(earliest),
            "last_served":             served_at,
            "last_served_ago":         _fmt_ago(served_at),
            "estimated_green_duration": est_green,
            "is_current":              pkey == current_phase,
            "is_winner":               False,
            "elimination_reason":      elimination_reason,
        }
        analysis.append(entry)

        if has_demand and not narrow_blocked:
            candidates.append((served_at, earliest, density, pkey, entry))

    candidates.sort(key=lambda x: (x[0], x[1], -x[2]))

    winner        = None
    winner_reason = "No phases have demand"

    if narrow_blocked:
        winner_reason = f"All phases blocked — narrow centre has {narrow_count} vehicle(s)"
    elif in_phase:
        winner_reason = f"Sequence {current_phase} is currently running"
        winner = current_phase
    elif candidates:
        winner = candidates[0][3]
        if len(candidates) == 1:
            winner_reason = "Only phase with demand"
        elif candidates[0][0] < candidates[1][0]:
            winner_reason = (
                f"Least recently served — last ran {_fmt_ago(candidates[0][0])} ago "
                f"vs {_fmt_ago(candidates[1][0])} ago for {candidates[1][3]}"
            )
        elif candidates[0][1] < candidates[1][1]:
            winner_reason = (
                f"FCFS — earliest arrival {_fmt_ago(candidates[0][1])} ago "
                f"vs {_fmt_ago(candidates[1][1])} ago for {candidates[1][3]}"
            )
        elif candidates[0][2] > candidates[1][2]:
            winner_reason = (
                f"Highest density — {candidates[0][2]} vehicles "
                f"vs {candidates[1][2]} for {candidates[1][3]}"
            )
        else:
            winner_reason = "All scores equal — defaulting to sequence order"

        for a in analysis:
            if a["phase_key"] == winner:
                a["is_winner"] = True

    return {
        "phases_analysis":        analysis,
        "winner":                 winner,
        "winner_reason":          winner_reason,
        "narrow_blocked":         narrow_blocked,
        "narrow_count":           narrow_count,
        "in_phase":               in_phase,
        "estimated_green_duration": est_green,
        "total_density":          total_density,
    }


# -----------------------------------------------------------------------------
# Trafficator login page (HTML)
# -----------------------------------------------------------------------------
_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trafficator — Login</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: system-ui, sans-serif; margin: 0; padding: 2rem; background: #1a1b26; color: #c0caf5; min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
    .card {{ background: #24283b; border-radius: 12px; padding: 2rem; border: 1px solid #414868; max-width: 320px; width: 100%; }}
    h1 {{ font-size: 1.25rem; color: #7aa2f7; margin: 0 0 1rem 0; }}
    label {{ display: block; font-size: 0.9rem; margin-bottom: 0.25rem; color: #a9b1d6; }}
    input {{ width: 100%; padding: 0.5rem; margin-bottom: 1rem; border-radius: 6px; border: 1px solid #414868; background: #1a1b26; color: #c0caf5; font-size: 1rem; }}
    button {{ width: 100%; padding: 0.6rem; font-size: 1rem; border-radius: 8px; border: none; background: #7aa2f7; color: #1a1b26; font-weight: 600; cursor: pointer; }}
    button:hover {{ background: #89b4fa; }}
    .error {{ color: #f7768e; font-size: 0.9rem; margin-bottom: 1rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Trafficator — Login</h1>
    <p id="err" class="error"></p>
    <form method="post" action="/trafficator/login?next={next}">
      <label for="username">Username</label>
      <input id="username" name="username" type="text" required autocomplete="username">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" required autocomplete="current-password">
      <button type="submit">Log in</button>
    </form>
  </div>
  <script>
    var err = "{error}";
    if (err) document.getElementById("err").textContent =
      err === "invalid" ? "Invalid username or password." : err === "rate" ? "Too many attempts. Try again later." : err;
  </script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# Dashboard server
# -----------------------------------------------------------------------------

class DashboardServer:
    """Encapsulates the Flask dashboard app, state cache, and MJPEG proxies."""

    def __init__(
        self,
        get_state_fn,
        cache_ttl: float = 1.0,
        set_mode_fn=None,
        trigger_phase_fn=None,
        cancel_phase_fn=None,
        sleep_standby_fn=None,
        wake_standby_fn=None,
    ):
        self._get_state        = get_state_fn
        self._cache_ttl        = cache_ttl
        self._state_cache      = {"response": None, "ts": 0.0}
        self._set_mode_fn      = set_mode_fn
        self._trigger_phase_fn = trigger_phase_fn
        self._cancel_phase_fn  = cancel_phase_fn
        self._sleep_standby_fn = sleep_standby_fn
        self._wake_standby_fn  = wake_standby_fn
        self.app               = self._create_app()

    def _create_app(self):
        app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
        app.secret_key = getattr(config, "TRAFFICATOR_SECRET_KEY", "change-me")
        app.add_url_rule("/",                       "index",           self._index)
        app.add_url_rule("/trafficator",            "trafficator",     self._trafficator)
        app.add_url_rule("/trafficator/login",      "trafficator_login", self._trafficator_login, methods=["GET", "POST"])
        app.add_url_rule("/api/state",              "api_state",      self._api_state,   methods=["GET"])
        app.add_url_rule("/api/geofences",          "api_geofences",   self._api_geofences)
        app.add_url_rule("/api/lane-location",      "api_lane_location", self._api_lane_location, methods=["POST"])
        app.add_url_rule("/api/config",             "api_config",     self._api_config)
        app.add_url_rule("/api/mode",               "api_mode",        self._api_mode,    methods=["POST"])
        app.add_url_rule("/api/trigger_phase",      "api_trigger_phase", self._api_trigger_phase, methods=["POST"])
        app.add_url_rule("/api/cancel_phase",       "api_cancel_phase",  self._api_cancel_phase,  methods=["POST"])
        app.add_url_rule("/api/sleep_standby",      "api_sleep_standby", self._api_sleep_standby, methods=["POST"])
        app.add_url_rule("/api/wake_standby",       "api_wake_standby",  self._api_wake_standby,  methods=["POST"])
        app.add_url_rule("/health",                 "health",         self._health)

        @app.before_request
        def _require_trafficator_auth():
            if not request.path.startswith("/trafficator"):
                return None
            if request.path == "/trafficator/login":
                return None
            if session.get("trafficator_logged_in"):
                return None
            return redirect(url_for("trafficator_login", next=request.path))

        return app

    # ── Routes ───────────────────────────────────────────────────────────────

    def _index(self):
        """Default root: lane-status app."""
        return send_from_directory(STATIC_DIR, "lane-status.html")

    def _trafficator(self):
        """Operator dashboard (protected)."""
        return send_from_directory(STATIC_DIR, "dashboard.html")

    def _trafficator_login(self):
        """Login form and POST handler; rate limited."""
        max_attempts = getattr(config, "TRAFFICATOR_LOGIN_MAX_ATTEMPTS", 5)
        lockout_min = getattr(config, "TRAFFICATOR_LOCKOUT_MINUTES", 15)
        window = lockout_min * 60.0
        ip = request.remote_addr or "unknown"
        now = time.time()

        if request.method == "POST":
            with _login_failures_lock:
                # Prune old attempts for this IP
                attempts = _login_failures.get(ip, [])
                attempts = [t for t in attempts if now - t < window]
                if len(attempts) >= max_attempts:
                    return redirect(
                        url_for("trafficator_login", next=request.args.get("next", "/trafficator"), error="rate")
                    )
                if request.form:
                    username = request.form.get("username", "")
                    password = request.form.get("password", "")
                else:
                    data = request.get_json(silent=True) or {}
                    username = data.get("username", "")
                    password = data.get("password", "")
                ok = (
                    username == getattr(config, "TRAFFICATOR_USERNAME", "admin")
                    and password == getattr(config, "TRAFFICATOR_PASSWORD", "trafficator")
                )
                if ok:
                    _login_failures[ip] = []
                    session["trafficator_logged_in"] = True
                    next_url = request.args.get("next") or url_for("trafficator")
                    return redirect(next_url)
                attempts.append(now)
                _login_failures[ip] = attempts

            return redirect(url_for("trafficator_login", next=request.args.get("next", "/trafficator"), error="invalid"))

        # GET: show login page
        next_url = request.args.get("next", "/trafficator")
        error = request.args.get("error", "")
        error_js = error.replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\u003c")
        html = _LOGIN_HTML.format(next=quote(next_url, safe=""), error=error_js)
        return Response(html, mimetype="text/html")

    def _api_geofences(self):
        """Return lane geofences for the lane-status app. Each has sequence, name, and polygon.
        Polygon is 4 points [lat, lon] in order A → B → D → C → A (square)."""
        raw = getattr(config, "LANE_GEOFENCES", [])
        geofences = []
        for g in raw:
            if "A" in g and "B" in g and "C" in g and "D" in g:
                polygon = [g["A"], g["B"], g["D"], g["C"]]  # A → B → D → C → A
                geofences.append({
                    "sequence": g.get("sequence"),
                    "name": g.get("name", ""),
                    "polygon": polygon,
                })
            else:
                geofences.append(g)
        return jsonify({"geofences": geofences})

    def _api_lane_location(self):
        """POST { lat, lon }: resolve lane from geofences (always from current coordinates).
        Optional fingerprint (max 256 chars): stores last sample in _lane_tracking for ops only."""
        try:
            data = request.get_json(force=True, silent=True) or {}
            try:
                lat = float(data.get("lat"))
                lon = float(data.get("lon"))
            except (TypeError, ValueError):
                return jsonify({"error": "lat and lon required as numbers"}), 400

            fingerprint = (data.get("fingerprint") or "").strip()
            if len(fingerprint) > 256:
                return jsonify({"error": "fingerprint max 256 chars"}), 400

            raw = getattr(config, "LANE_GEOFENCES", [])
            geofences = []
            for g in raw:
                if "A" in g and "B" in g and "C" in g and "D" in g:
                    polygon = [g["A"], g["B"], g["D"], g["C"]]
                    geofences.append({"sequence": g.get("sequence"), "name": g.get("name", ""), "polygon": polygon})
            now = time.time()
            lane = None
            for g in geofences:
                if _point_in_polygon(lat, lon, g["polygon"]):
                    lane = g
                    break

            if fingerprint:
                with _lane_tracking_lock:
                    _lane_tracking[fingerprint] = {
                        "sequence": lane["sequence"] if lane else None,
                        "name": lane["name"] if lane else None,
                        "lat": lat,
                        "lon": lon,
                        "updated_at": now,
                    }

            if not lane:
                return jsonify({"error": "not_in_lane", "message": "You're not in a monitored lane."}), 404
            return jsonify({"sequence": lane["sequence"], "name": lane["name"]})
        except Exception as e:
            logger.exception("lane-location error: %s", e)
            return jsonify({"error": str(e)}), 500

    def _api_state(self):
        """Coordinator state JSON for dashboard and lane-status. Phases include unit_ids and derived
        green_keys, yellow_keys, red_keys (from TRAFFIC_LIGHT_UNITS). Lane-status uses current_phase,
        phase_sub_state, and timing fields to show red/green/yellow for the user's lane."""
        if not self._get_state:
            return jsonify({"error": "no state function registered"}), 503
        try:
            now = time.time()
            if (
                self._state_cache["response"] is not None
                and now - self._state_cache["ts"] < self._cache_ttl
            ):
                return self._state_cache["response"]

            state    = self._get_state()
            lamps    = _build_lamp_state(state)
            if state.get("standby_mode") and lamps:
                # Standby: all relays off for power save — show all lamps as off.
                lamps = [{"id": l["id"], "on": False, "color": "red" if "red" in l["id"] and "green" not in l["id"] and "yellow" not in l["id"] else "off"} for l in lamps]
            decision = _build_decision(state)

            raw_data = state.get("data", {})
            annotated_counts = {
                key: {**entry, "active": entry.get("value", 0) > 0}
                for key, entry in raw_data.items()
            }

            response = jsonify({
                "lamps":                        lamps,
                "current_phase":                state.get("current_phase"),
                "in_phase":                     state.get("in_phase", False),
                "phases":                       state.get("phases", {}),
                "data":                         annotated_counts,
                "last_served":                  state.get("last_served", {}),
                "phase_start_time":              state.get("phase_start_time", 0),
                "assigned_green_duration":      state.get("assigned_green_duration", 0),
                "current_phase_trigger_count":   state.get("current_phase_trigger_count", 0),
                "last_heartbeat":                state.get("last_heartbeat", now),
                "decision":                     decision,
                "control_mode":                 state.get("control_mode", "auto"),
                "phase_sub_state":             state.get("phase_sub_state"),
                "yellow_start_time":           state.get("yellow_start_time", 0),
                "yellow_duration":              state.get("yellow_duration", 3.0),
                "standby_mode":                 state.get("standby_mode", False),
            })
            self._state_cache["response"] = response
            self._state_cache["ts"]       = now
            return response

        except Exception as e:
            logger.exception("Dashboard state error: %s", e)
            return jsonify({"error": str(e)}), 500

    def _api_mode(self):
        """POST: set control mode to 'auto' or 'manual'. Body: {"mode": "auto"|"manual"}."""
        if not self._set_mode_fn:
            return jsonify({"error": "mode control not available"}), 501
        try:
            data = request.get_json(force=True, silent=True) or {}
            mode = (data.get("mode") or "").strip().lower()
            if mode not in ("auto", "manual"):
                return jsonify({"error": "mode must be 'auto' or 'manual'"}), 400
            self._set_mode_fn(mode)
            self._state_cache = {"response": None, "ts": 0.0}
            return jsonify({"ok": True, "mode": mode}), 200
        except Exception as e:
            logger.exception("Set mode error: %s", e)
            return jsonify({"error": str(e)}), 500

    def _api_trigger_phase(self):
        """POST: trigger a phase (seq1/seq2/seq3). Body: {"phase": "seq1"|"seq2"|"seq3"}. Only in manual mode."""
        if not self._trigger_phase_fn:
            return jsonify({"error": "trigger phase not available"}), 501
        try:
            data = request.get_json(force=True, silent=True) or {}
            phase = (data.get("phase") or "").strip().lower()
            if phase not in ("seq1", "seq2", "seq3"):
                return jsonify({"error": "phase must be seq1, seq2, or seq3"}), 400
            self._trigger_phase_fn(phase)
            self._state_cache = {"response": None, "ts": 0.0}
            return jsonify({"ok": True, "phase": phase}), 200
        except Exception as e:
            logger.exception("Trigger phase error: %s", e)
            return jsonify({"error": str(e)}), 500

    def _api_cancel_phase(self):
        """POST: cancel current phase and go to all-red (trigger all red)."""
        if not self._cancel_phase_fn:
            return jsonify({"error": "cancel phase not available"}), 501
        try:
            self._cancel_phase_fn()
            self._state_cache = {"response": None, "ts": 0.0}
            return jsonify({"ok": True}), 200
        except Exception as e:
            logger.exception("Cancel phase error: %s", e)
            return jsonify({"error": str(e)}), 500

    def _api_sleep_standby(self):
        """POST: cancel phases and enter standby (all relays off)."""
        if not self._sleep_standby_fn:
            return jsonify({"error": "sleep standby not available"}), 501
        try:
            self._sleep_standby_fn()
            self._state_cache = {"response": None, "ts": 0.0}
            return jsonify({"ok": True}), 200
        except Exception as e:
            logger.exception("Sleep standby error: %s", e)
            return jsonify({"error": str(e)}), 500

    def _api_wake_standby(self):
        """POST: exit standby and resume normal relay control."""
        if not self._wake_standby_fn:
            return jsonify({"error": "wake standby not available"}), 501
        try:
            self._wake_standby_fn()
            self._state_cache = {"response": None, "ts": 0.0}
            return jsonify({"ok": True}), 200
        except Exception as e:
            logger.exception("Wake standby error: %s", e)
            return jsonify({"error": str(e)}), 500

    def _api_config(self):
        """Return client config (e.g. camera app base URL for fetching streams)."""
        return jsonify({
            "camera_app_base_url": getattr(config, "CAMERA_APP_BASE_URL", "http://127.0.0.1:5004").rstrip("/"),
        })

    def _health(self):
        return jsonify({"status": "ok", "ts": time.time()}), 200

    # ── Runner ───────────────────────────────────────────────────────────────

    def run(self, host: str = "0.0.0.0", port: int = 5001):
        """Start MJPEG proxies and Flask server. Call from a daemon thread."""
        if not (STATIC_DIR / "dashboard.html").exists():
            logger.error(
                "Dashboard static file missing at %s — dashboard will not start.",
                STATIC_DIR / "dashboard.html",
            )
            return

        logger.info("Dashboard starting on http://%s:%s/ (camera streams on CAMERA_APP_PORT)", host, port)

        try:
            from waitress import serve  # type: ignore
            logger.info("Using waitress (production server)")
            # Need enough threads for 8+ MJPEG streams plus /api/state polling; default is 4.
            serve(self.app, host=host, port=port, threads=14)
        except ImportError:
            logger.warning(
                "waitress not installed — using Flask dev server. "
                "Install with: pip install waitress"
            )
            self.app.run(host=host, port=port, threaded=True, use_reloader=False)


# -----------------------------------------------------------------------------
# Public entry point — matches coordinator.py call signature
# -----------------------------------------------------------------------------

def run_dashboard(
    get_state_fn,
    host: str = "0.0.0.0",
    port: int = 5001,
    set_mode_fn=None,
    trigger_phase_fn=None,
    cancel_phase_fn=None,
    sleep_standby_fn=None,
    wake_standby_fn=None,
):
    """Create and run a DashboardServer in the current thread (call from daemon thread)."""
    server = DashboardServer(
        get_state_fn,
        set_mode_fn=set_mode_fn,
        trigger_phase_fn=trigger_phase_fn,
        cancel_phase_fn=cancel_phase_fn,
        sleep_standby_fn=sleep_standby_fn,
        wake_standby_fn=wake_standby_fn,
    )
    server.run(host=host, port=port)