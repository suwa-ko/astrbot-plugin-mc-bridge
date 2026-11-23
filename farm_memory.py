"""Bounded local farm-plot memory.

A plot is a set of farmland cells, not a rectangle.  This is deliberate: the
fields around the base are irregular, and a cell set also handles terraces,
missing corners and water channels without special cases.  Only coordinates,
crop names and a short local label are stored; no chat text or player data.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

MAX_PLOTS = 8
MAX_CELLS = 256
MAX_WATER_CELLS = 64
CROPS = {"wheat", "carrots", "potatoes", "beetroots", "empty"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _coord(value):
    return max(-30_000_000, min(30_000_000, _int(value, 0)))


def _text(value, limit=64):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


class FarmMemory:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.plots: dict[str, dict] = {}
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw.get("plots") if isinstance(raw, dict) else None
            if not isinstance(rows, list):
                return
            for row in rows:
                plot = self._clean(row)
                if plot is not None:
                    self.plots[plot["id"]] = plot
        except Exception:
            self.plots = {}

    def save(self):
        payload = {"version": 1, "plots": list(self.plots.values())}
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _clean(self, row):
        if not isinstance(row, dict):
            return None
        plot_id = _text(row.get("id"), 32)
        if not _ID_RE.fullmatch(plot_id):
            return None
        cells = []
        seen = set()
        for raw in row.get("cells") or []:
            if not isinstance(raw, dict):
                continue
            x, y, z = _coord(raw.get("x")), _coord(raw.get("y")), _coord(raw.get("z"))
            key = (x, y, z)
            if key in seen:
                continue
            crop = _text(raw.get("crop"), 16).lower() or "empty"
            if crop not in CROPS:
                crop = "empty"
            seen.add(key)
            cells.append({"x": x, "y": y, "z": z, "crop": crop})
            if len(cells) >= MAX_CELLS:
                break
        if not cells:
            return None
        water = []
        water_seen = set()
        for raw in row.get("water_cells") or []:
            if not isinstance(raw, dict):
                continue
            x, y, z = _coord(raw.get("x")), _coord(raw.get("y")), _coord(raw.get("z"))
            key = (x, y, z)
            if key in water_seen:
                continue
            water_seen.add(key)
            water.append({"x": x, "y": y, "z": z})
            if len(water) >= MAX_WATER_CELLS:
                break
        xs = [cell["x"] for cell in cells]
        ys = [cell["y"] for cell in cells]
        zs = [cell["z"] for cell in cells]
        return {
            "id": plot_id,
            "name": _text(row.get("name"), 64) or plot_id,
            "world_key": _text(row.get("world_key"), 128),
            "dimension": _text(row.get("dimension"), 128),
            "cells": cells,
            "water_cells": water,
            "bounds": {
                "min_x": min(xs), "max_x": max(xs),
                "min_y": min(ys), "max_y": max(ys),
                "min_z": min(zs), "max_z": max(zs),
            },
            "shape": "irregular" if len(cells) != (max(xs) - min(xs) + 1) * (max(zs) - min(zs) + 1) else "rectangle",
            "updated_at": _int(row.get("updated_at"), int(time.time())),
        }

    def upsert(self, row):
        plot = self._clean(row)
        if plot is None:
            raise ValueError("invalid farm plot")
        if plot["id"] not in self.plots and len(self.plots) >= MAX_PLOTS:
            raise ValueError("farm plot limit reached")
        plot["updated_at"] = int(time.time())
        self.plots[plot["id"]] = plot
        self.save()
        return dict(plot)

    def get(self, plot_id):
        plot = self.plots.get(_text(plot_id, 32))
        return dict(plot) if plot else None

    def list(self):
        return [dict(plot) for plot in sorted(self.plots.values(), key=lambda row: str(row.get("id")))]

    def forget(self, plot_id):
        removed = self.plots.pop(_text(plot_id, 32), None)
        if removed is not None:
            self.save()
            return True
        return False
