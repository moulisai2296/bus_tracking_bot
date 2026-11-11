from fastapi.testclient import TestClient

from apsrtc_alerts import api
from apsrtc_alerts.apsrtc import APSRTCClient, APSRTCError
from apsrtc_alerts.config import settings


PLACES = [
    {"placeId": "101", "linkPlaceId": "1101", "placeName": "Vijayawada"},
    {"placeId": "202", "linkPlaceId": "2202", "placeName": "Visakhapatnam"},
]


def setup_api(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.sqlite3")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_evening_bot_token", "")
    monkeypatch.setattr(APSRTCClient, "get_places", lambda self: PLACES)
    monkeypatch.setattr(APSRTCClient, "search_services", lambda self, source, destination: [{"serviceDocId": "svc-1", "oprsNo": "AP01"}])
    monkeypatch.setattr(APSRTCClient, "waypoint_history", lambda self, service_id: [{"serviceDocId": service_id}])
    monkeypatch.setattr(APSRTCClient, "today_waypoints", lambda self, service_id: [])
    return TestClient(api.app)


def test_health_station_search_bus_and_stops(monkeypatch, tmp_path):
    with setup_api(monkeypatch, tmp_path) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/stations", params={"q": "vijay"}).json() == [PLACES[0]]
        buses = client.get("/buses", params={"from_station_id": "101", "to_station_id": "202"})
        assert buses.status_code == 200
        assert buses.json()["buses"][0]["serviceDocId"] == "svc-1"
        stops = client.get("/buses/svc-1/stops", params={"travel_date": "2026-11-11"})
        assert stops.json()["trips"][0]["serviceDocId"] == "svc-1"


def test_watch_crud_and_unknown_ids(monkeypatch, tmp_path):
    with setup_api(monkeypatch, tmp_path) as client:
        body = {"chat_id": "987654", "from_station_id": "101", "to_station_id": "202"}
        created = client.post("/watches", json=body)
        assert created.status_code == 201
        assert created.json()["from_station"]["name"] == "Vijayawada"
        assert client.get("/watches/987654").json()["to_station"]["name"] == "Visakhapatnam"
        evening = client.post("/watches", json={**body, "bot_profile": "evening"})
        assert evening.status_code == 201
        assert client.get("/watches/987654", params={"bot_profile": "evening"}).json()["bot_profile"] == "evening"
        assert client.delete("/watches/987654").json()["status"] == "stopped"
        assert client.get("/watches/987654", params={"bot_profile": "evening"}).status_code == 200
        assert client.get("/watches/987654").status_code == 404
        assert client.get("/buses", params={"from_station_id": "missing", "to_station_id": "202"}).status_code == 404
        assert client.post("/watches", json={**body, "to_station_id": "101"}).status_code == 422
        assert client.post("/watches", json={**body, "chat_id": "  "}).status_code == 422


def test_apsrtc_failure_returns_502(monkeypatch, tmp_path):
    def fail(self):
        raise APSRTCError("upstream unavailable")

    monkeypatch.setattr(settings, "database_path", tmp_path / "test.sqlite3")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_evening_bot_token", "")
    monkeypatch.setattr(APSRTCClient, "get_places", fail)
    with TestClient(api.app) as client:
        response = client.get("/stations")
    assert response.status_code == 502
    assert response.json()["source"] == "APSRTC"


def test_stops_endpoint_reads_history_separately(monkeypatch, tmp_path):
    with setup_api(monkeypatch, tmp_path) as client:
        response = client.get("/buses/sample-service/stops")
    assert response.status_code == 200
    assert response.json()["trips"] == [{"serviceDocId": "sample-service"}]


def test_today_stops_prefers_live_bydocid_snapshot(monkeypatch, tmp_path):
    stops = [
        {"seqNo": 2, "wayPointName": "Pichator", "ETA": 1794338100000},
        {"seqNo": 1, "wayPointName": "Puttur", "vtsDepartureTime": 1794336420000},
    ]
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.sqlite3")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_evening_bot_token", "")
    monkeypatch.setattr(APSRTCClient, "today_waypoints", lambda self, service_id: stops)
    with TestClient(api.app) as client:
        response = client.get("/buses/11112026_OT14_2_SATYAVEEDU/today-stops")
    data = response.json()
    assert response.status_code == 200
    assert data["data_source"] == "today_live_snapshot"
    assert data["tracking_available_today"] is True
    assert data["trip_count"] == 1
    assert [stop["wayPointName"] for stop in data["stops"]] == ["Puttur", "Pichator"]


def test_today_stops_stays_empty_when_live_api_has_no_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.sqlite3")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_evening_bot_token", "")
    monkeypatch.setattr(APSRTCClient, "today_waypoints", lambda self, service_id: [])

    def history_must_not_be_used(self, service_id):
        raise AssertionError("Today's endpoint must not use historical data")

    monkeypatch.setattr(APSRTCClient, "waypoint_history", history_must_not_be_used)
    with TestClient(api.app) as client:
        response = client.get("/buses/sample-service/today-stops")
    data = response.json()
    assert response.status_code == 200
    assert data["data_source"] == "unavailable"
    assert data["tracking_available_today"] is False
    assert data["trip_count"] == 0
    assert data["stop_count"] == 0
    assert data["stops"] == []
