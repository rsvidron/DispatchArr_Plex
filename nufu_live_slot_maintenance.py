#!/usr/bin/env python3
"""
Maintain Nufu live placeholder channels (names like "Nufu Live Games 01" .. "50").

prune  — DELETE any 1–50 live slot whose streams are empty or only *stale* rows,
         so finished / non-existent games disappear from Dispatcharr entirely.

ensure — Recreate any missing "Nufu Live Games NN" channels for numbers 1–50
         (after prune, or partial deletes). Uses the same Nufu channel_group as
         reserve_nufu_live_block.py.

Typical schedule:
  - After games / before export:  py -3 nufu_live_slot_maintenance.py prune --apply
  - Before next fill:            py -3 nufu_live_slot_maintenance.py ensure --apply

If *every* slot is inactive (nothing scheduled), pruning would remove all 50;
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

from dispatcharr_client import DispatcharrClient, config_from_env, load_dispatcharr_dotenv

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


def _channel_group_from_nufu_stream(client: DispatcharrClient, account_id: int) -> int:
    s = next(client.iter_streams(m3u_account=account_id, page_size=1, hide_stale=False))
    cg = s.get("channel_group")
    if not isinstance(cg, int):
        raise SystemExit("Could not read channel_group from a Nufu stream row.")
    return cg


def _name_regex(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}\s+(\d{{2}})\s*$", re.I)


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


def _placeholder_slots(channels: list[dict[str, Any]], prefix: str) -> dict[int, dict[str, Any]]:
    """slot 1..50 -> channel dict (matched by name only)."""
    rx = _name_regex(prefix)
    out: dict[int, dict[str, Any]] = {}
    for ch in channels:
        m = rx.match(str(ch.get("name") or ""))
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= 50:
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
    slots = _placeholder_slots(channels, prefix)
    if not slots:
        LOG.info("No channels match %r 01..50; nothing to prune.", prefix)
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


def cmd_ensure(client: DispatcharrClient, *, apply: bool) -> int:
    prefix = _prefix()
    channels = _list_channels(client)
    existing = _placeholder_slots(channels, prefix)
    missing = [n for n in range(1, 51) if n not in existing]
    if not missing:
        LOG.info("All 50 placeholders already exist for prefix %r.", prefix)
        return 0

    nufu_id = _nufu_account_id(client)
    cg = _channel_group_from_nufu_stream(client, nufu_id)
    LOG.info(
        "Creating %s missing placeholders (Nufu account=%s channel_group=%s)",
        len(missing),
        nufu_id,
        cg,
    )
    if not apply:
        LOG.info("Dry run: no POST.")
        return 0

    for n in missing:
        name = f"{prefix} {n:02d}"
        client.create_channel(name=name, channel_number=float(n), channel_group_id=cg)
        LOG.info("Created %s ch#%s", name, n)
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

    ap = argparse.ArgumentParser(description="Nufu live slot 1-50 maintenance")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_prune = sub.add_parser("prune", help="Delete inactive live placeholders (no non-stale stream)")
    p_prune.add_argument("--apply", action="store_true", help="Perform DELETEs")
    p_prune.add_argument(
        "--allow-all-inactive",
        action="store_true",
        help="Allow deleting every placeholder when none have an active stream",
    )

    p_ensure = sub.add_parser("ensure", help="Create missing Nufu Live Games 01..50 channels")
    p_ensure.add_argument("--apply", action="store_true", help="Perform POST creates")

    args = ap.parse_args(argv)
    client = DispatcharrClient(config_from_env())

    if args.cmd == "prune":
        return cmd_prune(client, apply=args.apply, allow_all_inactive=args.allow_all_inactive)
    if args.cmd == "ensure":
        return cmd_ensure(client, apply=args.apply)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
