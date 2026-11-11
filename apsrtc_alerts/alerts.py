"""ETA-based boarding alerts and the API process's monitor loop."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from .apsrtc import APSRTCClient
from .config import settings
from .message_log import log_sent_message
from .storage import all_watches, already_notified, mark_notified

log = logging.getLogger(__name__)


def station_label(waypoint: dict) -> str:
    return str(waypoint.get("wayPointName") or waypoint.get("stationName") or waypoint.get("placeName") or "")


def _to_milliseconds(value) -> int | None:
    try:
        if value is None:
            return None
        number = int(float(value))
        return number if number >= 10_000_000_000 else number * 1000
    except (TypeError, ValueError, OverflowError):
        return None


def _service_date(value: str | None) -> date | None:
    if not value:
        return None
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    return None


def _report_slot(now: datetime, profile: str) -> datetime | None:
    """Return the current 15-minute slot for a configured alert profile."""
    start, end = settings.alert_window_for(profile)
    slot_minute = now.minute - now.minute % 15
    slot = now.replace(minute=slot_minute, second=0, microsecond=0)
    if slot.time() < start or slot.time() > end:
        return None
    return slot


def _boarding_arrival(point: dict) -> datetime | None:
    eta_ms = _to_milliseconds(point.get("ETA"))
    if eta_ms is not None:
        return datetime.fromtimestamp(eta_ms / 1000, settings.timezone)
    return None


def _first_vts_departure_date(points: list[dict]) -> date | None:
    """Return the service's earliest recorded VTS departure date, if present."""
    departures = [
        departure_ms
        for point in points
        if (departure_ms := _to_milliseconds(point.get("vtsDepartureTime"))) is not None
    ]
    if not departures:
        return None
    earliest_departure_ms = min(departures)
    return datetime.fromtimestamp(earliest_departure_ms / 1000, settings.timezone).date()


def _markdown_cell(value: object) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", str(value))


def _profile_service_row(watch, service: dict, client: APSRTCClient, cutoff: datetime):
    service_doc_id = str(service.get("serviceDocId") or "")
    if not service_doc_id:
        return None, False
    try:
        points = client.today_waypoints(service_doc_id)
    except Exception as exc:
        log.debug("Today's waypoint snapshot unavailable for %s: %s", service_doc_id, exc)
        return None, False
    points = [point for point in points if isinstance(point, dict)]
    first_departure_date = _first_vts_departure_date(points)
    if first_departure_date is not None and first_departure_date < cutoff.date():
        log.debug(
            "Skipping %s because its first VTS departure is dated %s",
            service_doc_id,
            first_departure_date,
        )
        return None, False
    source_ids = {str(watch["source_id"]), str(watch["source_link_id"])}
    destination_ids = {str(watch["destination_id"]), str(watch["destination_link_id"])}
    source_name = " ".join(watch["source_name"].casefold().split())
    destination_name = " ".join(watch["destination_name"].casefold().split())
    source_names = {source_name}
    if source_name in {"pichator", "pichator-tirupathi"}:
        source_names = {"pichator", "pichator-tirupathi"}

    def matches(point: dict, ids: set[str], names: set[str]) -> bool:
        point_ids = {str(point.get(key, "")) for key in ("placeId", "stationId", "linkPlaceId")}
        point_name = " ".join(station_label(point).casefold().split())
        return bool(point_ids.intersection(ids)) or point_name in names

    origins = [point for point in points if matches(point, source_ids, source_names)]
    destinations = [
        point for point in points
        if matches(point, destination_ids, {destination_name})
    ]
    origins.sort(key=lambda point: int(point.get("seqNo", 0) or 0))
    has_boarding_time = False
    for origin in origins:
        sequence = int(origin.get("seqNo", 0) or 0)
        if not any(int(point.get("seqNo", 0) or 0) > sequence for point in destinations):
            continue
        arrival = _boarding_arrival(origin)
        if arrival is None:
            continue
        has_boarding_time = True
        if arrival <= cutoff or not settings.arrival_in_alert_window(watch["bot_profile"], arrival.time()):
            continue
        service_number = service.get("oprsNo") or service.get("dcpNo") or "Unavailable"
        service_type = service.get("serviceType") or "APSRTC"
        displayed_time = arrival.strftime("%I:%M %p").lstrip("0")
        return {
            "arrival": arrival,
            "service_number": service_number,
            "service_type": service_type,
            "boarding_time": displayed_time,
        }, True
    return None, has_boarding_time


def _profile_digest(watch, client: APSRTCClient, slot: datetime) -> str | None:
    """Build one upcoming-bus table from today's live waypoint snapshots."""
    source = {"placeId": watch["source_id"], "linkPlaceId": watch["source_link_id"]}
    destination = {"placeId": watch["destination_id"], "linkPlaceId": watch["destination_link_id"]}
    try:
        services = client.search_services(source, destination)
    except Exception as exc:
        log.warning("%s route lookup failed for chat %s: %s", watch["bot_profile"], watch["chat_id"], exc)
        return None
    services = [
        service for service in services
        if _service_date(service.get("journeyDate")) in (None, slot.date())
        and not service.get("isCancel") and not service.get("isClose")
        and service.get("serviceDocId")
    ]
    cutoff = slot
    rows = []
    has_live_boarding_data = False
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(_profile_service_row, watch, service, client, cutoff)
            for service in services
        ]
        for future in futures:
            try:
                row, has_time = future.result(timeout=35)
            except Exception as exc:
                log.debug("Today's snapshot unavailable: %s", exc)
                continue
            has_live_boarding_data = has_live_boarding_data or has_time
            if row:
                rows.append(row)
    if not has_live_boarding_data:
        return None
    rows.sort(key=lambda row: row["arrival"])
    message_time = slot.strftime("%I:%M %p").lstrip("0")
    threshold_time = cutoff.strftime("%I:%M %p").lstrip("0")
    _, arrival_window_end = settings.alert_window_for(watch["bot_profile"])
    end_time = arrival_window_end.strftime("%I:%M %p").lstrip("0")
    report_date = slot.strftime("%Y-%m-%d")
    lines = [
        f"## {_markdown_cell(watch['source_name'])} -> {_markdown_cell(watch['destination_name'])} | {report_date}",
        f"As of {message_time} IST --> {end_time} IST",
        "",
        "| Service No. | Boarding ETA | Service Type |",
        "| :--- | :--- | :--- |",
    ]
    if rows:
        for row in rows:
            lines.append(
                "| " + " | ".join(
                    _markdown_cell(row[key])
                    for key in ("service_number", "boarding_time", "service_type")
                ) + " |"
            )
    else:
        lines.append("| — | No buses remaining in this report window | — |")
    return "\n".join(lines)


def monitor_once(client: APSRTCClient) -> list[tuple[str, str, str, str | dict]]:
    now = datetime.now(settings.timezone)
    pending = []
    for watch in all_watches():
        profile = watch["bot_profile"]
        if not settings.token_for(profile):
            continue
        slot = _report_slot(now, profile)
        if slot is None:
            continue
        digest_key = f"{profile}:{watch['chat_id']}:digest:{slot.isoformat(timespec='minutes')}"
        if already_notified(digest_key):
            continue
        digest = _profile_digest(watch, client, slot)
        if digest:
            pending.append((profile, str(watch["chat_id"]), digest_key, {"rich_markdown": digest}))
    return pending


def send_telegram(profile: str, chat_id: str, text: str) -> None:
    token = settings.token_for(profile)
    if not token:
        raise RuntimeError(f"Telegram token for the {profile} profile is not configured")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    request = urllib.request.Request(
        url,
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Telegram sendMessage failed: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {result.get('description', 'unknown error')}")
    log_sent_message(profile, chat_id, text)


def send_telegram_rich(profile: str, chat_id: str, markdown: str) -> None:
    token = settings.token_for(profile)
    if not token:
        raise RuntimeError(f"Telegram token for the {profile} profile is not configured")
    url = f"https://api.telegram.org/bot{token}/sendRichMessage"
    request = urllib.request.Request(
        url,
        data=json.dumps({"chat_id": chat_id, "rich_message": {"markdown": markdown}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Telegram sendRichMessage failed: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendRichMessage failed: {result.get('description', 'unknown error')}")
    log_sent_message(profile, chat_id, markdown)


async def monitor_loop(client: APSRTCClient) -> None:
    while True:
        try:
            pending = await asyncio.to_thread(monitor_once, client)
            for profile, chat_id, key, message in pending:
                try:
                    if isinstance(message, dict):
                        await asyncio.to_thread(send_telegram_rich, profile, chat_id, message["rich_markdown"])
                    else:
                        await asyncio.to_thread(send_telegram, profile, chat_id, message)
                    mark_notified(key)
                except Exception as exc:
                    log.warning("Could not send %s alert to chat %s: %s", profile, chat_id, exc)
        except Exception:
            log.exception("Alert polling cycle failed")
        await asyncio.sleep(settings.poll_seconds)
