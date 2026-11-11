"""Environment-backed settings for the APSRTC tracker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _parse_clock_setting(name: str, default: str) -> time:
    value = os.getenv(name, default).strip()
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must use 24-hour HH:MM format") from exc


@dataclass
class Settings:
    busrtc_api_base: str = os.getenv(
        "BUSRTC_API_BASE", "https://utsappapicached01.apsrtconline.in/uts-vts-api"
    ).rstrip("/")
    busrtc_api_key: str = os.getenv(
        "BUSRTC_API_KEY",
        "5841383154776E4131454F4B663975336A4D416631384B3534396F65546E47644C39636148677A53722F773D",
    )
    busrtc_query_key: str = os.getenv("BUSRTC_QUERY_KEY", "BizZZ/rESHrW4kwbdQYiFZ4CCJIK3h8IIFsPxkTdouQ=")
    busrtc_user_id: str = os.getenv("BUSRTC_USER_ID", "1363789069449")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_evening_bot_token: str = os.getenv("TELEGRAM_EVENING_BOT_TOKEN", "").strip()
    bot_profile: str = os.getenv("BOT_PROFILE", "morning").strip().lower()
    alerts_api_url: str = os.getenv("ALERTS_API_URL", "http://127.0.0.1:8000").rstrip("/")
    database_path: Path = Path(os.getenv("DATABASE_PATH", "bus_alerts.sqlite3"))
    telegram_message_log_dir: Path = Path(os.getenv("TELEGRAM_MESSAGE_LOG_DIR", "logs"))
    morning_from_station: str = os.getenv("MORNING_FROM_STATION", "Puttur")
    morning_to_station: str = os.getenv("MORNING_TO_STATION", "Pichator")
    evening_from_station: str = os.getenv("EVENING_FROM_STATION", "Pichator")
    evening_to_station: str = os.getenv("EVENING_TO_STATION", "Puttur")
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "60"))
    alert_lead_minutes: tuple[int, ...] = tuple(
        sorted(
            {
                int(value.strip())
                for value in os.getenv("ALERT_LEAD_MINUTES", "30,15").split(",")
                if value.strip() and int(value.strip()) > 0
            },
            reverse=True,
        )
    )
    morning_alert_start: time = _parse_clock_setting("MORNING_ALERT_START", "08:00")
    morning_alert_end: time = _parse_clock_setting("MORNING_ALERT_END", "11:00")
    evening_alert_start: time = _parse_clock_setting("EVENING_ALERT_START", "15:00")
    evening_alert_end: time = _parse_clock_setting("EVENING_ALERT_END", "18:00")
    timezone: timezone = timezone(timedelta(hours=5, minutes=30), "IST")

    def __post_init__(self) -> None:
        # Render's private hostport references are formatted as "host:port".
        # Accept those directly while keeping local and public URLs unchanged.
        if not self.alerts_api_url.startswith(("http://", "https://")):
            self.alerts_api_url = f"http://{self.alerts_api_url}"

    def token_for(self, profile: str) -> str:
        return self.telegram_evening_bot_token if profile == "evening" else self.telegram_bot_token

    def route_for(self, profile: str) -> tuple[str, str]:
        if profile == "evening":
            return self.evening_from_station, self.evening_to_station
        return self.morning_from_station, self.morning_to_station

    def alert_window_for(self, profile: str) -> tuple[time, time]:
        if profile == "evening":
            return self.evening_alert_start, self.evening_alert_end
        return self.morning_alert_start, self.morning_alert_end

    def arrival_in_alert_window(self, profile: str, arrival_time: time) -> bool:
        start, end = self.alert_window_for(profile)
        if start <= end:
            return start <= arrival_time <= end
        return arrival_time >= start or arrival_time <= end


settings = Settings()
