"""Benutzerentscheidungen zu Konflikten (aus der Review-Oberfläche), gespeichert als JSON."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path


class Decisions:
    """Format: { "<memory-id>": {"location": {"lat":..,"lon":..} | {"none": true}, "fit": "letterbox", ...} }"""

    def __init__(self, path: Path, data: dict | None = None):
        self.path = path
        self.data: dict[str, dict] = data or {}
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: Path) -> "Decisions":
        if path.exists():
            return cls(path, json.loads(path.read_text(encoding="utf-8")))
        return cls(path)

    def get(self, mid: str) -> dict:
        return self.data.get(mid, {})

    def set(self, mid: str, key: str, value) -> None:
        with self._lock:
            entry = self.data.setdefault(mid, {})
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
            entry["updated"] = datetime.now().isoformat(timespec="seconds")
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
