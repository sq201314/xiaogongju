"""Persistent widget configuration (stored next to the project)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS: dict[str, Any] = {
    "x": None,
    "y": None,
    "mode": "compact",
    "locked": False,
    "lock_alpha": 0.5,
    "hidden": [],
    "orientation": "vertical",
    "ui_scale": 1.0,
    "topmost": True,
    "collapsed": {"wifi": False, "eth": False, "bt": True},
}


class Config(dict):
    def __init__(self) -> None:
        super().__init__(json.loads(json.dumps(DEFAULTS)))
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.update({k: v for k, v in data.items() if k in DEFAULTS})
            if not isinstance(self.get("collapsed"), dict):
                self["collapsed"] = dict(DEFAULTS["collapsed"])
            if not isinstance(self.get("hidden"), list):
                self["hidden"] = []

    def save(self) -> None:
        try:
            CONFIG_PATH.write_text(
                json.dumps(dict(self), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
