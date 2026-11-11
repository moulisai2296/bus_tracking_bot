"""FastAPI endpoints for APSRTC lookups and Telegram route watches."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .alerts import monitor_loop
from .apsrtc import APSRTCClient, APSRTCError, match_places, station_by_id
from .config import settings
from .storage import get_watch, init_db, remove_watch, save_watch, serialize_watch

log = logging.getLogger(__name__)


class WatchCreate(BaseModel):
    bot_profile: Literal["morning", "evening"] = "morning"
    chat_id: str = Field(min_length=1, max_length=100)
    from_station_id: str = Field(min_length=1, max_length=100)
    to_station_id: str = Field(min_length=1, max_length=100)

    @field_validator("chat_id", "from_station_id", "to_station_id", mode="before")
    @classmethod
    def trim_identifiers(cls, value):
        return value.strip() if isinstance(value, str) else value


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    client = APSRTCClient()
    app.state.apsrtc = client
    task = None
    if settings.telegram_bot_token or settings.telegram_evening_bot_token:
        task = asyncio.create_task(monitor_loop(client), name="apsrtc-alert-monitor")
    else:
        log.info("Alert monitor is idle; configure at least one Telegram bot token to send alerts")
    try:
        yield
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


app = FastAPI(
    title="APSRTC Bus Alerts API",
    description="Local API for APSRTC station and bus lookup plus Telegram boarding alerts.",
    version="1.0.0",
    lifespan=lifespan,
)


def _client(request: Request) -> APSRTCClient:
    return request.app.state.apsrtc


def _places_for_query(client: APSRTCClient, query: str, limit: int) -> list[dict]:
    places = client.get_places()
    if not query.strip():
        return places[:limit]
    return match_places(places, query, limit)


def _require_station(client: APSRTCClient, place_id: str) -> dict:
    station = station_by_id(client, place_id)
    if station is None:
        raise HTTPException(status_code=404, detail=f"APSRTC station ID {place_id!r} was not found")
    return station


@app.exception_handler(APSRTCError)
async def apsrtc_error_handler(_request: Request, exc: APSRTCError):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": str(exc), "source": "APSRTC"},
    )


@app.get("/health", tags=["system"])
def health():
    return {
        "status": "ok",
        "telegram_alerts_enabled": bool(settings.telegram_bot_token or settings.telegram_evening_bot_token),
        "bot_profiles": {
            "morning": bool(settings.telegram_bot_token),
            "evening": bool(settings.telegram_evening_bot_token),
        },
    }


@app.get("/stations", tags=["APSRTC lookup"])
def stations(
    request: Request,
    q: str = Query(default="", description="Station name or part of a name"),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return _places_for_query(_client(request), q, limit)


@app.get("/buses", tags=["APSRTC lookup"])
def buses(
    request: Request,
    from_station_id: Annotated[str, Query(min_length=1)],
    to_station_id: Annotated[str, Query(min_length=1)],
):
    client = _client(request)
    source = _require_station(client, from_station_id)
    destination = _require_station(client, to_station_id)
    if from_station_id == to_station_id:
        raise HTTPException(status_code=422, detail="Starting point and destination must be different")
    return {
        "from_station": {"id": from_station_id, "name": source.get("placeName")},
        "to_station": {"id": to_station_id, "name": destination.get("placeName")},
        "buses": client.search_services(source, destination),
    }


@app.get("/buses/{service_id}/stops", tags=["APSRTC lookup"])
def bus_stops(
    service_id: str,
    request: Request,
    travel_date: str | None = Query(default=None, description="Optional date in YYYY-MM-DD format"),
):
    if travel_date:
        from datetime import date

        try:
            date.fromisoformat(travel_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="travel_date must use YYYY-MM-DD format") from exc
    return {"service_id": service_id, "travel_date": travel_date, "trips": _client(request).waypoint_history(service_id)}


@app.get("/buses/{service_id}/today-stops", tags=["APSRTC lookup"])
def today_bus_stops(service_id: str, request: Request):
    """Return only APSRTC's current waypoint snapshot for this service."""
    today = datetime.now(settings.timezone).date()
    client = _client(request)
    live_stops = [
        point for point in client.today_waypoints(service_id) if isinstance(point, dict)
    ]
    if live_stops:
        live_stops.sort(key=lambda point: int(point.get("seqNo", 0) or 0))
        trip = {"serviceDocId": service_id, "waypoints": live_stops}
        stops = live_stops
        trips = [trip]
        data_source = "today_live_snapshot"
    else:
        stops = []
        trips = []
        data_source = "unavailable"
    return {
        "service_id": service_id,
        "date": today.isoformat(),
        "data_source": data_source,
        "tracking_available_today": bool(live_stops),
        "trip_count": len(trips),
        "stop_count": len(stops),
        "stops": stops,
        "trips": trips,
    }


@app.post("/watches", status_code=status.HTTP_201_CREATED, tags=["watches"])
def create_watch(body: WatchCreate, request: Request):
    client = _client(request)
    source = _require_station(client, body.from_station_id)
    destination = _require_station(client, body.to_station_id)
    if body.from_station_id == body.to_station_id:
        raise HTTPException(status_code=422, detail="Starting point and destination must be different")
    watch = save_watch(body.bot_profile, body.chat_id, source, destination)
    return {"status": "watching", **watch}


@app.get("/watches/{chat_id}", tags=["watches"])
def read_watch(
    chat_id: str,
    bot_profile: Literal["morning", "evening"] = Query(default="morning"),
):
    watch = get_watch(chat_id, bot_profile)
    if watch is None:
        raise HTTPException(status_code=404, detail="No active watch for this chat_id")
    return serialize_watch(watch)


@app.delete("/watches/{chat_id}", tags=["watches"])
def delete_watch(
    chat_id: str,
    bot_profile: Literal["morning", "evening"] = Query(default="morning"),
):
    if not remove_watch(chat_id, bot_profile):
        raise HTTPException(status_code=404, detail="No active watch for this chat_id")
    return {"status": "stopped", "chat_id": chat_id, "bot_profile": bot_profile}
