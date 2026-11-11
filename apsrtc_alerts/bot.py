"""Telegram polling client. All station and watch operations use FastAPI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from .apsrtc import APSRTCError, match_places
from .config import settings
from .message_log import log_sent_message

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


class APIClient:
    def __init__(self, profile: str) -> None:
        self.profile = profile

    def _request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{settings.alerts_api_url}{path}",
            data=data,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if data else {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail", str(exc))
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = str(exc)
            raise APSRTCError(str(detail)) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise APSRTCError(f"Could not reach the local APSRTC API: {exc}") from exc

    def stations(self, query: str) -> list[dict]:
        path = "/stations?" + urllib.parse.urlencode({"q": query, "limit": 100})
        return self._request("GET", path)

    def create_watch(self, chat_id: str, source_id: str, destination_id: str):
        return self._request(
            "POST",
            "/watches",
            {
                "bot_profile": self.profile,
                "chat_id": chat_id,
                "from_station_id": source_id,
                "to_station_id": destination_id,
            },
        )

    def get_watch(self, chat_id: str):
        query = urllib.parse.urlencode({"bot_profile": self.profile})
        return self._request("GET", f"/watches/{urllib.parse.quote(chat_id, safe='')}?{query}")

    def delete_watch(self, chat_id: str):
        query = urllib.parse.urlencode({"bot_profile": self.profile})
        return self._request("DELETE", f"/watches/{urllib.parse.quote(chat_id, safe='')}?{query}")


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, payload: dict | None = None, timeout: int = 30) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Telegram {method} failed: {exc}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {result.get('description', 'unknown error')}")
        return result

    def send(self, chat_id: str, text: str) -> None:
        self.call("sendMessage", {"chat_id": chat_id, "text": text})
        log_sent_message(settings.bot_profile, chat_id, text)


def parse_pair(value: str) -> tuple[str, str] | None:
    parts = value.split("|", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip(), parts[1].strip()


def display_places(places: list[dict], max_count: int = 10) -> str:
    if not places:
        return "No matching APSRTC station was found. Try /places <part of station name>."
    return "\n".join(f"• {p.get('placeName')} — ID {p.get('placeId')}" for p in places[:max_count])


def resolve_route(api: APIClient, from_name: str, to_name: str) -> tuple[dict, dict] | None:
    from_matches = match_places(api.stations(from_name), from_name, 10)
    to_matches = match_places(api.stations(to_name), to_name, 10)
    if len(from_matches) == 1 and len(to_matches) == 1:
        return from_matches[0], to_matches[0]
    return None


async def handle_update(update: dict, telegram: TelegramClient, api: APIClient) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message or not message.get("text"):
        return
    chat_id = str(message["chat"]["id"])
    text = message["text"].strip()
    command, _, argument = text.partition(" ")
    command = command.split("@", 1)[0].lower()

    try:
        if command == "/chatid":
            telegram.send(chat_id, f"Your chat ID is {chat_id}. Use it as chat_id when creating a watch in Postman.")
        elif command == "/start":
            try:
                watch = api.get_watch(chat_id)
            except APSRTCError as exc:
                if "No active watch" not in str(exc):
                    raise
                from_name, to_name = settings.route_for(api.profile)
                route = resolve_route(api, from_name, to_name)
                if route is None:
                    telegram.send(
                        chat_id,
                        f"I couldn’t select the default route {from_name} → {to_name} uniquely. "
                        "Use /places to check station names, then /watch <boarding station> | <destination>.",
                    )
                    return
                watch = api.create_watch(chat_id, str(route[0]["placeId"]), str(route[1]["placeId"]))
                telegram.send(
                    chat_id,
                    f"Default {api.profile} route is active: {watch['from_station']['name']} → "
                    f"{watch['to_station']['name']}. Change it any time with /watch <boarding station> | <destination>.",
                )
                return
            telegram.send(
                chat_id,
                f"Your {api.profile} bot is active. Current route: {watch['from_station']['name']} → "
                f"{watch['to_station']['name']}. Change it any time with /watch <boarding station> | <destination>.",
            )
        elif command == "/help":
            from_name, to_name = settings.route_for(api.profile)
            telegram.send(
                chat_id,
                f"APSRTC {api.profile} stop alerts\n\n"
                f"Default route: {from_name} → {to_name}. /start activates it.\n"
                "Change it with /watch <boarding station> | <destination>.\n"
                "Use /places <station name> if names are ambiguous, then /watchid <boarding ID> | <destination ID>.\n"
                "/route shows your route; /stop disables alerts; /chatid shows your Telegram chat ID.\n\n"
                "Alerts use APSRTC waypoint ETAs. They are estimates; some services do not have live GPS tracking.",
            )
        elif command == "/places":
            if not argument.strip():
                telegram.send(chat_id, "Use /places <part of station name>.")
                return
            telegram.send(chat_id, display_places(api.stations(argument.strip())))
        elif command in ("/watch", "/watchid"):
            pair = parse_pair(argument) if argument.strip() else (
                None if command == "/watchid" else settings.route_for(api.profile)
            )
            if not pair:
                syntax = (
                    "/watch <boarding station> | <destination> (or /watch for the default route)"
                    if command == "/watch"
                    else "/watchid <boarding place ID> | <destination place ID>"
                )
                telegram.send(chat_id, f"Use {syntax}.")
                return
            if command == "/watchid":
                watch = api.create_watch(chat_id, pair[0], pair[1])
            else:
                route = resolve_route(api, pair[0], pair[1])
                if route is None:
                    from_options = match_places(api.stations(pair[0]), pair[0], 6)
                    to_options = match_places(api.stations(pair[1]), pair[1], 6)
                    telegram.send(
                        chat_id,
                        "Choose exact APSRTC place IDs with /watchid <boarding ID> | <destination ID>.\n\n"
                        f"Boarding station options:\n{display_places(from_options)}\n\n"
                        f"Destination options:\n{display_places(to_options)}",
                    )
                    return
                watch = api.create_watch(chat_id, str(route[0]["placeId"]), str(route[1]["placeId"]))
            telegram.send(
                chat_id,
                f"Watching {watch['from_station']['name']} → {watch['to_station']['name']}. "
                "I’ll alert you at 30 and 15 minutes before its estimated arrival at your boarding stop.",
            )
        elif command == "/route":
            try:
                watch = api.get_watch(chat_id)
            except APSRTCError as exc:
                if "No active watch" in str(exc):
                    telegram.send(chat_id, "No route is set. Send /start to activate the default route.")
                    return
                raise
            telegram.send(chat_id, f"Current {api.profile} route: {watch['from_station']['name']} → {watch['to_station']['name']}.")
        elif command == "/stop":
            try:
                api.delete_watch(chat_id)
            except APSRTCError as exc:
                if "No active watch" in str(exc):
                    telegram.send(chat_id, "There is no active route to stop.")
                    return
                raise
            telegram.send(chat_id, f"{api.profile.title()} route alerts stopped.")
        else:
            telegram.send(chat_id, "Use /help to see the APSRTC alert commands.")
    except APSRTCError as exc:
        log.warning("API command failed for chat %s: %s", chat_id, exc)
        telegram.send(chat_id, f"The APSRTC API could not complete that request: {exc}")


async def main() -> None:
    profile = settings.bot_profile
    if profile not in ("morning", "evening"):
        raise SystemExit("BOT_PROFILE must be either morning or evening.")
    token = settings.token_for(profile)
    if not token:
        token_variable = "TELEGRAM_EVENING_BOT_TOKEN" if profile == "evening" else "TELEGRAM_BOT_TOKEN"
        raise SystemExit(f"Set {token_variable} in .env before starting the {profile} bot.")
    telegram = TelegramClient(token)
    api = APIClient(profile)
    identity = telegram.call("getMe")["result"]
    log.info("Started %s bot @%s; using API at %s", profile, identity.get("username"), settings.alerts_api_url)
    offset = 0
    while True:
        try:
            result = await asyncio.to_thread(
                telegram.call,
                "getUpdates",
                {"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
                35,
            )
            for update in result.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                await handle_update(update, telegram, api)
        except Exception:
            log.exception("Telegram update polling failed")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Telegram client stopped")
