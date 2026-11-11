"""Run the API and both Telegram bot profiles as one hosted service."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


async def _stop_processes(processes: list[asyncio.subprocess.Process]) -> None:
    for process in processes:
        if process.returncode is None:
            process.terminate()

    try:
        await asyncio.wait_for(
            asyncio.gather(*(process.wait() for process in processes)), timeout=15
        )
    except TimeoutError:
        for process in processes:
            if process.returncode is None:
                process.kill()
        await asyncio.gather(*(process.wait() for process in processes))


async def main() -> int:
    port = os.getenv("PORT", "8000")
    base_env = os.environ.copy()
    api_url = (
        os.getenv("ALERTS_API_URL", "").strip()
        or f"http://127.0.0.1:{port}"
    ).rstrip("/")

    for token_name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_EVENING_BOT_TOKEN"):
        if not base_env.get(token_name, "").strip():
            log.error("%s must be set to start both Telegram bot profiles", token_name)
            return 1

    commands = [
        (
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apsrtc_alerts.api:app",
                "--host",
                "0.0.0.0",
                "--port",
                port,
            ],
            base_env,
        )
    ]
    for profile in ("morning", "evening"):
        bot_env = base_env.copy()
        bot_env["BOT_PROFILE"] = profile
        bot_env["ALERTS_API_URL"] = api_url
        commands.append(
            (f"{profile}-bot", [sys.executable, "-m", "apsrtc_alerts.bot"], bot_env)
        )

    processes: list[asyncio.subprocess.Process] = []
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, shutdown.set)
        except NotImplementedError:
            signal.signal(
                signum,
                lambda _signal_number, _frame: loop.call_soon_threadsafe(shutdown.set),
            )

    try:
        for name, command, env in commands:
            process = await asyncio.create_subprocess_exec(*command, env=env)
            processes.append(process)
            log.info("Started %s process (pid=%s)", name, process.pid)

        waiters = [asyncio.create_task(process.wait()) for process in processes]
        shutdown_waiter = asyncio.create_task(shutdown.wait())
        done, _pending = await asyncio.wait(
            [*waiters, shutdown_waiter], return_when=asyncio.FIRST_COMPLETED
        )

        if shutdown_waiter in done:
            log.info("Shutdown requested; stopping API and both Telegram bots")
            return 0

        finished_index = waiters.index(next(task for task in done if task in waiters))
        finished_code = processes[finished_index].returncode
        log.error("A hosted service process exited unexpectedly with code %s", finished_code)
        return finished_code or 1
    finally:
        for task in [*locals().get("waiters", []), locals().get("shutdown_waiter")]:
            if task is not None and not task.done():
                task.cancel()
        await _stop_processes(processes)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
