"""Logging plugin: prints/records events."""
from __future__ import annotations
from typing import Any
from core import BasePlugin, Event, SystemState
import json
import sys


class LoggingPlugin:
    def __init__(self, sink=None):
        self.sink = sink or sys.stdout

    def on_event(self, event: Event, state: SystemState) -> None:
        line = json.dumps(event.__dict__)
        self.sink.write(line + "\n")
        self.sink.flush()


__all__ = ["LoggingPlugin"]
