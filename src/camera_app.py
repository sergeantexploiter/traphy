# camera_app.py — standalone camera streaming server (MJPEG proxy for RTSP).
# Serves /api/cameras and /stream/<cam_id> on CAMERA_APP_PORT. Uses config.CAMERA_FEEDS and config.FFMPEG_PATH.
# Optional disk recording: bounded queue + writer thread (does not block the capture/stream path).
# Run from coordinator as a daemon thread, or standalone: python -m src.camera_app

import logging
import os
import queue
import time
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import config
from flask import Flask, jsonify, Response

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Camera config from config.py
# -----------------------------------------------------------------------------
CAMERA_FEEDS = config.CAMERA_FEEDS
FFMPEG_PATH = config.FFMPEG_PATH

MJPEG_WIDTH = 640
MJPEG_HEIGHT = 360
MJPEG_FPS = 10
MJPEG_QUALITY = 5
MJPEG_RESTART_DELAY = 3.0

RECORD_ENABLED = getattr(config, "CAMERA_RECORD_ENABLED", False)
RECORD_DIR = Path(getattr(config, "CAMERA_RECORD_DIR", "camera_recordings")).expanduser()
RECORD_QUEUE_MAX = max(1, int(getattr(config, "CAMERA_RECORD_QUEUE_MAX", 2)))
RECORD_SEGMENT_MINUTES = max(1, int(getattr(config, "CAMERA_RECORD_SEGMENT_MINUTES", 60)))


def _feed_record_enabled(cam: dict) -> bool:
    if not RECORD_ENABLED:
        return False
    if cam.get("record") is False:
        return False
    return True


# -----------------------------------------------------------------------------
# MJPEG proxy — one instance per camera
# -----------------------------------------------------------------------------
class MJPEGProxy:
    """Wraps one RTSP stream as a thread-safe MJPEG frame buffer."""

    BOUNDARY = b"--mjpegframe"

    def __init__(self, cam_id: str, rtsp_url: str, record_enabled: bool = False):
        self.cam_id = cam_id
        self.rtsp_url = rtsp_url
        self._lock = threading.Lock()
        self._frame = None
        self._running = False
        self._thread = None
        self._record_enabled = bool(record_enabled)
        self._record_queue: Optional[queue.Queue] = None
        self._record_thread: Optional[threading.Thread] = None
        self._record_running = False

    def start(self):
        if self._running:
            return
        self._running = True
        if self._record_enabled:
            self._record_queue = queue.Queue(maxsize=RECORD_QUEUE_MAX)
            self._record_running = True
            self._record_thread = threading.Thread(
                target=self._record_writer_loop,
                daemon=True,
                name=f"record-{self.cam_id}",
            )
            self._record_thread.start()
            logger.info("Disk recording enabled for %s → %s", self.cam_id, RECORD_DIR)
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name=f"mjpeg-{self.cam_id}"
        )
        self._thread.start()
        logger.info("MJPEG proxy started: %s → %s", self.cam_id, self.rtsp_url)

    def stop(self):
        if self._record_queue is not None:
            try:
                self._record_queue.put_nowait(None)  # wake writer before clearing flags
            except queue.Full:
                pass
        self._record_running = False
        self._running = False

    def get_frame(self):
        with self._lock:
            return self._frame

    def stream(self):
        while True:
            frame = self.get_frame()
            if frame:
                yield (
                    self.BOUNDARY + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                    + frame + b"\r\n"
                )
            time.sleep(1.0 / MJPEG_FPS)

    def _build_ffmpeg_cmd(self):
        scale = ""
        if MJPEG_WIDTH and MJPEG_HEIGHT:
            scale = f",scale={MJPEG_WIDTH}:{MJPEG_HEIGHT}"
        return [
            FFMPEG_PATH,
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-vf", f"fps={MJPEG_FPS}{scale}",
            "-vcodec", "mjpeg",
            "-qscale:v", str(MJPEG_QUALITY),
            "-f", "mjpeg",
            "pipe:1",
        ]

    def _enqueue_record_frame(self, frame: bytes):
        """Non-blocking: drops frames if writer falls behind so streaming never waits on disk."""
        if not self._record_queue or not self._record_running:
            return
        try:
            self._record_queue.put_nowait(frame)
        except queue.Full:
            try:
                self._record_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._record_queue.put_nowait(frame)
            except queue.Full:
                pass

    def _record_writer_loop(self):
        """Dedicated thread: append JPEG frames to time-segmented .mjpeg files."""
        out_dir = RECORD_DIR / self.cam_id
        out_dir.mkdir(parents=True, exist_ok=True)
        fh = None
        segment_key = None

        def open_segment(now: datetime):
            nonlocal fh, segment_key
            day = now.strftime("%Y-%m-%d")
            total_min = now.hour * 60 + now.minute
            bucket = total_min // RECORD_SEGMENT_MINUTES
            key = (day, bucket)
            if key == segment_key and fh:
                return
            if fh:
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                except OSError:
                    pass
                fh.close()
                fh = None
            segment_key = key
            path = out_dir / day / f"{bucket:04d}.mjpeg"
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "ab", buffering=0)
            logger.info("Recording segment: %s", path)

        while True:
            try:
                item = self._record_queue.get(timeout=0.5)
            except queue.Empty:
                if not self._record_running:
                    break
                continue
            if item is None:
                break
            try:
                now = datetime.now()
                open_segment(now)
                if fh:
                    fh.write(item)
            except OSError as e:
                logger.error("Recording write error (%s): %s", self.cam_id, e)
                time.sleep(1.0)

        if fh:
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except OSError:
                pass
            fh.close()
        logger.info("Recording writer stopped: %s", self.cam_id)

    def _capture_loop(self):
        while self._running:
            proc = None
            try:
                cmd = self._build_ffmpeg_cmd()
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                buf = b""
                SOI = b"\xff\xd8"
                EOI = b"\xff\xd9"

                while self._running:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        start = buf.find(SOI)
                        if start == -1:
                            buf = b""
                            break
                        end = buf.find(EOI, start + 2)
                        if end == -1:
                            buf = buf[start:]
                            break
                        frame = buf[start: end + 2]
                        buf = buf[end + 2:]
                        with self._lock:
                            self._frame = frame
                        self._enqueue_record_frame(frame)

            except FileNotFoundError:
                logger.error("ffmpeg not found at %r — camera %s will not stream.", FFMPEG_PATH, self.cam_id)
                self._running = False
                return
            except Exception as e:
                logger.warning("MJPEG capture error (%s): %s — restarting", self.cam_id, e)
            finally:
                if proc and proc.poll() is None:
                    proc.kill()

            if self._running:
                time.sleep(MJPEG_RESTART_DELAY)


_proxies: Dict[str, MJPEGProxy] = {
    cam["id"]: MJPEGProxy(cam["id"], cam["url"], record_enabled=_feed_record_enabled(cam))
    for cam in CAMERA_FEEDS
    if cam.get("url")
}

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)


@app.after_request
def _cors_headers(response):
    """Allow dashboard (different port) to fetch /api/cameras and load stream URLs."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/api/cameras")
def api_cameras():
    """Return camera metadata. stream_url is full URL using CAMERA_APP_BASE_URL."""
    base = getattr(config, "CAMERA_APP_BASE_URL", "http://127.0.0.1:5004").rstrip("/")
    cameras = []
    for cam in CAMERA_FEEDS:
        cam_id = cam["id"]
        stream_url = f"{base}/stream/{cam_id}" if cam.get("url") else None
        seq = cam.get("sequence")
        if seq is None or seq == "" or (isinstance(seq, str) and seq.strip().lower() == "none"):
            seq = None
        cameras.append({
            "id": cam_id,
            "label": cam["label"],
            "available": cam.get("url") is not None,
            "full_width": cam.get("full_width", False),
            "stream_url": stream_url,
            "sequence": seq,
            "recording": _feed_record_enabled(cam) if cam.get("url") else False,
        })
    return jsonify({"cameras": cameras})


@app.route("/stream/<cam_id>")
def stream(cam_id: str):
    """MJPEG proxy stream. Proxies start lazily on first request."""
    proxy = _proxies.get(cam_id)
    if not proxy:
        return Response("Camera not found or not configured", status=404)
    proxy.start()
    return Response(
        proxy.stream(),
        mimetype="multipart/x-mixed-replace; boundary=mjpegframe",
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": time.time()}), 200


def run(host: str = None, port: int = None):
    """Run the camera app. Uses config.CAMERA_APP_HOST and config.CAMERA_APP_PORT if not given."""
    host = host or getattr(config, "CAMERA_APP_HOST", "0.0.0.0")
    port = port or getattr(config, "CAMERA_APP_PORT", 5004)
    logger.info("Camera app starting on http://%s:%s/ (%s cameras, lazy-start)", host, port, len(_proxies))
    try:
        from waitress import serve  # type: ignore
        serve(app, host=host, port=port, threads=14)
    except ImportError:
        app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
