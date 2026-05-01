#!/usr/bin/env python3
"""
Maintain Nufu live placeholder channels (names like "Nufu Live Games 01" .. "NN").

prune  — DELETE any live-game slot whose streams are empty or only *stale* rows,
         so finished / non-existent games disappear from Dispatcharr entirely.

ensure — Recreate any missing "Nufu Live Games NN" channels (slot index 1..MAX_SLOTS)
         (after prune, or partial deletes). channel_group_id comes from a **Live-Games**
         stream row (DISPATCHARR_NUFU_LIVE_STREAM_GROUP).

fix-game-groups — PATCH existing Live Games placeholders onto that Live-Games group if the UI
         showed them under Live-Channels (older code used the wrong stream row).

Typical schedule:
  - After games / before export:  py -3 nufu_live_slot_maintenance.py prune --apply
  - Before next fill:            py -3 nufu_live_slot_maintenance.py ensure --apply

If *every* slot is inactive (nothing scheduled), pruning would remove all slots;
pass --allow-all-inactive to confirm that intentional.

Env: DISPATCHARR_NUFU_M3U_ACCOUNT_ID, DISPATCHARR_NUFU_LIVE_PREFIX (same as reserve script).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import Any, Optional

from dispatcharr_client import (
    DispatcharrClient,
    channel_group_id_from_stream_group,
    config_from_env,
    load_dispatcharr_dotenv,
)

LOG = logging.getLogger("nufu_live_slots")


def _prefix() -> str:
    return os.environ.get("DISPATCHARR_NUFU_LIVE_PREFIX", "Nufu Live Games").strip()


def _nufu_account_id(client: DispatcharrClient) -> int:
    env = os.environ.get("DISPATCHARR_NUFU_M3U_ACCOUNT_ID")
    if env:
        return int(env)
    for a in client.iter_m3u_accounts():
        name = str(a.get("name") or "")
        if "nufu" in name.lower():
            return int(a["id"])
    raise SystemExit("Set DISPATCHARR_NUFU_M3U_ACCOUNT_ID or add 'nufu' to the M3U account name.")


def _name_regex(prefix: str) -> re.Pattern[str]:
    """Match '{prefix} N' with one or more digits (01, 50, 97, …)."""
    return re.compile(rf"^{re.escape(prefix)}\s+(\d+)\s*$", re.I)


def _list_channels(client: DispatcharrClient) -> list[dict[str, Any]]:
    return list(client.iter_channels(page_size=200))


def _load_streams_by_id(client: DispatcharrClient, *, hide_stale: bool) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for s in client.iter_streams(
        page_size=5000,
        m3u_account=None,
        hide_stale=hide_stale,
    ):
        sid = s.get("id")
        if isinstance(sid, int):
            out[sid] = s
    return out


def _max_slots_default() -> int:
    return max(1, min(100, int(os.environ.get("DISPATCHARR_NUFU_LIVE_MAX_SLOTS", "51"))))


def _placeholder_slots(
    channels: list[dict[str, Any]],
    prefix: str,
    *,
    max_slot: int | None = None,
) -> dict[int, dict[str, Any]]:
    """slot 1..max_slot -> channel dict (matched by name only)."""
    cap = max_slot if max_slot is not None else _max_slots_default()
    rx = _name_regex(prefix)
    out: dict[int, dict[str, Any]] = {}
    for ch in channels:
        m = rx.match(str(ch.get("name") or ""))
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= cap:
            out[n] = ch
    return out


def _has_active_stream(ch: dict[str, Any], stream_by_id: dict[int, dict[str, Any]]) -> bool:
    """True if any assigned stream exists and is not stale."""
    for sid in ch.get("streams") or []:
        if not isinstance(sid, int):
            continue
        s = stream_by_id.get(sid)
        if not s:
            continue
        if s.get("is_stale"):
            continue
        return True
    return False


def cmd_prune(client: DispatcharrClient, *, apply: bool, allow_all_inactive: bool) -> int:
    prefix = _prefix()
    channels = _list_channels(client)
    slots = _placeholder_slots(channels, prefix, max_slot=_max_slots_default())
    if not slots:
        LOG.info("No channels match %r live-game placeholders; nothing to prune.", prefix)
        return 0

    # Prefer index that still includes stale rows so we can detect "only stale" assignments.
    stream_by_id = _load_streams_by_id(client, hide_stale=False)

    inactive: list[tuple[int, dict[str, Any]]] = []
    active = 0
    for slot in sorted(slots.keys()):
        ch = slots[slot]
        if _has_active_stream(ch, stream_by_id):
            active += 1
        else:
            inactive.append((slot, ch))

    LOG.info(
        "Placeholders: %s slots; active (non-stale stream): %s; inactive: %s",
        len(slots),
        active,
        len(inactive),
    )

    if len(inactive) == len(slots) and not allow_all_inactive:
        LOG.error(
            "All %s placeholders are inactive. Refusing to delete without "
            "--allow-all-inactive (prevents wiping the block if streams are still importing).",
            len(slots),
        )
        return 2

    for slot, ch in inactive:
        cid = int(ch["id"])
        LOG.info("Prune slot %02d channel id=%s name=%r", slot, cid, ch.get("name"))
        if apply:
            client.delete_channel(cid)

    if not apply:
        LOG.info("Dry run: no DELETE.")
    return 0


def ensure_placeholder_slots(
    client: DispatcharrClient,
    *,
    prefix: str,
    max_slots: int,
    channel_start: int,
    apply: bool,
    stream_group_for_create: str | None = None,
) -> int:
    """Create missing '{prefix} NN' channels with dial positions channel_start..+.

    ``stream_group_for_create`` selects which playlist group's ``channel_group_id`` to use
    (e.g. ``Live-Games`` for Nufu Live Games rows, ``Live-Channels`` for Nufu Live Channels).
    """
    channels = _list_channels(client)
    existing = _placeholder_slots(channels, prefix, max_slot=max_slots)
    missing = [n for n in range(1, max_slots + 1) if n not in existing]
    if not missing:
        LOG.info("All %s placeholders already exist for prefix %r.", max_slots, prefix)
        return 0

    nufu_id = _nufu_account_id(client)
    sg = (
        stream_group_for_create
        or os.environ.get("DISPATCHARR_NUFU_LIVE_STREAM_GROUP", "Live-Games").strip()
    )
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
        "Creating %s missing placeholders (prefix=%r max=%s dial_start=%s Nufu account=%s "
        "stream_group=%r channel_group_id=%s)",
        len(missing),
        prefix,
        max_slots,
        channel_start,
        nufu_id,
        sg,
        cg,
    )
    if not apply:
        LOG.info("Dry run: no POST.")
        return 0

    for n in missing:
        name = f"{prefix} {n:02d}"
        chno = float(channel_start + n - 1)
        client.create_channel(name=name, channel_number=chno, channel_group_id=cg)
        LOG.info("Created %s dial=%s", name, chno)
    return 0


def cmd_ensure(client: DispatcharrClient, *, apply: bool) -> int:
    prefix = _prefix()
    max_slots = _max_slots_default()
    channel_start = int(os.environ.get("DISPATCHARR_NUFU_LIVE_CHANNEL_START", "500"))
    return ensure_placeholder_slots(
        client,
        prefix=prefix,
        max_slots=max_slots,
        channel_start=channel_start,
        apply=apply,
        stream_group_for_create=os.environ.get(
            "DISPATCHARR_NUFU_LIVE_STREAM_GROUP",
            "Live-Games",
        ).strip(),
    )


def cmd_fix_live_games_group(client: DispatcharrClient, *, apply: bool) -> int:
    """Set channel_group_id on Nufu Live Games NN channels from a Live-Games stream row (fixes wrong UI group)."""
    prefix = _prefix()
    max_slot = _max_slots_default()
    nufu_id = _nufu_account_id(client)
    sg = os.environ.get("DISPATCHARR_NUFU_LIVE_STREAM_GROUP", "Live-Games").strip()
    try:
        target_cg = channel_group_id_from_stream_group(
            client,
            m3u_account_id=nufu_id,
            stream_group=sg,
            hide_stale=False,
        )
    except ValueError as e:
        raise SystemExit(str(e)) from e

    rx = _name_regex(prefix)
    patched = 0
    already = 0
    for ch in client.iter_channels(page_size=500):
        m = rx.match(str(ch.get("name") or ""))
        if not m:
            continue
        n = int(m.group(1))
        if not (1 <= n <= max_slot):
            continue
        cid = int(ch["id"])
        cur = ch.get("channel_group_id")
        if cur == target_cg:
            already += 1
            continue
        LOG.info(
            "PATCH channel id=%s %r channel_group_id %s -> %s (from stream group %r)",
            cid,
            ch.get("name"),
            cur,
            target_cg,
            sg,
        )
        if apply:
            client.patch_channel(cid, {"channel_group_id": target_cg})
        patched += 1
    LOG.info(
        "fix-live-games-group: %s updated, %s already Live-Games group (%s)%s.",
        patched,
        already,
        target_cg,
        "" if apply else " dry-run",
    )
    return 0


def prune_unused_nufu_live_slots(
    client: DispatcharrClient,
    *,
    apply: bool,
    allow_all_inactive: bool = False,
) -> int:
    """Callable from sync_streams_after_m3u (same semantics as CLI prune)."""
    return cmd_prune(client, apply=apply, allow_all_inactive=allow_all_inactive)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dispatcharr_dotenv()

    ap = argparse.ArgumentParser(description="Nufu live game placeholder maintenance")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_prune = sub.add_parser("prune", help="Delete inactive live placeholders (no non-stale stream)")
    p_prune.add_argument("--apply", action="store_true", help="Perform DELETEs")
    p_prune.add_argument(
        "--allow-all-inactive",
        action="store_true",
        help="Allow deleting every placeholder when none have an active stream",
    )

    p_ensure = sub.add_parser("ensure", help="Create missing Nufu Live Games 01..NN channels")
    p_ensure.add_argument("--apply", action="store_true", help="Perform POST creates")

    p_fix_grp = sub.add_parser(
        "fix-game-groups",
        help="PATCH Live Games placeholders to Live-Games channel_group_id (after mistaken Live-Channels)",
    )
    p_fix_grp.add_argument("--apply", action="store_true", help="Perform PATCH")

    args = ap.parse_args(argv)
    client = DispatcharrClient(config_from_env())

    if args.cmd == "prune":
        return cmd_prune(client, apply=args.apply, allow_all_inactive=args.allow_all_inactive)
    if args.cmd == "ensure":
        return cmd_ensure(client, apply=args.apply)
    if args.cmd == "fix-game-groups":
        return cmd_fix_live_games_group(client, apply=args.apply)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
