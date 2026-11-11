"""Client for APSRTC's public station, service, and waypoint endpoints."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings


class APSRTCError(RuntimeError):
    """Raised when APSRTC cannot fulfill a request."""


def encrypt_query(payload: dict) -> str:
    """Encode the compact, raw-deflate AES-GCM query used by the Flutter client."""
    payload = {key: value for key, value in payload.items() if key != "userId" and value is not None}
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    compressor = zlib.compressobj(level=6, wbits=-15)
    compressed = compressor.compress(plaintext) + compressor.flush()
    nonce = os.urandom(12)
    aes_key = base64.b64decode(settings.busrtc_query_key)
    if len(aes_key) != 32:
        raise APSRTCError("BUSRTC_QUERY_KEY must decode to 32 bytes")
    encrypted = AESGCM(aes_key).encrypt(nonce, compressed, None)
    return base64.urlsafe_b64encode(b"\x01" + nonce + encrypted).rstrip(b"=").decode("ascii")


def _http_json(url: str, *, headers: dict | None = None, timeout: int = 20) -> dict | list:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise APSRTCError(f"APSRTC request failed: {exc}") from exc
    if isinstance(result, dict) and result.get("errorCode"):
        raise APSRTCError(result.get("message", "APSRTC returned an error"))
    return result


class APSRTCClient:
    def __init__(self) -> None:
        self.headers = {
            "Authorization": "Bearer abhibus",
            "x-api-key": settings.busrtc_api_key,
            "userId": settings.busrtc_user_id,
        }
        self._places: tuple[float, list[dict]] | None = None
        self._services: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    def get_places(self) -> list[dict]:
        now = time.monotonic()
        if self._places and now - self._places[0] < 3600:
            return self._places[1]
        query = urllib.parse.urlencode({"q": encrypt_query({"q": ""})})
        result = _http_json(f"{settings.busrtc_api_base}/mappedplaces/fetchAllUniquePlaces?{query}", headers=self.headers)
        places = result.get("miniServicePlaces", []) if isinstance(result, dict) else []
        if not places:
            raise APSRTCError("APSRTC returned no stations")
        self._places = (now, places)
        return places

    def search_services(self, source: dict, destination: dict) -> list[dict]:
        key = (str(source["placeId"]), str(destination["placeId"]))
        now = time.monotonic()
        cached = self._services.get(key)
        if cached and now - cached[0] < 300:
            return cached[1]
        payload = {
            "sourceLinkId": str(source.get("linkPlaceId") or source["placeId"]),
            "destinationLinkId": str(destination.get("linkPlaceId") or destination["placeId"]),
            "sourcePlaceId": str(source["placeId"]),
            "destinationPlaceId": str(destination["placeId"]),
            "versionCode": None,
            "apiVersion": None,
            "isAirportService": 0,
        }
        query = urllib.parse.urlencode({"q": encrypt_query(payload)})
        result = _http_json(f"{settings.busrtc_api_base}/services/all?{query}", headers=self.headers)
        services = result.get("data", []) if isinstance(result, dict) else []
        self._services[key] = (now, services)
        return services

    def waypoint_history(self, service_doc_id: str) -> list[dict]:
        payload = urllib.parse.urlencode({"q": encrypt_query({"docId": service_doc_id, "hardRefresh": True})})
        result = _http_json(
            f"{settings.busrtc_api_base}/servicewaypointdetails/bydocid/history?{payload}",
            headers=self.headers,
        )
        return result.get("data", []) if isinstance(result, dict) else []

    def today_waypoints(self, service_doc_id: str) -> list[dict]:
        """Fetch the current waypoint snapshot used by APSRTC's live service view."""
        payload = urllib.parse.urlencode({"q": encrypt_query({"docId": service_doc_id, "hardRefresh": True})})
        result = _http_json(
            f"{settings.busrtc_api_base}/servicewaypointdetails/bydocid?{payload}",
            headers=self.headers,
        )
        return result.get("data", []) if isinstance(result, dict) else []


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def match_places(places: list[dict], query: str, limit: int = 10) -> list[dict]:
    term = normalize(query)
    exact = [place for place in places if normalize(str(place.get("placeName", ""))) == term]
    matches = exact or [place for place in places if term in normalize(str(place.get("placeName", "")))]
    return matches[:limit]


def station_by_id(client: APSRTCClient, place_id: str) -> dict | None:
    return next((place for place in client.get_places() if str(place.get("placeId")) == str(place_id)), None)
