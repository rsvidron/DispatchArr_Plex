#!/usr/bin/env python3
"""
Reserve channel numbers 1–50 for Nufu live-game slots, and move every *existing*
channel to start at 51 (preserving previous channel_number order).

- First run (--apply): bump all current channel_number values to a temporary
  range (avoids unique-number collisions), create 50 placeholder channels
  "Nufu Live Games 01" … "50" on a Nufu stream channel_group, then assign
  your library 51, 52, … in the same order as before.

- Later runs: if the 50 placeholders are already present, does nothing unless
  you pass --repair-mains (relabels non-placeholder channels to 51..N by
  current channel_number sort).

Env:
  DISPATCHARR_NUFU_M3U_ACCOUNT_ID — optional; defaults to account whose name
    contains "nufu" (case-insensitive).
  DISPATCHARR_NUFU_LIVE_PREFIX — placeholder channel name prefix (default:
    Nufu Live Games). Channel names are "{prefix} 01" .. "{prefix} 50".
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import Any, Optional

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

from dispatcharr_client import DispatcharrClient, config_from_env

LOG = logging.getLogger("reserve_nufu")


def _nufu_account_id(client: DispatcharrClient) -> int:
    env = os.environ.get("DISPATCHARR_NUFU_M3U_ACCOUNT_ID")
    if env:
        return int(env)
    for a in client.iter_m3u_accounts():
        name = str(a.get("name") or "")
        if "nufu" in name.lower():
            return int(a["id"])
    raise SystemExit(
        "Could not find a M3U account with 'nufu' in the name. "
        "Set DISPATCHARR_NUFU_M3U_ACCOUNT_ID."
    )


def _channel_group_from_nufu_stream(client: DispatcharrClient, account_id: int) -> int:
    s = next(client.iter_streams(m3u_account=account_id, page_size=1, hide_stale=False))
    cg = s.get("channel_group")
    if not isinstance(cg, int):
        raise SystemExit("Could not read channel_group from a Nufu stream row.")
    return cg


def _prefix() -> str:
    return os.environ.get("DISPATCHARR_NUFU_LIVE_PREFIX", "Nufu Live Games").strip()


def _reserved_regex(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}\s+(\d{{2}})\s*$", re.I)


def _reserved_slots(channels: list[dict[str, Any]], prefix: str) -> dict[int, dict[str, Any]]:
    """slot number 1..50 -> channel dict."""
    rx = _reserved_regex(prefix)
    out: dict[int, dict[str, Any]] = {}
    for ch in channels:
        m = rx.match(str(ch.get("name") or ""))
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= 50:
            out[n] = ch
    return out


def _is_migrated(slots: dict[int, dict[str, Any]]) -> bool:
    if len(slots) != 50:
        return False
    nums = {int(float(ch["channel_number"])) for ch in slots.values()}
    return nums == set(range(1, 51))


def _list_channels(client: DispatcharrClient) -> list[dict[str, Any]]:
    return list(client.iter_channels(page_size=200))


def _temp_bias(snapshot_nums: list[float]) -> int:
    hi = max(snapshot_nums) if snapshot_nums else 0.0
    return int(max(1_000_000.0, hi + 50_000.0))


def run_init(client: DispatcharrClient, *, apply: bool, prefix: str) -> None:
    channels = _list_channels(client)
    rx_ids_reserved = {ch["id"] for ch in _reserved_slots(channels, prefix).values()}
    if rx_ids_reserved:
        raise SystemExit(
            "Some channels already match the Nufu live placeholder name pattern. "
            "Rename or remove them, or pick a different DISPATCHARR_NUFU_LIVE_PREFIX."
        )

    snapshot: dict[int, tuple[float, str]] = {}
    nums: list[float] = []
    for ch in channels:
        cid = int(ch["id"])
        num = float(ch.get("channel_number") or 0)
        snapshot[cid] = (num, str(ch.get("name") or ""))
        nums.append(num)

    bias = _temp_bias(nums)
    LOG.info(
        "Init: %s channels; temp bias +%s; reserve 1-50 as %r 01..50; mains -> %s..",
        len(snapshot),
        bias,
        prefix,
        51,
    )
    if not apply:
        LOG.info("Dry run: no PATCH/POST.")
        return

    for cid in snapshot:
        client.patch_channel(cid, {"channel_number": float(bias + cid)})

    nufu_id = _nufu_account_id(client)
    cg = _channel_group_from_nufu_stream(client, nufu_id)
    LOG.info("Using Nufu M3U account id=%s channel_group_id=%s", nufu_id, cg)

    for i in range(1, 51):
        name = f"{prefix} {i:02d}"
        client.create_channel(name=name, channel_number=float(i), channel_group_id=cg)
        LOG.info("Created placeholder %s ch#%s", name, i)

    mains_sorted = sorted(snapshot.keys(), key=lambda cid: (snapshot[cid][0], cid))
    for idx, cid in enumerate(mains_sorted):
        new_num = float(51 + idx)
        client.patch_channel(cid, {"channel_number": new_num})
        if idx < 3 or idx == len(mains_sorted) - 1:
            LOG.info(
                "Renumber main id=%s %r -> %s",
                cid,
                snapshot[cid][1],
                int(new_num),
            )
    if len(mains_sorted) > 4:
        LOG.info("... %s more mains renumbered to 52..%s", len(mains_sorted) - 4, 50 + len(mains_sorted))


def run_repair_mains(client: DispatcharrClient, *, apply: bool, prefix: str) -> None:
    channels = _list_channels(client)
    slots = _reserved_slots(channels, prefix)
    if not _is_migrated(slots):
        raise SystemExit(
            "Reserve block not detected (need 50 channels named 'Prefix 01'..'50' "
            "with channel_number 1..50). Run without --repair-mains-only first."
        )

    reserved_ids = {int(ch["id"]) for ch in slots.values()}
    mains = [ch for ch in channels if int(ch["id"]) not in reserved_ids]
    mains.sort(
        key=lambda ch: (float(ch.get("channel_number") or 0), int(ch["id"])),
    )
    LOG.info("Repair mains: %s channels -> %s..%s", len(mains), 51, 50 + len(mains))
    if not apply:
        LOG.info("Dry run: no PATCH.")
        return

    for idx, ch in enumerate(mains):
        new_num = float(51 + idx)
        cur = float(ch.get("channel_number") or 0)
        if cur != new_num:
            client.patch_channel(int(ch["id"]), {"channel_number": new_num})


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if load_dotenv:
        load_dotenv()

    ap = argparse.ArgumentParser(description="Reserve ch 1–50 for Nufu live; move library to 51+")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Perform PATCH/POST (default is a dry plan only)",
    )
    ap.add_argument(
        "--repair-mains-only",
        action="store_true",
        help="Only relabel non-placeholder channels to 51..N (after slots exist)",
    )
    args = ap.parse_args(argv)

    prefix = _prefix()
    client = DispatcharrClient(config_from_env())
    channels = _list_channels(client)
    slots = _reserved_slots(channels, prefix)

    if args.repair_mains_only:
        run_repair_mains(client, apply=args.apply, prefix=prefix)
        LOG.info("Done")
        return 0

    if _is_migrated(slots):
        LOG.info(
            "Reserve block already present (%s). Nothing to do. Use --repair-mains-only to relabel mains.",
            prefix,
        )
        return 0

    run_init(client, apply=args.apply, prefix=prefix)
    LOG.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
