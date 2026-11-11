"""Record successful Telegram deliveries in per-profile JSON files."""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .config import settings

log = logging.getLogger(__name__)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Lock updates across the API and bot processes."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, 2)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def log_sent_message(profile: str, chat_id: str, text: str) -> None:
    """Record a message after Telegram confirms that it was sent."""
    record = {
        "timestamp": datetime.now(settings.timezone).isoformat(timespec="seconds"),
        "profile": profile,
        "chat_id": str(chat_id),
        "text": text,
        "status": "sent",
    }
    filename = "eveng_log.json" if profile == "evening" else "morning_log.json"
    path = settings.telegram_message_log_dir / filename
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(path):
            try:
                records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            except json.JSONDecodeError:
                log.warning("Resetting invalid Telegram message log %s", path)
                records = []
            if not isinstance(records, list):
                log.warning("Resetting Telegram message log with unexpected format: %s", path)
                records = []
            records.append(record)
            path.write_text(
                json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    except OSError:
        # A local log problem must not cause a successfully delivered alert to be retried.
        log.exception("Could not append Telegram message to %s", path)
