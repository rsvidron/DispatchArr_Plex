"""
Stable Live-Channels → placeholder slot mapping for Plex (fixed dial order).

The playlist refresh creates new stream *rows*; we resolve each slot by **tvg_id**
or **normalized name** from `nufu_live_channels_mapping.json`, not by sorting.

Env:
  DISPATCHARR_NUFU_LIVE_CHANNELS_MAPPING — path to JSON (default: file beside this module)
  DISPATCHARR_NUFU_LIVE_CHANNELS_ORDERED_FALLBACK — if 1/true, use old alphabetical
    fill when the mapping file is missing (not stable for Plex)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

LOG_NAME = "nufu_live_channels_mapping"


def default_mapping_path() -> Path:
    return Path(__file__).resolve().parent / "nufu_live_channels_mapping.json"


def mapping_path_from_env() -> Path:
    raw = os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_MAPPING", "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_mapping_path()


def ordered_fallback_enabled() -> bool:
    v = os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_ORDERED_FALLBACK", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def norm_name(s: Any) -> str:
    t = str(s or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _parse_slots_obj(raw: dict[str, Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    slots = raw.get("slots")
    if slots is None:
        return out
    if isinstance(slots, list):
        for i, entry in enumerate(slots):
            if isinstance(entry, dict):
                out[i + 1] = dict(entry)
        return out
    if isinstance(slots, dict):
        for k, entry in slots.items():
            if not isinstance(entry, dict):
                continue
            try:
                slot = int(str(k).strip())
            except ValueError:
                continue
            if slot >= 1:
                out[slot] = dict(entry)
    return out


def load_slot_mapping(path: Path) -> dict[int, dict[str, Any]]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return _parse_slots_obj(data)


def save_initial_mapping(
    path: Path,
    *,
    streams: list[dict[str, Any]],
    max_slots: int,
    stream_group: str,
) -> None:
    """Write mapping from current API order (sorted by name) — edit file to reorder Plex slots."""
    streams = sorted(streams, key=lambda s: str(s.get("name") or "").lower())
    slots: dict[str, dict[str, Any]] = {}
    for i in range(min(len(streams), max_slots)):
        st = streams[i]
        slot = i + 1
        tvg = st.get("tvg_id")
        if isinstance(tvg, str) and tvg.strip():
            slots[str(slot)] = {"tvg_id": tvg.strip()}
        else:
            slots[str(slot)] = {"name": str(st.get("name") or "").strip()}
    payload = {
        "version": 1,
        "stream_group": stream_group,
        "comment": (
            "Each slot matches one stream after M3U refresh. Prefer tvg_id; name is fallback. "
            "Slots are Nufu Live Channels 01..NN (not channel numbers). Edit keys or add tvg_id for stability."
        ),
        "slots": slots,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def resolve_mapped_streams(
    slot_entries: dict[int, dict[str, Any]],
    streams: list[dict[str, Any]],
    *,
    max_slot: int,
) -> dict[int, Optional[dict[str, Any]]]:
    """
    For each slot 1..max_slot, pick at most one stream matching that slot's entry.
    Streams are claimed once (first slot wins) so duplicates surface as missed matches later.
    """
    by_tvg: dict[str, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for s in streams:
        sid = s.get("id")
        if not isinstance(sid, int):
            continue
        tvg = str(s.get("tvg_id") or "").strip().lower()
        if tvg:
            by_tvg.setdefault(tvg, []).append(s)
        nm = norm_name(s.get("name"))
        if nm:
            by_name.setdefault(nm, []).append(s)

    used: set[int] = set()
    out: dict[int, Optional[dict[str, Any]]] = {}

    for slot in range(1, max_slot + 1):
        entry = slot_entries.get(slot)
        if not entry:
            out[slot] = None
            continue

        tvg_key = entry.get("tvg_id")
        if isinstance(tvg_key, str) and tvg_key.strip():
            cands = list(by_tvg.get(tvg_key.strip().lower(), []))
        else:
            cands = []

        if not cands:
            name_key = entry.get("name")
            if isinstance(name_key, str) and name_key.strip():
                cands = list(by_name.get(norm_name(name_key.strip()), []))

        chosen: Optional[dict[str, Any]] = None
        for s in sorted(cands, key=lambda x: int(x.get("id") or 0)):
            sid = s.get("id")
            if isinstance(sid, int) and sid not in used:
                chosen = s
                used.add(sid)
                break
        out[slot] = chosen

    return out
