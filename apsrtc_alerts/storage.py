"""SQLite persistence for active route watches and delivered alerts."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .config import settings


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.database_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with connect() as db:
        existing = db.execute("PRAGMA table_info(watches)").fetchall()
        if existing and "bot_profile" not in {row["name"] for row in existing}:
            # Preserve previous single-bot watches as morning-profile watches.
            db.execute("DROP TABLE IF EXISTS watches_new")
            db.execute(
                """CREATE TABLE watches_new (
                    bot_profile TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_link_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    destination_id TEXT NOT NULL,
                    destination_link_id TEXT NOT NULL,
                    destination_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (bot_profile, chat_id)
                )"""
            )
            db.execute(
                """INSERT INTO watches_new
                   SELECT 'morning', chat_id, source_id, source_link_id, source_name,
                          destination_id, destination_link_id, destination_name, created_at
                   FROM watches"""
            )
            db.execute("DROP TABLE watches")
            db.execute("ALTER TABLE watches_new RENAME TO watches")
        else:
            db.execute(
                """CREATE TABLE IF NOT EXISTS watches (
                    bot_profile TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_link_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    destination_id TEXT NOT NULL,
                    destination_link_id TEXT NOT NULL,
                    destination_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (bot_profile, chat_id)
                )"""
            )
        db.execute("CREATE TABLE IF NOT EXISTS notified (alert_key TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")


def serialize_watch(watch: sqlite3.Row | dict) -> dict:
    return {
        "bot_profile": watch["bot_profile"],
        "chat_id": str(watch["chat_id"]),
        "from_station": {"id": str(watch["source_id"]), "name": watch["source_name"]},
        "to_station": {"id": str(watch["destination_id"]), "name": watch["destination_name"]},
        "created_at": watch["created_at"],
    }


def get_watch(chat_id: str, bot_profile: str = "morning") -> sqlite3.Row | None:
    with connect() as db:
        return db.execute(
            "SELECT * FROM watches WHERE bot_profile=? AND chat_id=?", (bot_profile, chat_id)
        ).fetchone()


def save_watch(bot_profile: str, chat_id: str, source: dict, destination: dict) -> dict:
    from .config import settings

    created_at = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            """INSERT OR REPLACE INTO watches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bot_profile,
                chat_id,
                str(source["placeId"]),
                str(source.get("linkPlaceId") or source["placeId"]),
                source["placeName"],
                str(destination["placeId"]),
                str(destination.get("linkPlaceId") or destination["placeId"]),
                destination["placeName"],
                created_at,
            ),
        )
    return serialize_watch(get_watch(chat_id, bot_profile))


def remove_watch(chat_id: str, bot_profile: str = "morning") -> bool:
    with connect() as db:
        cursor = db.execute(
            "DELETE FROM watches WHERE bot_profile=? AND chat_id=?", (bot_profile, chat_id)
        )
        return cursor.rowcount > 0


def all_watches() -> list[sqlite3.Row]:
    with connect() as db:
        return list(db.execute("SELECT * FROM watches"))


def already_notified(alert_key: str) -> bool:
    with connect() as db:
        return db.execute("SELECT 1 FROM notified WHERE alert_key=?", (alert_key,)).fetchone() is not None


def mark_notified(alert_key: str) -> None:
    from .config import settings

    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO notified VALUES (?, ?)",
            (alert_key, datetime.now(settings.timezone).isoformat(timespec="seconds")),
        )
