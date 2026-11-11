import sqlite3
from datetime import datetime, timedelta

from apsrtc_alerts import alerts
from apsrtc_alerts.config import settings
from apsrtc_alerts.storage import get_watch, init_db, mark_notified, save_watch


class FakeAPSRTC:
    def __init__(self, boarding_eta_ms):
        self.boarding_eta_ms = boarding_eta_ms

    def search_services(self, source, destination):
        return [{"serviceDocId": "service-doc", "oprsNo": "AP01", "journeyDate": datetime.now(settings.timezone).date().isoformat()}]

    def waypoint_history(self, service_doc_id):
        return [{
            "isTripStarted": True,
            "serviceStartDate": "2026-11-11",
            "vtsInfo": {"enableTracking": "0"},
            "waypoints": [
                {"seqNo": 1, "placeId": "previous", "wayPointName": "Previous Stop", "ETA": self.boarding_eta_ms - 1_200_000},
                {"seqNo": 2, "placeId": "101", "wayPointName": "Vijayawada", "ETA": self.boarding_eta_ms},
                {"seqNo": 3, "placeId": "202", "wayPointName": "Visakhapatnam", "ETA": self.boarding_eta_ms + 2_400_000},
            ],
        }]


def test_monitor_alert_deduplicates(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_path", tmp_path / "alerts.sqlite3")
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    init_db()
    source = {"placeId": "101", "linkPlaceId": "1101", "placeName": "Vijayawada"}
    destination = {"placeId": "202", "linkPlaceId": "2202", "placeName": "Visakhapatnam"}
    save_watch("morning", "987", source, destination)
    today = datetime.now(settings.timezone).date()
    frozen_now = [datetime.combine(today, datetime.min.time(), settings.timezone) + timedelta(hours=8, minutes=15)]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now[0]

    monkeypatch.setattr(alerts, "datetime", FrozenDateTime)
    arrival_ms = int((frozen_now[0] + timedelta(minutes=30)).timestamp() * 1000)
    client = FakeAPSRTC(arrival_ms)

    first = alerts.monitor_once(client)
    assert len(first) == 1
    assert first[0][0] == "morning"
    assert "Vijayawada" in first[0][3]
    assert "30 minutes" in first[0][3]
    assert "live GPS is not enabled" in first[0][3]

    mark_notified(first[0][2])
    frozen_now[0] += timedelta(minutes=15)
    second = alerts.monitor_once(client)
    assert len(second) == 1
    assert "15 minutes" in second[0][3]
    assert second[0][2] != first[0][2]

    mark_notified(second[0][2])
    assert alerts.monitor_once(client) == []


def test_morning_eta_window_allows_reminder_before_start(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_path", tmp_path / "window.sqlite3")
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    init_db()
    source = {"placeId": "101", "linkPlaceId": "1101", "placeName": "Vijayawada"}
    destination = {"placeId": "202", "linkPlaceId": "2202", "placeName": "Visakhapatnam"}
    save_watch("morning", "987", source, destination)

    today = datetime.now(settings.timezone).date()
    frozen_now = [datetime.combine(today, datetime.min.time(), settings.timezone) + timedelta(hours=7, minutes=30)]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now[0]

    monkeypatch.setattr(alerts, "datetime", FrozenDateTime)
    arrival_ms = int((frozen_now[0] + timedelta(minutes=30)).timestamp() * 1000)
    reminders = alerts.monitor_once(FakeAPSRTC(arrival_ms))
    assert len(reminders) == 1
    assert "30 minutes" in reminders[0][3]
    assert "08:00 AM" in reminders[0][3]


def test_morning_window_excludes_bus_arrivals_after_end(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_path", tmp_path / "outside-window.sqlite3")
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    init_db()
    source = {"placeId": "101", "linkPlaceId": "1101", "placeName": "Vijayawada"}
    destination = {"placeId": "202", "linkPlaceId": "2202", "placeName": "Visakhapatnam"}
    save_watch("morning", "987", source, destination)

    today = datetime.now(settings.timezone).date()
    frozen_now = [datetime.combine(today, datetime.min.time(), settings.timezone) + timedelta(hours=11)]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now[0]

    monkeypatch.setattr(alerts, "datetime", FrozenDateTime)
    arrival_ms = int((frozen_now[0] + timedelta(minutes=30)).timestamp() * 1000)
    assert alerts.monitor_once(FakeAPSRTC(arrival_ms)) == []


def test_legacy_database_migrates_existing_watch(monkeypatch, tmp_path):
    database_path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(settings, "database_path", database_path)
    with sqlite3.connect(database_path) as db:
        db.execute(
            """CREATE TABLE watches (
                chat_id TEXT PRIMARY KEY, source_id TEXT, source_link_id TEXT, source_name TEXT,
                destination_id TEXT, destination_link_id TEXT, destination_name TEXT, created_at TEXT
            )"""
        )
        db.execute("INSERT INTO watches VALUES ('987', '1', '11', 'Puttur', '2', '22', 'Pichator', 'today')")
    init_db()
    migrated = get_watch("987", "morning")
    assert migrated["source_name"] == "Puttur"
    assert migrated["destination_name"] == "Pichator"
