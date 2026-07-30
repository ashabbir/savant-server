"""Dedicated process entry point for graph maintenance scheduling."""

from __future__ import annotations

import signal
import threading

from .maintenance import start_maintenance_scheduler, stop_maintenance_scheduler


def main() -> None:
    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    start_maintenance_scheduler()
    stop_event.wait()
    stop_maintenance_scheduler()


if __name__ == "__main__":
    main()
