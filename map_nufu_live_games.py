#!/usr/bin/env python3
"""
Map Nufu M3U streams from configurable stream **channel_group** filters onto
numbered placeholder channels (same pattern as reserve_nufu_live_block.py).

**Live games** (default):
  Stream group **Live-Games** → placeholders **Nufu Live Games 01** … **N**.

**Live channels** (optional second block):
  Stream group **Live-Channels** → placeholders **Nufu Live Channels 01** … **N**
  (run `sync_streams_after_m3u.py --map-nufu-live-channels` or env-driven helpers).

  **Plex:** slot→stream mapping is **stable** via `nufu_live_channels_mapping.json`
  (match each slot by `tvg_id` or `name`, not alphabetical order). Generate once:
  `py -3 map_nufu_live_channels.py --write-initial-mapping` then edit the JSON if needed.

**Live games** block still fills by sorted name (short daily game list).

- Live-channel slots without a matching stream in the playlist get **streams cleared**.

EPG / guide:
  **tvg_id** — by default PATCHed from the stream (XMLTV / client matching).
  **Channel name** — optional (default on): set to the **game title** for the day
  (``DISPATCHARR_NUFU_LIVE_SYNC_CHANNEL_NAME``) so Plex and other clients can show
  the event in the guide when programme data is thin. Empty slots restore
  ``Nufu Live Games NN``. Template: ``DISPATCHARR_NUFU_LIVE_CHANNEL_NAME_TEMPLATE``
  (default ``{title}``; supports ``{title}``, ``{slot}``, ``{slot:02d}``).

Env (live games):
  DISPATCHARR_NUFU_M3U_ACCOUNT_ID, DISPATCHARR_NUFU_LIVE_PREFIX,
  DISPATCHARR_NUFU_LIVE_STREAM_GROUP (default Live-Games),
  DISPATCHARR_NUFU_LIVE_MAX_SLOTS (default 50),
  DISPATCHARR_NUFU_LIVE_CHANNEL_START (dial position for slot 1; default 1),
  DISPATCHARR_NUFU_LIVE_SYNC_TVG_ID,
  DISPATCHARR_NUFU_LIVE_SYNC_CHANNEL_NAME (default 1) — PATCH channel name to game title,
  DISPATCHARR_NUFU_LIVE_CHANNEL_NAME_TEMPLATE (default {title})

Env (live channels — separate block):
  DISPATCHARR_NUFU_LIVE_CHANNELS_PREFIX (default Nufu Live Channels),
  DISPATCHARR_NUFU_LIVE_CHANNELS_STREAM_GROUP (default Live-Channels),
  DISPATCHARR_NUFU_LIVE_CHANNELS_MAX_SLOTS (default 100),
  DISPATCHARR_NUFU_LIVE_CHANNELS_CHANNEL_START (default 51 — avoids 1–50 game block),
  DISPATCHARR_NUFU_LIVE_CHANNELS_SYNC_TVG_ID,
  DISPATCHARR_NUFU_LIVE_CHANNELS_MAPPING — path to JSON (see nufu_live_channels_mapping.py),
  DISPATCHARR_NUFU_LIVE_CHANNELS_ORDERED_FALLBACK — if true, use sorted-name fill when JSON missing (unstable)
  DISPATCHARR_NUFU_LIVE_CHANNELS_SYNC_CHANNEL_NAME — PATCH channel name to stream title (default 1; Plex/guide).
  DISPATCHARR_NUFU_LIVE_CHANNELS_CHANNEL_NAME_TEMPLATE — default {title}; same placeholders as live games.
  DISPATCHARR_NUFU_LIVE_CHANNELS_MODE — by_channel_number (default) | placeholders.
    Use by_channel_number when M3U already created channels on each dial (51+): map into those
    rows and avoid duplicate "Nufu Live Channels NN" placeholders. placeholders = old behavior.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from dispatcharr_client import DispatcharrClient, config_from_env, load_dispatcharr_dotenv

from nufu_live_channels_mapping import (
    load_slot_mapping,
    mapping_path_from_env,
    ordered_fallback_enabled,
    resolve_mapped_streams,
)
from nufu_live_slot_maintenance import (
    _list_channels,
    _nufu_account_id,
    _placeholder_slots,
    _prefix,
    ensure_placeholder_slots,
)

LOG = logging.getLogger("map_nufu_live")


def _stream_group_games() -> str:
    return os.environ.get("DISPATCHARR_NUFU_LIVE_STREAM_GROUP", "Live-Games").strip()


def _max_slots_games() -> int:
    return max(1, min(100, int(os.environ.get("DISPATCHARR_NUFU_LIVE_MAX_SLOTS", "50"))))


def _sync_tvg_id_games() -> bool:
    v = os.environ.get("DISPATCHARR_NUFU_LIVE_SYNC_TVG_ID", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _sync_channel_name_games() -> bool:
    v = os.environ.get("DISPATCHARR_NUFU_LIVE_SYNC_CHANNEL_NAME", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _live_game_channel_display_name(stream: dict[str, Any], slot: int, *, prefix: str) -> str:
    """Build channel ``name`` for guide/Plex; placeholders ``{title}`` ``{slot}`` ``{slot:02d}``."""
    tmpl = os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNEL_NAME_TEMPLATE", "{title}").strip()
    title = str(stream.get("name") or "").strip()
    out = (
        tmpl.replace("{slot:02d}", f"{slot:02d}")
        .replace("{slot}", str(slot))
        .replace("{title}", title)
    )
    out = out.strip()
    return out or f"{prefix} {slot:02d}"


def _sync_channel_name_live_channels() -> bool:
    v = os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_SYNC_CHANNEL_NAME", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _live_channels_stream_display_name(stream: dict[str, Any], slot: int, *, prefix: str) -> str:
    """Channel display name for Live-Channels slots (``DISPATCHARR_NUFU_LIVE_CHANNELS_CHANNEL_NAME_TEMPLATE``)."""
    tmpl = os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_CHANNEL_NAME_TEMPLATE", "{title}").strip()
    title = str(stream.get("name") or "").strip()
    out = (
        tmpl.replace("{slot:02d}", f"{slot:02d}")
        .replace("{slot}", str(slot))
        .replace("{title}", title)
    )
    out = out.strip()
    return out or f"{prefix} {slot:02d}"


def _channel_start_games() -> int:
    return int(os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNEL_START", "1"))


def _live_channels_prefix() -> str:
    return os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_PREFIX", "Nufu Live Channels").strip()


def _live_channels_stream_group() -> str:
    return os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_STREAM_GROUP", "Live-Channels").strip()


def _live_channels_max_slots() -> int:
    return max(1, min(100, int(os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_MAX_SLOTS", "100"))))


def _live_channels_channel_start() -> int:
    return int(os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_CHANNEL_START", "51"))


def _live_channels_sync_tvg_id() -> bool:
    v = os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_SYNC_TVG_ID", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _live_channels_mode() -> str:
    """by_channel_number = PATCH channels at dial CHANNEL_START.. using mapping (no extra placeholders)."""
    v = os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_MODE", "by_channel_number").strip().lower()
    if v in ("placeholders", "placeholder", "named"):
        return "placeholders"
    return "by_channel_number"


def _live_channels_stream_group_id(client: DispatcharrClient) -> int:
    nufu_id = _nufu_account_id(client)
    sg = _live_channels_stream_group()
    s = next(
        client.iter_streams(
            page_size=10,
            m3u_account=nufu_id,
            channel_group=sg,
            hide_stale=True,
        ),
        None,
    )
    if not s:
        raise SystemExit(
            f"No non-stale stream in group {sg!r}; cannot resolve channel_group_id. Refresh M3U or check group name.",
        )
    cg = s.get("channel_group")
    if not isinstance(cg, int):
        raise SystemExit("Stream row missing numeric channel_group.")
    return cg


def _placeholder_name_regex(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}\s+\d+\s*$", re.I)


def slots_live_channels_by_dial(
    client: DispatcharrClient,
    *,
    channel_group_id: int,
    channel_start: int,
    max_slots: int,
    placeholder_prefix: str,
) -> dict[int, dict[str, Any]]:
    """slot 1..N → channel at dial channel_start + slot - 1; prefer real names over 'Prefix NN' duplicates."""
    rx_ph = _placeholder_name_regex(placeholder_prefix)
    by_dial: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for ch in client.iter_channels(page_size=500):
        if ch.get("channel_group_id") != channel_group_id:
            continue
        raw = ch.get("channel_number")
        try:
            dial_f = float(raw or 0)
        except (TypeError, ValueError):
            continue
        if dial_f != int(dial_f):
            continue
        dial = int(dial_f)
        if channel_start <= dial < channel_start + max_slots:
            by_dial[dial].append(ch)

    slots: dict[int, dict[str, Any]] = {}
    for slot in range(1, max_slots + 1):
        dial = channel_start + slot - 1
        cands = by_dial.get(dial, [])
        if not cands:
            continue
        non_ph = [c for c in cands if not rx_ph.match(str(c.get("name") or ""))]
        chosen = sorted(non_ph or cands, key=lambda c: int(c.get("id") or 0))[0]
        slots[slot] = chosen
    return slots


def dedupe_live_channel_placeholder_channels(
    client: DispatcharrClient,
    *,
    placeholder_prefix: str,
    channel_group_id: int,
    apply: bool,
) -> int:
    """Remove 'Prefix NN' channels when another channel exists on the same dial + group (M3U duplicates)."""
    rx_ph = _placeholder_name_regex(placeholder_prefix)
    by_dial: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for ch in client.iter_channels(page_size=500):
        if ch.get("channel_group_id") != channel_group_id:
            continue
        try:
            df = float(ch.get("channel_number") or 0)
        except (TypeError, ValueError):
            continue
        if df != int(df):
            continue
        dial = int(df)
        by_dial[dial].append(ch)

    deleted = 0
    for dial, group in sorted(by_dial.items()):
        ph = [c for c in group if rx_ph.match(str(c.get("name") or ""))]
        others = [c for c in group if not rx_ph.match(str(c.get("name") or ""))]
        if not ph or not others:
            continue
        for c in ph:
            cid = int(c["id"])
            LOG.info(
                "Dedupe dial=%s: DELETE placeholder id=%s %r (keeping id=%s %r)",
                dial,
                cid,
                c.get("name"),
                others[0].get("id"),
                others[0].get("name"),
            )
            if apply:
                client.delete_channel(cid)
            deleted += 1
    if deleted and not apply:
        LOG.info("Dry run: would delete %s duplicate placeholder channel(s).", deleted)
    elif deleted:
        LOG.info("Deleted %s duplicate placeholder channel(s).", deleted)
    else:
        LOG.info("No duplicate placeholder channels found for prefix %r.", placeholder_prefix)
    return 0


def _guide_tvg_id_from_stream(stream: dict[str, Any]) -> str:
    """Prefer playlist tvg_id; fall back to stream name."""
    t = stream.get("tvg_id")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return str(stream.get("name") or "").strip()


def _fetch_group_streams(
    client: DispatcharrClient,
    account_id: int,
    group: str,
    *,
    sort_by_name: bool = True,
) -> list[dict[str, Any]]:
    rows = list(
        client.iter_streams(
            page_size=5000,
            m3u_account=account_id,
            channel_group=group,
            hide_stale=True,
        )
    )
    if sort_by_name:
        rows.sort(key=lambda s: str(s.get("name") or "").lower())
    return rows


def _patch_slots_from_streams(
    client: DispatcharrClient,
    *,
    slots: dict[int, dict[str, Any]],
    slot_stream: dict[int, dict[str, Any] | None],
    max_slots: int,
    sync_tvg_id: bool,
    apply: bool,
    block_label: str,
    sync_channel_display_name: bool = False,
    display_name_prefix: str | None = None,
) -> None:
    """Apply PATCH for each slot using pre-resolved stream row or clear."""
    skipped_no_placeholder = 0
    for slot in range(1, max_slots + 1):
        ch = slots.get(slot)
        if not ch:
            skipped_no_placeholder += 1
            continue
        cid = int(ch["id"])
        st = slot_stream.get(slot)
        if st:
            sid = int(st["id"])
            body: dict[str, Any] = {"streams": [sid]}
            if sync_tvg_id:
                gid = _guide_tvg_id_from_stream(st)
                body["tvg_id"] = gid or None
            if sync_channel_display_name and display_name_prefix is not None:
                if block_label == "live-channels":
                    body["name"] = _live_channels_stream_display_name(st, slot, prefix=display_name_prefix)
                else:
                    body["name"] = _live_game_channel_display_name(st, slot, prefix=display_name_prefix)
            if sync_tvg_id:
                LOG.info(
                    "[%s] Slot %02d channel id=%s <- stream id=%s name=%r tvg_id=%r ch_name=%r",
                    block_label,
                    slot,
                    cid,
                    sid,
                    st.get("name"),
                    body.get("tvg_id"),
                    body.get("name"),
                )
            else:
                LOG.info(
                    "[%s] Slot %02d channel id=%s <- stream id=%s %r ch_name=%r",
                    block_label,
                    slot,
                    cid,
                    sid,
                    st.get("name"),
                    body.get("name"),
                )
        else:
            body = {"streams": []}
            if sync_tvg_id:
                body["tvg_id"] = None
            if sync_channel_display_name and display_name_prefix is not None:
                body["name"] = f"{display_name_prefix} {slot:02d}"
            LOG.info(
                "[%s] Slot %02d channel id=%s <- clear (no match) ch_name=%r",
                block_label,
                slot,
                cid,
                body.get("name"),
            )
        if apply:
            client.patch_channel(cid, body)

    if skipped_no_placeholder:
        LOG.warning(
            "[%s] Skipped %s slots (no placeholder channel). Run ensure or lower MAX_SLOTS.",
            block_label,
            skipped_no_placeholder,
        )


def run_map_nufu_slot_block(
    client: DispatcharrClient,
    *,
    prefix: str,
    stream_group: str,
    max_slots: int,
    ensure_channel_start: int,
    apply: bool,
    refresh_nufu_first: bool,
    wait_after_refresh: int | None,
    ensure_placeholders: bool,
    sync_tvg_id: bool,
    block_label: str,
    sync_channel_display_name: bool = False,
) -> int:
    """Map streams in ``stream_group`` to '{prefix} 01' … '{prefix} NN}' placeholders."""
    nufu_id = _nufu_account_id(client)

    if refresh_nufu_first:
        wait = wait_after_refresh
        if wait is None:
            wait = int(os.environ.get("DISPATCHARR_POST_REFRESH_WAIT", "60"))
        LOG.info(
            "[%s] Refreshing Nufu M3U account %s then waiting %s s",
            block_label,
            nufu_id,
            wait,
        )
        if apply:
            client.refresh_m3u_account(nufu_id)
            time.sleep(max(0, wait))
        else:
            LOG.info("Dry run: would refresh + wait")

    if ensure_placeholders:
        rc = ensure_placeholder_slots(
            client,
            prefix=prefix,
            max_slots=max_slots,
            channel_start=ensure_channel_start,
            apply=apply,
            stream_group_for_create=stream_group,
        )
        if rc != 0:
            return rc

    channels = _list_channels(client)
    slots = _placeholder_slots(channels, prefix, max_slot=max_slots)
    if len(slots) < max_slots:
        LOG.warning(
            "[%s] Only %s/%s placeholder channels exist for prefix %r. Run ensure with matching env.",
            block_label,
            len(slots),
            max_slots,
            prefix,
        )

    streams = _fetch_group_streams(client, nufu_id, stream_group, sort_by_name=True)
    LOG.info(
        "[%s] Nufu account=%s stream_group=%r: %s non-stale streams; ordered fill slots 1..%s (cap %s)",
        block_label,
        nufu_id,
        stream_group,
        len(streams),
        min(len(streams), max_slots),
        max_slots,
    )
    LOG.info("[%s] PATCH channel tvg_id from stream: %s", block_label, sync_tvg_id)
    LOG.info("[%s] PATCH channel name from stream (guide/Plex): %s", block_label, sync_channel_display_name)

    slot_stream: dict[int, dict[str, Any] | None] = {}
    for slot in range(1, max_slots + 1):
        if slot <= len(streams):
            slot_stream[slot] = streams[slot - 1]
        else:
            slot_stream[slot] = None

    _patch_slots_from_streams(
        client,
        slots=slots,
        slot_stream=slot_stream,
        max_slots=max_slots,
        sync_tvg_id=sync_tvg_id,
        apply=apply,
        block_label=block_label,
        sync_channel_display_name=sync_channel_display_name,
        display_name_prefix=prefix if sync_channel_display_name else None,
    )

    if not apply:
        LOG.info("[%s] Dry run: no PATCH.", block_label)
    return 0


def write_live_channels_mapping_file(client: DispatcharrClient) -> Path:
    """Snapshot mapping aligned with **dial order** (slot N ↔ dial channel_start+N-1), matching ``by_channel_number`` mode."""
    path = mapping_path_from_env()
    stream_group = _live_channels_stream_group()
    max_slots = _live_channels_max_slots()
    channel_start = _live_channels_channel_start()
    prefix = _live_channels_prefix()
    nufu_id = _nufu_account_id(client)
    cg_id = _live_channels_stream_group_id(client)

    streams_idx: dict[int, dict[str, Any]] = {}
    for s in client.iter_streams(
        page_size=5000,
        m3u_account=nufu_id,
        channel_group=stream_group,
        hide_stale=True,
    ):
        sid = s.get("id")
        if isinstance(sid, int):
            streams_idx[sid] = s

    slots_ch = slots_live_channels_by_dial(
        client,
        channel_group_id=cg_id,
        channel_start=channel_start,
        max_slots=max_slots,
        placeholder_prefix=prefix,
    )

    slots_json: dict[str, dict[str, Any]] = {}
    for slot in range(1, max_slots + 1):
        ch = slots_ch.get(slot)
        if not ch:
            continue
        st_row: dict[str, Any] | None = None
        for sid in ch.get("streams") or []:
            if isinstance(sid, int) and sid in streams_idx:
                st_row = streams_idx[sid]
                break
        if st_row:
            tvg = st_row.get("tvg_id")
            if isinstance(tvg, str) and tvg.strip():
                slots_json[str(slot)] = {"tvg_id": tvg.strip()}
            else:
                slots_json[str(slot)] = {"name": str(st_row.get("name") or "").strip()}
        else:
            nm = str(ch.get("name") or "").strip()
            if nm:
                slots_json[str(slot)] = {"name": nm}

    payload = {
        "version": 1,
        "stream_group": stream_group,
        "channel_start": channel_start,
        "mapping_kind": "by_dial_order",
        "comment": (
            "Slot N is the channel at dial channel_start+N-1 in this group. "
            "Matches after refresh use tvg_id, then name."
        ),
        "slots": slots_json,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    last_dial = channel_start + len(slots_json) - 1 if slots_json else channel_start
    LOG.info(
        "Wrote %s — %s slots (dials %s..%s; aligns with DISPATCHARR_NUFU_LIVE_CHANNELS_MODE=by_channel_number).",
        path.resolve(),
        len(slots_json),
        channel_start,
        last_dial,
    )
    return path


def run_map_nufu_live_games(
    client: DispatcharrClient,
    *,
    apply: bool,
    refresh_nufu_first: bool,
    wait_after_refresh: int | None,
    ensure_placeholders: bool,
    sync_tvg_id: bool | None = None,
    sync_channel_display_name: bool | None = None,
) -> int:
    if sync_tvg_id is None:
        sync_tvg_id = _sync_tvg_id_games()
    if sync_channel_display_name is None:
        sync_channel_display_name = _sync_channel_name_games()
    return run_map_nufu_slot_block(
        client,
        prefix=_prefix(),
        stream_group=_stream_group_games(),
        max_slots=_max_slots_games(),
        ensure_channel_start=_channel_start_games(),
        apply=apply,
        refresh_nufu_first=refresh_nufu_first,
        wait_after_refresh=wait_after_refresh,
        ensure_placeholders=ensure_placeholders,
        sync_tvg_id=sync_tvg_id,
        block_label="live-games",
        sync_channel_display_name=sync_channel_display_name,
    )


def run_map_nufu_live_channels(
    client: DispatcharrClient,
    *,
    apply: bool,
    refresh_nufu_first: bool,
    wait_after_refresh: int | None,
    ensure_placeholders: bool,
    sync_tvg_id: bool | None = None,
    sync_channel_display_name: bool | None = None,
) -> int:
    if sync_tvg_id is None:
        sync_tvg_id = _live_channels_sync_tvg_id()
    if sync_channel_display_name is None:
        sync_channel_display_name = _sync_channel_name_live_channels()

    prefix = _live_channels_prefix()
    stream_group = _live_channels_stream_group()
    max_slots = _live_channels_max_slots()
    ensure_ch_start = _live_channels_channel_start()
    path = mapping_path_from_env()
    entries = load_slot_mapping(path)

    if not entries:
        if ordered_fallback_enabled():
            LOG.warning(
                "live-channels: mapping file missing or empty (%s); "
                "DISPATCHARR_NUFU_LIVE_CHANNELS_ORDERED_FALLBACK=1 — alphabetical fill is NOT stable for Plex",
                path,
            )
            return run_map_nufu_slot_block(
                client,
                prefix=prefix,
                stream_group=stream_group,
                max_slots=max_slots,
                ensure_channel_start=ensure_ch_start,
                apply=apply,
                refresh_nufu_first=refresh_nufu_first,
                wait_after_refresh=wait_after_refresh,
                ensure_placeholders=ensure_placeholders,
                sync_tvg_id=sync_tvg_id,
                block_label="live-channels",
                sync_channel_display_name=sync_channel_display_name,
            )
        LOG.error(
            "live-channels: no slot mapping at %s. For stable Plex order, run:\n"
            "  py -3 map_nufu_live_channels.py --write-initial-mapping\n"
            "Edit the JSON if needed, then re-run sync. "
            "Or set DISPATCHARR_NUFU_LIVE_CHANNELS_ORDERED_FALLBACK=1 (not recommended).",
            path.resolve(),
        )
        return 2

    nufu_id = _nufu_account_id(client)

    if refresh_nufu_first:
        wait = wait_after_refresh
        if wait is None:
            wait = int(os.environ.get("DISPATCHARR_POST_REFRESH_WAIT", "60"))
        LOG.info(
            "[live-channels] Refreshing Nufu M3U account %s then waiting %s s",
            nufu_id,
            wait,
        )
        if apply:
            client.refresh_m3u_account(nufu_id)
            time.sleep(max(0, wait))
        else:
            LOG.info("Dry run: would refresh + wait")

    mode = _live_channels_mode()
    cg_id: int | None = None
    if mode == "by_channel_number":
        cg_id = _live_channels_stream_group_id(client)
        LOG.info(
            "[live-channels] mode=by_channel_number group_id=%s dials %s..%s (M3U channels, not name placeholders)",
            cg_id,
            ensure_ch_start,
            ensure_ch_start + max_slots - 1,
        )
        if ensure_placeholders:
            LOG.info(
                "[live-channels] Skipping placeholder CREATE (not used in by_channel_number mode).",
            )
    elif ensure_placeholders:
        rc = ensure_placeholder_slots(
            client,
            prefix=prefix,
            max_slots=max_slots,
            channel_start=ensure_ch_start,
            apply=apply,
            stream_group_for_create=_live_channels_stream_group(),
        )
        if rc != 0:
            return rc

    if mode == "by_channel_number":
        assert cg_id is not None
        slots = slots_live_channels_by_dial(
            client,
            channel_group_id=cg_id,
            channel_start=ensure_ch_start,
            max_slots=max_slots,
            placeholder_prefix=prefix,
        )
        if len(slots) < max_slots:
            LOG.warning(
                "[live-channels] Only %s/%s channels with dial in [%s..%s] in group %s.",
                len(slots),
                max_slots,
                ensure_ch_start,
                ensure_ch_start + max_slots - 1,
                cg_id,
            )
    else:
        channels = _list_channels(client)
        slots = _placeholder_slots(channels, prefix, max_slot=max_slots)
        if len(slots) < max_slots:
            LOG.warning(
                "[live-channels] Only %s/%s placeholders for prefix %r.",
                len(slots),
                max_slots,
                prefix,
            )

    streams = _fetch_group_streams(client, nufu_id, stream_group, sort_by_name=False)
    LOG.info(
        "[live-channels] mapping=%s entries=%s streams_in_group=%s (match by tvg_id/name)",
        path.resolve(),
        len(entries),
        len(streams),
    )
    LOG.info("[live-channels] PATCH channel tvg_id from stream: %s", sync_tvg_id)
    LOG.info("[live-channels] PATCH channel name from stream (guide/Plex): %s", sync_channel_display_name)

    resolved = resolve_mapped_streams(entries, streams, max_slot=max_slots)

    unmatched = [s for s in range(1, max_slots + 1) if entries.get(s) and resolved.get(s) is None]
    if unmatched:
        LOG.warning(
            "[live-channels] No stream matched mapping for %s slot(s) (check tvg_id/name after playlist change): %s",
            len(unmatched),
            unmatched[:20] + (["..."] if len(unmatched) > 20 else []),
        )

    _patch_slots_from_streams(
        client,
        slots=slots,
        slot_stream=resolved,
        max_slots=max_slots,
        sync_tvg_id=sync_tvg_id,
        apply=apply,
        block_label="live-channels",
        sync_channel_display_name=sync_channel_display_name,
        display_name_prefix=prefix if sync_channel_display_name else None,
    )

    if not apply:
        LOG.info("[live-channels] Dry run: no PATCH.")
    return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dispatcharr_dotenv()

    ap = argparse.ArgumentParser(description="Map Nufu Live-Games streams to numbered placeholders")
    ap.add_argument("--apply", action="store_true", help="PATCH channels (default is dry run)")
    ap.add_argument(
        "--refresh-nufu-first",
        action="store_true",
        help="POST M3U refresh for Nufu only before reading streams",
    )
    ap.add_argument(
        "--wait-after-refresh",
        type=int,
        default=None,
        help="Seconds to wait after Nufu refresh (default DISPATCHARR_POST_REFRESH_WAIT)",
    )
    ap.add_argument(
        "--no-ensure",
        action="store_true",
        help="Do not create missing placeholder channels first",
    )
    ap.add_argument(
        "--no-sync-tvg-id",
        action="store_true",
        help="Do not PATCH channel tvg_id (only assign/clear streams)",
    )
    ap.add_argument(
        "--no-sync-channel-name",
        action="store_true",
        help="Do not PATCH channel name to game title (keep Nufu Live Games NN placeholders)",
    )
    args = ap.parse_args(argv)

    client = DispatcharrClient(config_from_env())
    return run_map_nufu_live_games(
        client,
        apply=args.apply,
        refresh_nufu_first=args.refresh_nufu_first,
        wait_after_refresh=args.wait_after_refresh,
        ensure_placeholders=not args.no_ensure,
        sync_tvg_id=False if args.no_sync_tvg_id else None,
        sync_channel_display_name=False if args.no_sync_channel_name else None,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
