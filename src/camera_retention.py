# camera_retention.py — delete camera recording files older than CAMERA_RECORD_RETENTION_DAYS (by mtime).
# Uses the same CAMERA_RECORD_DIR layout as camera_app.py: <dir>/<cam_id>/...
#
# Run once (e.g. daily cron):  python -m src.camera_retention
# Run as background daemon:    python -m src.camera_retention --daemon

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Allow `python -m src.camera_retention` or `python src/camera_retention.py` from repo root.
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import config

logger = logging.getLogger(__name__)


def _record_dir() -> Path:
    return Path(getattr(config, "CAMERA_RECORD_DIR", "camera_recordings")).expanduser()


def _retention_days() -> int:
    return int(getattr(config, "CAMERA_RECORD_RETENTION_DAYS", 30))


def _cleanup_interval_seconds() -> int:
    return max(300, int(getattr(config, "CAMERA_RECORD_CLEANUP_INTERVAL_SECONDS", 6 * 3600)))


def cleanup_old_recordings(
    record_dir: Path | None = None,
    retention_days: int | None = None,
) -> int:
    """
    Remove files under record_dir older than retention_days (mtime).
    Returns number of files removed. Empty nested dirs under each cam_id are removed best-effort.
    """
    record_dir = record_dir or _record_dir()
    days = retention_days if retention_days is not None else _retention_days()

    if days <= 0:
        logger.info("Retention disabled (CAMERA_RECORD_RETENTION_DAYS <= 0); nothing to do.")
        return 0
    if not record_dir.is_dir():
        logger.warning("Recording directory does not exist: %s", record_dir)
        return 0

    cutoff = time.time() - (days * 86400)
    removed = 0
    for cam_dir in sorted(record_dir.iterdir()):
        if not cam_dir.is_dir():
            continue
        for path in sorted(cam_dir.rglob("*"), reverse=True):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError as e:
                logger.warning("Retention: could not remove %s: %s", path, e)
        for path in sorted(cam_dir.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    if removed:
        logger.info(
            "Recording retention: removed %s file(s) older than %s days under %s",
            removed,
            days,
            record_dir,
        )
    else:
        logger.debug("Recording retention: no files older than %s days under %s", days, record_dir)

    return removed


def run_daemon_loop() -> None:
    """Run cleanup periodically until process exit (CAMERA_RECORD_CLEANUP_INTERVAL_SECONDS)."""
    interval = _cleanup_interval_seconds()
    days = _retention_days()
    logger.info(
        "Recording retention daemon: dir=%s, retention_days=%s, interval=%ss",
        _record_dir(),
        days,
        interval,
    )
    while True:
        try:
            cleanup_old_recordings()
        except Exception:
            logger.exception("Recording retention cleanup failed")
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete camera recordings older than retention days.")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run forever, cleaning every CAMERA_RECORD_CLEANUP_INTERVAL_SECONDS",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.daemon:
        if _retention_days() <= 0:
            logger.error("CAMERA_RECORD_RETENTION_DAYS is 0; daemon would do nothing. Exiting.")
            return 1
        run_daemon_loop()
        return 0

    cleanup_old_recordings()
    return 0


if __name__ == "__main__":
    sys.exit(main())
