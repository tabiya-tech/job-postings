"""Entry point: `python -m app api` serves the HTTP API, `python -m app worker` processes tasks."""

import argparse
import asyncio
import logging
import signal

import uvicorn

from app.config import get_settings
from app.worker import run_worker


def _run_api() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.server:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


async def _run_worker() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    await run_worker(get_settings(), stop)


def main() -> None:
    """Start the process named on the command line."""
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("process", choices=["api", "worker"])
    args = parser.parse_args()

    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.process == "api":
        _run_api()
    else:
        asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
