#!/usr/bin/env python3
"""
Refresh M3U accounts, then re-point each channel's stream list to the refreshed
stream rows (same M3U account + tvg_id, else same account + normalized name).

Channel objects from GET /api/channels/... include `streams: [stream_id, ...]`.
Each stream carries `m3u_account`, `tvg_id`, `name`, and `url` — after a playlist
refresh, stream *ids* can change; this script rebuilds the ordered id list.

Usage:
  py -3 sync_streams_after_m3u.py
  py -3 sync_streams_after_m3u.py --dry-run
  py -3 sync_streams_after_m3u.py --skip-refresh --export mapping.json
  py -3 sync_streams_after_m3u.py --wait-seconds 90

Env:
  DISPATCHARR_POST_REFRESH_WAIT (default 60) — seconds to wait after refresh
  before rebuilding the stream index (worker may still be importing).

Optional Nufu live (same placeholder names as reserve_nufu_live_block.py):
  --map-nufu-live-games — assign Nufu "Live-Games" streams to Nufu Live Games 01..N (dial block from env, default 500–550).
    Those placeholders are excluded from the generic remap above so only this step sets them (daily churn).
  --map-nufu-live-channels — assign Live-Channels using nufu_live_channels_mapping.json (stable Plex order)
  --map-nufu-no-ensure — skip creating missing game placeholders before mapping
  --map-nufu-live-channels-no-ensure — skip creating missing live-channel placeholders
  --map-nufu-no-sync-tvg-id — do not PATCH channel tvg_id for live games
  --map-nufu-no-sync-channel-name — do not PATCH channel name to game title (guide/Plex label)
  --map-nufu-live-channels-no-sync-tvg-id — do not PATCH tvg_id for live channels
  --map-nufu-live-channels-no-sync-channel-name — do not PATCH live-channel display name to stream title
  --prune-unused-nufu-live-slots — remove placeholder channels with no active stream
  --prune-nufu-allow-all-inactive — required when every live-game slot would be removed
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dispatcharr_client import DispatcharrClient, config_from_env, load_dispatcharr_dotenv

LOG = logging.getLogger("sync_streams")


def _is_nufu_live_game_placeholder_name(name: Any, *, prefix: str, max_slot: int) -> bool:
    rx = re.compile(rf"^{re.escape(prefix)}\s+(\d+)\s*$", re.I)
    m = rx.match(str(name or ""))
    if not m:
        return False
    n = int(m.group(1))
    return 1 <= n <= max_slot


def strip_nufu_live_game_plans_from_generic_remap(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reserve generic remap for library channels; Live Games 01..N are handled only by map_nufu_live_games."""
    prefix = os.environ.get("DISPATCHARR_NUFU_LIVE_PREFIX", "Nufu Live Games").strip()
    max_slot = max(1, min(100, int(os.environ.get("DISPATCHARR_NUFU_LIVE_MAX_SLOTS", "51"))))
    out: list[dict[str, Any]] = []
    skipped = 0
    for p in plans:
        if _is_nufu_live_game_placeholder_name(p.get("channel_name"), prefix=prefix, max_slot=max_slot):
            skipped += 1
            continue
        out.append(p)
    if skipped:
        LOG.info(
            "Excluded %s Nufu Live Games placeholder(s) from generic remap "
            "(slots 1-%s filled next by --map-nufu-live-games only)",
            skipped,
            max_slot,
        )
    return out


def strip_nufu_live_channel_plans_from_generic_remap(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """When using --map-nufu-live-channels, only that step should assign streams to Nufu Live Channels NN."""
    prefix = os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_PREFIX", "Nufu Live Channels").strip()
    max_slot = max(1, min(100, int(os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNELS_MAX_SLOTS", "100"))))
    rx = re.compile(rf"^{re.escape(prefix)}\s+(\d+)\s*$", re.I)
    out: list[dict[str, Any]] = []
    skipped = 0
    for p in plans:
        m = rx.match(str(p.get("channel_name") or ""))
        if m and 1 <= int(m.group(1)) <= max_slot:
            skipped += 1
            continue
        out.append(p)
    if skipped:
        LOG.info(
            "Excluded %s Nufu Live Channels placeholder(s) from generic remap "
            "(filled by --map-nufu-live-channels only)",
            skipped,
        )
    return out


def norm_name(s: Any) -> str:
    t = str(s or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


def m3u_account_id(stream: dict[str, Any]) -> Optional[int]:
    v = stream.get("m3u_account")
    if isinstance(v, int):
        return v
    if isinstance(v, dict):
        x = v.get("id")
        return int(x) if isinstance(x, int) else None
    return None


def load_streams_by_id(
    client: DispatcharrClient,
    *,
    hide_stale: bool,
    page_size: int = 5000,
) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for s in client.iter_streams(
        page_size=page_size,
        m3u_account=None,
        hide_stale=hide_stale,
    ):
        sid = s.get("id")
        if isinstance(sid, int):
            out[sid] = s
    return out


def build_match_indexes(streams: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Per M3U account: tvg_id -> streams, norm_name -> streams."""
    by_acc: dict[int, dict[str, Any]] = {}
    for s in streams:
        aid = m3u_account_id(s)
        if aid is None:
            continue
        bucket = by_acc.setdefault(
            aid,
            {"by_tvg": defaultdict(list), "by_name": defaultdict(list)},
        )
        tvg = str(s.get("tvg_id") or "").strip().lower()
        if tvg:
            bucket["by_tvg"][tvg].append(s)
        bucket["by_name"][norm_name(s.get("name"))].append(s)
    return by_acc


def pick_best_match(candidates: list[dict[str, Any]], old: dict[str, Any]) -> dict[str, Any]:
    """Prefer non-stale, then exact name match to old row, then lowest id."""

    def sort_key(c: dict[str, Any]) -> tuple:
        stale = bool(c.get("is_stale"))
        name_ok = norm_name(c.get("name")) == norm_name(old.get("name"))
        cid = int(c.get("id") or 0)
        return (stale, not name_ok, cid)

    return sorted(candidates, key=sort_key)[0]


def resolve_stream(
    old: dict[str, Any],
    indexes: dict[int, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    aid = m3u_account_id(old)
    if aid is None:
        return None
    bucket = indexes.get(aid)
    if not bucket:
        return None

    tvg = str(old.get("tvg_id") or "").strip().lower()
    if tvg:
        cands = bucket["by_tvg"].get(tvg)
        if cands:
            return pick_best_match(cands, old)

    cands = bucket["by_name"].get(norm_name(old.get("name")))
    if cands:
        return pick_best_match(cands, old)
    return None


def stream_snapshot_row(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "stream_id": s.get("id"),
        "m3u_account": m3u_account_id(s),
        "tvg_id": s.get("tvg_id"),
        "name": s.get("name"),
        "url": s.get("url"),
        "is_stale": s.get("is_stale"),
    }


def collect_accounts(client: DispatcharrClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for a in client.iter_m3u_accounts():
        rows.append({"id": a.get("id"), "name": a.get("name")})
    return rows


def build_channel_plan(
    client: DispatcharrClient,
    stream_by_id: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for ch in client.iter_channels():
        raw_ids = ch.get("streams") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            continue
        slots: list[dict[str, Any]] = []
        for sid in raw_ids:
            if not isinstance(sid, int):
                continue
            s = stream_by_id.get(sid)
            if not s:
                LOG.warning(
                    "Channel %s %r: stream id %s not in current stream index",
                    ch.get("id"),
                    ch.get("name"),
                    sid,
                )
                continue
            slots.append(dict(s))
        if not slots:
            continue
        plans.append(
            {
                "channel_id": ch.get("id"),
                "channel_name": ch.get("name"),
                "channel_number": ch.get("channel_number"),
                "streams_before": [stream_snapshot_row(dict(x)) for x in slots],
                "old_stream_ids": [int(x["id"]) for x in slots if isinstance(x.get("id"), int)],
                "_slots_full": slots,
            }
        )
    return plans


def remap_plans(
    plans: list[dict[str, Any]],
    indexes: dict[int, dict[str, Any]],
) -> None:
    for p in plans:
        new_ids: list[int] = []
        details: list[dict[str, Any]] = []
        for old in p["_slots_full"]:
            hit = resolve_stream(old, indexes)
            if hit and isinstance(hit.get("id"), int):
                new_ids.append(int(hit["id"]))
                details.append(
                    {
                        "from": stream_snapshot_row(old),
                        "to": stream_snapshot_row(hit),
                    }
                )
            else:
                oid = old.get("id")
                LOG.warning(
                    "No match for channel %s %r slot stream id=%s account=%s tvg_id=%r name=%r — keeping old id",
                    p["channel_id"],
                    p["channel_name"],
                    oid,
                    m3u_account_id(old),
                    old.get("tvg_id"),
                    old.get("name"),
                )
                if isinstance(oid, int):
                    new_ids.append(int(oid))
                details.append({"from": stream_snapshot_row(old), "to": None})

        p["streams_resolution"] = details
        p["new_stream_ids"] = new_ids
        p["changed"] = new_ids != p.get("old_stream_ids", [])


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dispatcharr_dotenv()

    ap = argparse.ArgumentParser(
        description="Refresh M3U then remap channel stream IDs to refreshed rows",
    )
    ap.add_argument("--dry-run", action="store_true", help="Do not PATCH channels")
    ap.add_argument("--skip-refresh", action="store_true", help="Only remap (no M3U POST)")
    ap.add_argument(
        "--m3u-account",
        type=int,
        default=None,
        help="Refresh only this M3U account id (default: all accounts)",
    )
    ap.add_argument(
        "--wait-seconds",
        type=int,
        default=None,
        help="Override DISPATCHARR_POST_REFRESH_WAIT",
    )
    ap.add_argument(
        "--export",
        type=Path,
        default=None,
        help="Write mapping report JSON (default: channel_stream_mapping.json)",
    )
    ap.add_argument(
        "--no-export",
        action="store_true",
        help="Do not write channel_stream_mapping.json",
    )
    ap.add_argument(
        "--index-hide-stale",
        choices=("true", "false"),
        default="false",
        help="When building post-refresh index, hide stale streams (default false = wider match)",
    )
    ap.add_argument(
        "--map-nufu-live-games",
        action="store_true",
        help="After remap, map Nufu Live-Games streams onto Nufu Live Games 01..N",
    )
    ap.add_argument(
        "--map-nufu-live-channels",
        action="store_true",
        help="After remap, map Nufu Live-Channels streams onto Nufu Live Channels 01..N",
    )
    ap.add_argument(
        "--map-nufu-no-ensure",
        action="store_true",
        help="With --map-nufu-live-games, do not POST missing game placeholders first",
    )
    ap.add_argument(
        "--map-nufu-live-channels-no-ensure",
        action="store_true",
        help="With --map-nufu-live-channels, do not POST missing live-channel placeholders first",
    )
    ap.add_argument(
        "--map-nufu-no-sync-tvg-id",
        action="store_true",
        help="With --map-nufu-live-games, only set streams (do not PATCH channel tvg_id)",
    )
    ap.add_argument(
        "--map-nufu-no-sync-channel-name",
        action="store_true",
        help="With --map-nufu-live-games, keep placeholder channel names (do not PATCH name to game title)",
    )
    ap.add_argument(
        "--map-nufu-live-channels-no-sync-tvg-id",
        action="store_true",
        help="With --map-nufu-live-channels, only set streams (do not PATCH channel tvg_id)",
    )
    ap.add_argument(
        "--map-nufu-live-channels-no-sync-channel-name",
        action="store_true",
        help="With --map-nufu-live-channels, keep placeholder names (do not PATCH name to stream title)",
    )
    ap.add_argument(
        "--prune-unused-nufu-live-slots",
        action="store_true",
        help="After remap, delete Nufu Live Games placeholders with no non-stale streams (env slot count)",
    )
    ap.add_argument(
        "--prune-nufu-allow-all-inactive",
        action="store_true",
        help="With --prune-unused-nufu-live-slots, allow deleting entire game block when none have a live stream",
    )
    args = ap.parse_args(argv)

    wait = args.wait_seconds
    if wait is None:
        wait = int(os.environ.get("DISPATCHARR_POST_REFRESH_WAIT", "60"))

    cfg = config_from_env()
    client = DispatcharrClient(cfg)

    hide_for_index = args.index_hide_stale == "true"

    LOG.info("Loading current streams (hide_stale=%s)", hide_for_index)
    stream_by_id = load_streams_by_id(client, hide_stale=hide_for_index)
    LOG.info("Streams in index: %s", len(stream_by_id))

    accounts = collect_accounts(client)
    LOG.info("M3U accounts: %s", accounts)

    plans = build_channel_plan(client, stream_by_id)
    LOG.info("Channels with at least one stream: %s", len(plans))

    if args.map_nufu_live_games:
        plans = strip_nufu_live_game_plans_from_generic_remap(plans)

    if args.map_nufu_live_channels:
        plans = strip_nufu_live_channel_plans_from_generic_remap(plans)

    if not args.skip_refresh:
        if args.m3u_account is not None:
            LOG.info("Refreshing M3U account %s", args.m3u_account)
            out = client.refresh_m3u_account(args.m3u_account)
        else:
            LOG.info("Refreshing all M3U accounts")
            out = client.refresh_all_m3u()
        LOG.info("M3U refresh response: %s", out)
        LOG.info("Waiting %s s for import to finish", wait)
        time.sleep(max(0, wait))

        LOG.info("Reloading streams after refresh (hide_stale=%s)", hide_for_index)
        stream_by_id = load_streams_by_id(client, hide_stale=hide_for_index)
        LOG.info("Streams in index: %s", len(stream_by_id))

    indexes = build_match_indexes(list(stream_by_id.values()))
    remap_plans(plans, indexes)

    changed = [p for p in plans if p.get("changed")]
    LOG.info("Channels needing PATCH: %s", len(changed))

    if args.no_export:
        export_path: Optional[Path] = None
    elif args.export is not None:
        export_path = args.export
    else:
        export_path = Path("channel_stream_mapping.json")

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "m3u_accounts": accounts,
        "wait_seconds_after_refresh": wait,
        "channels": [],
    }

    for p in plans:
        report["channels"].append(
            {
                "channel_id": p["channel_id"],
                "channel_name": p["channel_name"],
                "channel_number": p["channel_number"],
                "streams_before": p["streams_before"],
                "old_stream_ids": p["old_stream_ids"],
                "new_stream_ids": p["new_stream_ids"],
                "changed": p["changed"],
                "streams_resolution": p["streams_resolution"],
            }
        )

    if export_path:
        export_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        LOG.info("Wrote %s", export_path.resolve())

    for p in plans:
        if not p.get("changed"):
            continue
        cid = int(p["channel_id"])
        body = {"streams": p["new_stream_ids"]}
        LOG.info("PATCH channel %s %r streams %s -> %s", cid, p["channel_name"], p["old_stream_ids"], p["new_stream_ids"])
        if args.dry_run:
            continue
        client.patch_channel(cid, body)

    if args.map_nufu_live_games:
        from map_nufu_live_games import run_map_nufu_live_games

        rc = run_map_nufu_live_games(
            client,
            apply=not args.dry_run,
            refresh_nufu_first=False,
            wait_after_refresh=wait,
            ensure_placeholders=not args.map_nufu_no_ensure,
            sync_tvg_id=False if args.map_nufu_no_sync_tvg_id else None,
            sync_channel_display_name=False if args.map_nufu_no_sync_channel_name else None,
        )
        if rc != 0:
            return rc

    if args.map_nufu_live_channels:
        from map_nufu_live_games import run_map_nufu_live_channels

        rc = run_map_nufu_live_channels(
            client,
            apply=not args.dry_run,
            refresh_nufu_first=False,
            wait_after_refresh=wait,
            ensure_placeholders=not args.map_nufu_live_channels_no_ensure,
            sync_tvg_id=False if args.map_nufu_live_channels_no_sync_tvg_id else None,
            sync_channel_display_name=False if args.map_nufu_live_channels_no_sync_channel_name else None,
        )
        if rc != 0:
            return rc

    if args.prune_unused_nufu_live_slots:
        from nufu_live_slot_maintenance import prune_unused_nufu_live_slots

        rc = prune_unused_nufu_live_slots(
            client,
            apply=not args.dry_run,
            allow_all_inactive=args.prune_nufu_allow_all_inactive,
        )
        if rc != 0:
            return rc

    LOG.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
