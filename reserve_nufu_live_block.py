#!/usr/bin/env python3
"""
Reserve a contiguous dial block for Nufu live-game placeholders and move every *other*
channel to start at DISPATCHARR_NUFU_MAIN_LIBRARY_START (default **26**), preserving order.

Default layout (env defaults): live games **500–550** (51 × ``Nufu Live Games NN``);
other automated channels **26, 27, …** (dials **1–25** left for manual channels).

- First run (--apply): bump all current channel_number values to a temporary range,
  create placeholder channels at ``CHANNEL_START`` … ``CHANNEL_START + MAX_SLOTS - 1``,
  then assign former channels to ``MAIN_LIBRARY_START``, ``MAIN_LIBRARY_START+1``, …

- Later runs: if placeholders already match that layout, does nothing unless
  ``--repair-mains-only`` (relabels non-placeholder channels to sequential dials from ``MAIN_LIBRARY_START``).

Env:
  DISPATCHARR_NUFU_M3U_ACCOUNT_ID — optional; defaults to account whose name
    contains "nufu" (case-insensitive).
  DISPATCHARR_NUFU_LIVE_STREAM_GROUP — playlist group for channel_group_id (default Live-Games).
  DISPATCHARR_NUFU_LIVE_PREFIX — placeholder name prefix (default Nufu Live Games).
    Names are "{prefix} 01" … "{prefix} NN" (NN zero-padded to width 2).
  DISPATCHARR_NUFU_LIVE_CHANNEL_START — first dial for slot 1 (default 500).
  DISPATCHARR_NUFU_LIVE_MAX_SLOTS — number of game slots (default 51 → dials 500–550).
  DISPATCHARR_NUFU_MAIN_LIBRARY_START — first dial for non-game channels after init (default 26; dials 1–25 left for manual).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import Any

from dispatcharr_client import (
    DispatcharrClient,
    channel_group_id_from_stream_group,
    config_from_env,
    load_dispatcharr_dotenv,
)

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


def _prefix() -> str:
    return os.environ.get("DISPATCHARR_NUFU_LIVE_PREFIX", "Nufu Live Games").strip()


def _max_slots() -> int:
    return max(1, min(100, int(os.environ.get("DISPATCHARR_NUFU_LIVE_MAX_SLOTS", "51"))))


def _channel_start() -> int:
    return int(os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNEL_START", "500"))


def _main_library_start() -> int:
    return int(os.environ.get("DISPATCHARR_NUFU_MAIN_LIBRARY_START", "26"))


def _reserved_regex(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}\s+(\d{{1,3}})\s*$", re.I)


def _reserved_slots(channels: list[dict[str, Any]], prefix: str, *, max_slot: int) -> dict[int, dict[str, Any]]:
    """slot number 1..max_slot -> channel dict (by name)."""
    rx = _reserved_regex(prefix)
    out: dict[int, dict[str, Any]] = {}
    for ch in channels:
        m = rx.match(str(ch.get("name") or ""))
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= max_slot:
            out[n] = ch
    return out


def _is_migrated(
    slots: dict[int, dict[str, Any]],
    *,
    channel_start: int,
    max_slot: int,
) -> bool:
    if len(slots) != max_slot:
        return False
    nums = {int(float(ch["channel_number"])) for ch in slots.values()}
    return nums == set(range(channel_start, channel_start + max_slot))


def _list_channels(client: DispatcharrClient) -> list[dict[str, Any]]:
    return list(client.iter_channels(page_size=200))


def _temp_bias(snapshot_nums: list[float]) -> int:
    hi = max(snapshot_nums) if snapshot_nums else 0.0
    return int(max(1_000_000.0, hi + 50_000.0))


def run_init(client: DispatcharrClient, *, apply: bool, prefix: str) -> None:
    max_slot = _max_slots()
    ch_start = _channel_start()
    main0 = _main_library_start()

    channels = _list_channels(client)
    rx_ids_reserved = {ch["id"] for ch in _reserved_slots(channels, prefix, max_slot=max_slot).values()}
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
    last_game_dial = ch_start + max_slot - 1
    LOG.info(
        "Init: %s channels; temp bias +%s; reserve dials %s..%s as %r 01..%02d; mains -> %s..",
        len(snapshot),
        bias,
        ch_start,
        last_game_dial,
        prefix,
        max_slot,
        main0,
    )
    if not apply:
        LOG.info("Dry run: no PATCH/POST.")
        return

    for cid in snapshot:
        client.patch_channel(cid, {"channel_number": float(bias + cid)})

    nufu_id = _nufu_account_id(client)
    sg = os.environ.get("DISPATCHARR_NUFU_LIVE_STREAM_GROUP", "Live-Games").strip()
    try:
        cg = channel_group_id_from_stream_group(
            client,
            m3u_account_id=nufu_id,
            stream_group=sg,
            hide_stale=False,
        )
    except ValueError as e:
        raise SystemExit(str(e)) from e
    LOG.info(
        "Using Nufu M3U account id=%s stream_group=%r channel_group_id=%s",
        nufu_id,
        sg,
        cg,
    )

    for i in range(1, max_slot + 1):
        name = f"{prefix} {i:02d}"
        dial = float(ch_start + i - 1)
        client.create_channel(name=name, channel_number=dial, channel_group_id=cg)
        LOG.info("Created placeholder %s dial=%s", name, int(dial))

    mains_sorted = sorted(snapshot.keys(), key=lambda cid: (snapshot[cid][0], cid))
    for idx, cid in enumerate(mains_sorted):
        new_num = float(main0 + idx)
        client.patch_channel(cid, {"channel_number": new_num})
        if idx < 3 or idx == len(mains_sorted) - 1:
            LOG.info(
                "Renumber main id=%s %r -> %s",
                cid,
                snapshot[cid][1],
                int(new_num),
            )
    if len(mains_sorted) > 4:
        LOG.info(
            "... %s more mains renumbered to %s..%s",
            len(mains_sorted) - 4,
            main0 + 4,
            main0 + len(mains_sorted) - 1,
        )


def run_repair_mains(client: DispatcharrClient, *, apply: bool, prefix: str) -> None:
    max_slot = _max_slots()
    ch_start = _channel_start()
    main0 = _main_library_start()

    channels = _list_channels(client)
    slots = _reserved_slots(channels, prefix, max_slot=max_slot)
    if not _is_migrated(slots, channel_start=ch_start, max_slot=max_slot):
        raise SystemExit(
            f"Reserve block not detected (need {max_slot} channels named 'Prefix 01'..'Prefix {max_slot:02d}' "
            f"with channel_number {ch_start}..{ch_start + max_slot - 1}). "
            "Run without --repair-mains-only first."
        )

    reserved_ids = {int(ch["id"]) for ch in slots.values()}
    mains = [ch for ch in channels if int(ch["id"]) not in reserved_ids]
    mains.sort(
        key=lambda ch: (float(ch.get("channel_number") or 0), int(ch["id"])),
    )
    last_main = main0 + len(mains) - 1
    LOG.info("Repair mains: %s channels -> %s..%s", len(mains), main0, last_main)
    if not apply:
        LOG.info("Dry run: no PATCH.")
        return

    for idx, ch in enumerate(mains):
        new_num = float(main0 + idx)
        cur = float(ch.get("channel_number") or 0)
        if cur != new_num:
            client.patch_channel(int(ch["id"]), {"channel_number": new_num})


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dispatcharr_dotenv()

    ap = argparse.ArgumentParser(
        description="Reserve live-game dial block for Nufu; move other channels to MAIN_LIBRARY_START..",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Perform PATCH/POST (default is a dry plan only)",
    )
    ap.add_argument(
        "--repair-mains-only",
        action="store_true",
        help="Only relabel non-placeholder channels to MAIN_LIBRARY_START..N (after slots exist)",
    )
    args = ap.parse_args(argv)

    prefix = _prefix()
    client = DispatcharrClient(config_from_env())
    channels = _list_channels(client)
    slots = _reserved_slots(channels, prefix, max_slot=_max_slots())

    if args.repair_mains_only:
        run_repair_mains(client, apply=args.apply, prefix=prefix)
        LOG.info("Done")
        return 0

    if _is_migrated(slots, channel_start=_channel_start(), max_slot=_max_slots()):
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
