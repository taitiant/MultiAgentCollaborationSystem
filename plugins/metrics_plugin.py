"""Simple in-memory metrics collector."""
from __future__ import annotations
from typing import Dict
from core import BasePlugin, Event, SystemState
import collections


class MetricsPlugin:
    def __init__(self):
        self.counters = collections.Counter()

    def on_event(self, event: Event, state: SystemState) -> None:
        self.counters[f"event:{event.event_type}"] += 1

    def snapshot(self) -> Dict[str, int]:
        return dict(self.counters)


__all__ = ["MetricsPlugin"]
