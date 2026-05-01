#!/usr/bin/env python3
"""
Map Nufu M3U streams in the **Live-Games** (configurable) channel group onto
placeholder channels **Nufu Live Games 01** … **50** (same names as
reserve_nufu_live_block.py).

- Streams are sorted by name (case-insensitive), then assigned in order:
  slot 1 gets the first stream, … up to min(50, number of streams).
- Remaining slots (if fewer than 50 games today) get **streams cleared** so
  stale feeds are not left attached.

EPG / guide (channel **name** can stay "Nufu Live Games 01"):
  Many clients match the XMLTV **tvg-id** to programme listings. Nufu streams
  already carry a **tvg_id** (often the same as the stream/game name). By
  default this script also **PATCHes each slot's channel `tvg_id`** to that
  value when a stream is assigned, and clears `tvg_id` when the slot is empty.
  If you merge an XMLTV (or other source) that publishes programmes for those
  ids, the grid can show the game. Dispatcharr's REST **program** / **epgdata**
  write endpoints are not available on all installs (405/500), so per-day
  **programme titles** usually come from an external EPG keyed by **tvg-id**,
  or from UI features such as **Dummy EPG** / provider guides.

This was *not* part of sync_streams_after_m3u.py before; run this script daily
or pass **--map-nufu-live-games** on that script after refresh/remap.

Env:
  DISPATCHARR_NUFU_M3U_ACCOUNT_ID — optional; defaults to M3U account whose name
    contains "nufu".
  DISPATCHARR_NUFU_LIVE_PREFIX — placeholder channel name prefix (default:
    Nufu Live Games).
  DISPATCHARR_NUFU_LIVE_STREAM_GROUP — stream table group filter (default:
    Live-Games).
  DISPATCHARR_NUFU_LIVE_MAX_SLOTS — default 50.
  DISPATCHARR_NUFU_LIVE_SYNC_TVG_ID — if 0/false/no, do not PATCH channel tvg_id
    (default: set/clear tvg_id with each slot).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

from dispatcharr_client import DispatcharrClient, config_from_env

from nufu_live_slot_maintenance import (
    _list_channels,
    _nufu_account_id,
    _placeholder_slots,
    _prefix,
    cmd_ensure,
)

LOG = logging.getLogger("map_nufu_live")


def _stream_group() -> str:
    return os.environ.get("DISPATCHARR_NUFU_LIVE_STREAM_GROUP", "Live-Games").strip()


def _max_slots() -> int:
    return max(1, min(100, int(os.environ.get("DISPATCHARR_NUFU_LIVE_MAX_SLOTS", "50"))))


def _sync_tvg_id_enabled() -> bool:
    v = os.environ.get("DISPATCHARR_NUFU_LIVE_SYNC_TVG_ID", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _guide_tvg_id_from_stream(stream: dict[str, Any]) -> str:
    """Prefer playlist tvg_id; fall back to stream name (Nufu live-games use both)."""
    t = stream.get("tvg_id")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return str(stream.get("name") or "").strip()


def _fetch_live_streams(client: DispatcharrClient, account_id: int, group: str) -> list[dict[str, Any]]:
    rows = list(
        client.iter_streams(
            page_size=5000,
            m3u_account=account_id,
            channel_group=group,
            hide_stale=True,
        )
    )
    rows.sort(key=lambda s: str(s.get("name") or "").lower())
    return rows


def run_map_nufu_live_games(
    client: DispatcharrClient,
    *,
    apply: bool,
    refresh_nufu_first: bool,
    wait_after_refresh: int | None,
    ensure_placeholders: bool,
    sync_tvg_id: bool | None = None,
) -> int:
    prefix = _prefix()
    group = _stream_group()
    cap = _max_slots()
    nufu_id = _nufu_account_id(client)
    if sync_tvg_id is None:
        sync_tvg_id = _sync_tvg_id_enabled()

    if refresh_nufu_first:
        wait = wait_after_refresh
        if wait is None:
            wait = int(os.environ.get("DISPATCHARR_POST_REFRESH_WAIT", "60"))
        LOG.info("Refreshing Nufu M3U account %s then waiting %s s", nufu_id, wait)
        if apply:
            client.refresh_m3u_account(nufu_id)
            time.sleep(max(0, wait))
        else:
            LOG.info("Dry run: would refresh + wait")

    if ensure_placeholders:
        rc = cmd_ensure(client, apply=apply)
        if rc != 0:
            return rc

    channels = _list_channels(client)
    slots = _placeholder_slots(channels, prefix)
    if len(slots) < cap:
        LOG.warning(
            "Only %s/%s placeholder channels exist for prefix %r. Run: "
            "py -3 nufu_live_slot_maintenance.py ensure --apply",
            len(slots),
            cap,
            prefix,
        )

    streams = _fetch_live_streams(client, nufu_id, group)
    LOG.info(
        "Nufu account=%s group=%r: %s non-stale streams; mapping first %s to slots 1..%s",
        nufu_id,
        group,
        len(streams),
        min(len(streams), cap),
        cap,
    )
    LOG.info("Sync channel tvg_id from stream for guide/XMLTV: %s", sync_tvg_id)

    for slot in range(1, cap + 1):
        ch = slots.get(slot)
        if not ch:
            LOG.warning("No placeholder channel for slot %s; skip", slot)
            continue
        cid = int(ch["id"])
        if slot <= len(streams):
            st = streams[slot - 1]
            sid = int(st["id"])
            body: dict[str, Any] = {"streams": [sid]}
            if sync_tvg_id:
                gid = _guide_tvg_id_from_stream(st)
                body["tvg_id"] = gid or None
                LOG.info(
                    "Slot %02d channel id=%s <- stream id=%s name=%r tvg_id=%r",
                    slot,
                    cid,
                    sid,
                    st.get("name"),
                    body.get("tvg_id"),
                )
            else:
                LOG.info("Slot %02d channel id=%s <- stream id=%s %r", slot, cid, sid, st.get("name"))
        else:
            body = {"streams": []}
            if sync_tvg_id:
                body["tvg_id"] = None
            LOG.info("Slot %02d channel id=%s <- clear streams (no game)", slot, cid)
        if apply:
            client.patch_channel(cid, body)

    if not apply:
        LOG.info("Dry run: no PATCH.")
    return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if load_dotenv:
        load_dotenv()

    ap = argparse.ArgumentParser(description="Map Nufu Live-Games streams to channels 1-50")
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
        help="Do not create missing Nufu Live Games 01..50 placeholders first",
    )
    ap.add_argument(
        "--no-sync-tvg-id",
        action="store_true",
        help="Do not PATCH channel tvg_id (only assign/clear streams)",
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
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
