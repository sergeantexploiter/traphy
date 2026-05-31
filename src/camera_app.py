# camera_app.py — standalone camera streaming server (MJPEG proxy for RTSP).
# Serves /api/cameras and /stream/<cam_id> on CAMERA_APP_PORT. Uses config.CAMERA_FEEDS and config.FFMPEG_PATH.
# Priority: one RTSP + one ffmpeg → MJPEG only for the live stream. Optional recording: same JPEG frames
# copied via a bounded non-blocking queue to a second ffmpeg (stdin MJPEG → H.264 segments); slow disk/encode
# drops recording frames only, never blocks streaming.
# RTSP/ffmpeg starts on first /stream/<id> client and stops when the last client disconnects (saves CPU when idle).
# Run standalone: python -m src.camera_app (or import run() from a thread).
# SIGINT/SIGTERM: stops proxies, joins recording threads (ffmpeg stdin EOF → finalize segments), then stops Waitress.
# Avoid SIGKILL while recording — it cannot flush the current segment.

import errno
import logging
import queue
import signal
import sys
import time
import threading
import subprocess
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
_RECORD_CONTAINER = (getattr(config, "CAMERA_RECORD_CONTAINER", "mp4") or "mp4").lower().lstrip(".")
if _RECORD_CONTAINER not in ("mp4", "mkv"):
    _RECORD_CONTAINER = "mp4"
RECORD_EXT = "." + _RECORD_CONTAINER
RECORD_PRESET = str(getattr(config, "CAMERA_RECORD_PRESET", "veryfast"))
RECORD_CRF = str(getattr(config, "CAMERA_RECORD_CRF", 23))


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
    """1× RTSP → ffmpeg → MJPEG (stream). Optional 2nd ffmpeg via queue for H.264 disk (non-blocking)."""

    BOUNDARY = b"--mjpegframe"

    def __init__(self, cam_id: str, rtsp_url: str, record_enabled: bool = False):
        self.cam_id = cam_id
        self.rtsp_url = rtsp_url
        self._lock = threading.Lock()
        self._frame = None
        self._running = False
        self._thread = None
        self._stream_proc_lock = threading.Lock()
        self._capture_proc: Optional[subprocess.Popen] = None
        self._viewers_lock = threading.Lock()
        self._stream_viewers = 0
        self._record_enabled = bool(record_enabled)
        self._record_queue: Optional[queue.Queue] = None
        self._record_thread: Optional[threading.Thread] = None
        self._record_running = False
        self._record_proc_lock = threading.Lock()
        self._record_proc: Optional[subprocess.Popen] = None

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
            logger.info(
                "Recording thread started (%s): queue max %s — stream path unaffected by disk lag",
                self.cam_id,
                RECORD_QUEUE_MAX,
            )
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name=f"mjpeg-{self.cam_id}"
        )
        self._thread.start()
        logger.info(
            "Stream pipeline (priority): 1× RTSP → MJPEG %s → %s",
            self.cam_id,
            self.rtsp_url,
        )

    def stop(self):
        """Stop capture and recording. Recording ffmpeg gets EOF on stdin so segments can finalize."""
        self._running = False
        self._record_running = False
        with self._stream_proc_lock:
            cap = self._capture_proc
        if cap is not None and cap.poll() is None:
            try:
                cap.terminate()
                cap.wait(timeout=12)
            except subprocess.TimeoutExpired:
                try:
                    cap.kill()
                    cap.wait(timeout=5)
                except OSError:
                    pass
            except OSError:
                pass
        if self._record_queue is not None:
            try:
                self._record_queue.put_nowait(None)
            except queue.Full:
                try:
                    self._record_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._record_queue.put_nowait(None)
                except queue.Full:
                    pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=25.0)

    def join_recording_thread(self, timeout: float = 120.0) -> None:
        """Wait for the recording writer to close ffmpeg cleanly (call after stop())."""
        t = self._record_thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                logger.warning("Recording thread for %s did not finish within %ss", self.cam_id, timeout)

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

    def _build_ffmpeg_stream_cmd(self) -> list:
        """One RTSP input → MJPEG on stdout only (live stream priority)."""
        scale_part = ""
        if MJPEG_WIDTH and MJPEG_HEIGHT:
            scale_part = f",scale={MJPEG_WIDTH}:{MJPEG_HEIGHT}"
        return [
            FFMPEG_PATH,
            "-hide_banner",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-vf", f"fps={MJPEG_FPS}{scale_part}",
            "-vcodec", "mjpeg",
            "-qscale:v", str(MJPEG_QUALITY),
            "-f", "mjpeg",
            "pipe:1",
        ]

    def _enqueue_record_frame(self, frame: bytes):
        """Non-blocking: never wait on disk/encoder; drop frames for recording if queue is full."""
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

    def _build_ffmpeg_record_cmd(self, out_pattern: str) -> list:
        """MJPEG on stdin → H.264 segmented files (separate process from stream)."""
        seg_secs = RECORD_SEGMENT_MINUTES * 60
        seg_fmt = "mp4" if _RECORD_CONTAINER == "mp4" else "matroska"
        cmd = [
            FFMPEG_PATH,
            "-hide_banner",
            "-loglevel", "error",
            "-f", "mjpeg",
            "-framerate", str(MJPEG_FPS),
            "-thread_queue_size", "512",
            "-i", "pipe:0",
            "-c:v", "libx264",
            "-preset", RECORD_PRESET,
            "-crf", RECORD_CRF,
            "-pix_fmt", "yuv420p",
            "-an",
        ]
        if _RECORD_CONTAINER == "mp4":
            cmd += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
        cmd += [
            "-f", "segment",
            "-segment_time", str(seg_secs),
            "-segment_format", seg_fmt,
            "-reset_timestamps", "1",
            out_pattern,
        ]
        return cmd

    def _record_writer_loop(self):
        """Separate thread + ffmpeg: consume queue, write MJPEG to ffmpeg stdin → segmented video."""
        out_dir = RECORD_DIR / self.cam_id
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(out_dir / f"rec_%03d{RECORD_EXT}")
        logger.info("Recorder ffmpeg segments → %s", pattern)

        while self._record_running:
            try:
                rec = subprocess.Popen(
                    self._build_ffmpeg_record_cmd(pattern),
                    stdin=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            except FileNotFoundError:
                logger.error("ffmpeg not found at %r — cannot record %s", FFMPEG_PATH, self.cam_id)
                return

            with self._record_proc_lock:
                self._record_proc = rec

            try:
                while self._record_running and rec.poll() is None:
                    try:
                        item = self._record_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if item is None:
                        self._record_running = False
                        break
                    if rec.stdin and rec.poll() is None:
                        rec.stdin.write(item)
            except BrokenPipeError:
                logger.warning("Recorder pipe broken (%s) — will restart ffmpeg if still running", self.cam_id)
            except OSError as e:
                logger.error("Recorder write error (%s): %s", self.cam_id, e)
            finally:
                with self._record_proc_lock:
                    if self._record_proc is rec:
                        self._record_proc = None
                stdin = rec.stdin
                if stdin:
                    try:
                        stdin.flush()
                    except (OSError, BrokenPipeError):
                        pass
                    try:
                        stdin.close()
                    except OSError:
                        pass
                # EOF lets ffmpeg flush the segment muxer; then SIGTERM if it hangs.
                try:
                    rec.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "Recorder ffmpeg for %s did not exit after stdin close; sending SIGTERM",
                        self.cam_id,
                    )
                    try:
                        rec.terminate()
                    except OSError:
                        pass
                    try:
                        rec.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        try:
                            rec.kill()
                        except OSError:
                            pass
                        try:
                            rec.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            pass

            if not self._record_running:
                break
            time.sleep(0.5)

        logger.info("Recording writer stopped: %s", self.cam_id)

    def _capture_loop(self):
        while self._running:
            proc = None
            try:
                cmd = self._build_ffmpeg_stream_cmd()
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                with self._stream_proc_lock:
                    self._capture_proc = proc
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
                with self._stream_proc_lock:
                    if self._capture_proc is proc:
                        self._capture_proc = None
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    except OSError:
                        try:
                            proc.kill()
                        except OSError:
                            pass

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


def _mjpeg_stream_with_viewers(proxy: MJPEGProxy):
    """Start capture on first viewer; stop RTSP/ffmpeg when the last client disconnects."""
    with proxy._viewers_lock:
        proxy._stream_viewers += 1
        if proxy._stream_viewers == 1:
            proxy.start()
    try:
        yield from proxy.stream()
    finally:
        with proxy._viewers_lock:
            proxy._stream_viewers -= 1
            if proxy._stream_viewers == 0:
                logger.info("Last viewer left %s — stopping stream pipeline", proxy.cam_id)
                proxy.stop()


@app.route("/stream/<cam_id>")
def stream(cam_id: str):
    """MJPEG proxy stream; RTSP capture runs only while at least one client is connected."""
    proxy = _proxies.get(cam_id)
    if not proxy:
        return Response("Camera not found or not configured", status=404)
    return Response(
        _mjpeg_stream_with_viewers(proxy),
        mimetype="multipart/x-mixed-replace; boundary=mjpegframe",
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": time.time()}), 200


def start_all_camera_proxies():
    """Start RTSP capture for every camera (optional warm-up; not called on normal app start)."""
    for cam_id, proxy in _proxies.items():
        try:
            proxy.start()
        except Exception as e:
            logger.exception("Failed to start camera proxy %s: %s", cam_id, e)


# Set by run() when using Waitress so SIGTERM/SIGINT can call close() after flushing recordings.
_waitress_server = None
_shutdown_lock = threading.Lock()
_shutdown_started = False


def stop_all_camera_proxies(*, recording_join_timeout: float = 120.0) -> None:
    """
    Stop all streams and recording pipelines so ffmpeg can finalize segments (stdin EOF, then wait/SIGTERM).
    Call this on SIGINT/SIGTERM before exiting the process.
    """
    global _shutdown_started
    with _shutdown_lock:
        if _shutdown_started:
            return
        _shutdown_started = True
    logger.info("Shutting down camera proxies (finalize recordings)...")
    for proxy in _proxies.values():
        proxy.stop()
    for proxy in _proxies.values():
        proxy.join_recording_thread(timeout=recording_join_timeout)
    logger.info("Camera proxy shutdown complete.")


def _on_shutdown_signal(signum, frame):
    stop_all_camera_proxies()
    srv = _waitress_server
    if srv is not None:
        try:
            srv.close()
        except Exception as e:
            logger.warning("Waitress close after shutdown: %s", e)
    # Must not return into Waitress's asyncore loop: sockets are closed and select() raises EBADF (Errno 9).
    sys.exit(0)


def _register_shutdown_signals() -> None:
    signal.signal(signal.SIGINT, _on_shutdown_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_shutdown_signal)


def run(host: str = None, port: int = None):
    """Run the camera app. Uses config.CAMERA_APP_HOST and config.CAMERA_APP_PORT if not given."""
    global _waitress_server
    host = host or getattr(config, "CAMERA_APP_HOST", "0.0.0.0")
    port = port or getattr(config, "CAMERA_APP_PORT", 5004)
    logger.info(
        "Camera app on http://%s:%s/ (%s cameras; RTSP starts on /stream/<id> request, stops when idle)",
        host,
        port,
        len(_proxies),
    )
    if RECORD_ENABLED:
        logger.info("Stop with SIGINT/SIGTERM so recordings finalize; SIGKILL can leave segments unreadable.")
    _register_shutdown_signals()
    try:
        from waitress import create_server  # type: ignore

        _waitress_server = create_server(app, host=host, port=port, threads=14)
        try:
            _waitress_server.run()
        except OSError as e:
            # If close() ran from a signal handler, the loop can raise EBADF before sys.exit runs.
            if e.errno == errno.EBADF and _shutdown_started:
                logger.debug("Waitress loop ended after shutdown (EBADF).")
            else:
                raise
    except ImportError:
        _waitress_server = None
        app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
