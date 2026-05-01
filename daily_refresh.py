#!/usr/bin/env python3
"""
Daily job: refresh M3U (and optionally EPG), then apply channel→stream remap rules.

Usage:
  set DISPATCHARR_BASE_URL and DISPATCHARR_API_KEY (or USERNAME/PASSWORD)
  python daily_refresh.py
  python daily_refresh.py --m3u-account 1 --remap remap.yaml
  python daily_refresh.py --dry-run

Schedule on Windows: Task Scheduler -> daily trigger -> run:
  py -3 "C:\\path\\to\\daily_refresh.py"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from dispatcharr_client import DispatcharrClient, config_from_env, load_dispatcharr_dotenv


LOG = logging.getLogger("daily_refresh")


def _load_remap_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        if yaml is None:
            raise SystemExit("Install PyYAML to use .yaml remap files: pip install PyYAML")
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise SystemExit("Remap file must be a JSON/YAML object")
    return data


def _find_channel_id_by_name(client: DispatcharrClient, pattern: str) -> Optional[int]:
    rx = re.compile(pattern, re.I)
    for ch in client.iter_channels():
        name = str(ch.get("name") or "")
        if rx.search(name):
            cid = ch.get("id")
            if isinstance(cid, int):
                return cid
    return None


def _resolve_stream_ids(
    client: DispatcharrClient,
    spec: dict[str, Any],
    *,
    default_m3u_account: Optional[int],
) -> list[int]:
    if "stream_ids" in spec:
        raw = spec["stream_ids"]
        if not isinstance(raw, list):
            raise ValueError("stream_ids must be a list of integers")
        return [int(x) for x in raw]

    m3u = spec.get("m3u_account")
    if m3u is not None:
        m3u_id = int(m3u)
    elif default_m3u_account is not None:
        m3u_id = default_m3u_account
    else:
        raise ValueError("Remap entry needs stream_ids or m3u_account + stream_match")

    sm = spec.get("stream_match")
    if not isinstance(sm, dict):
        raise ValueError("stream_match must be an object with 'name_regex' or 'tvg_id'")

    name_rx = sm.get("name_regex")
    tvg = sm.get("tvg_id")
    candidates: list[dict[str, Any]] = []
    for st in client.iter_streams(m3u_account=m3u_id, hide_stale=True):
        if name_rx:
            if re.search(str(name_rx), str(st.get("name") or ""), re.I):
                candidates.append(st)
        elif tvg:
            if str(st.get("tvg_id") or "").lower() == str(tvg).lower():
                candidates.append(st)
        else:
            raise ValueError("stream_match needs name_regex or tvg_id")

    if not candidates:
        raise ValueError(f"No stream matched for m3u_account={m3u_id} spec={spec!r}")

    if len(candidates) > 1 and not spec.get("allow_multiple_streams"):
        names = [str(c.get("name")) for c in candidates[:5]]
        raise ValueError(
            f"Ambiguous stream match ({len(candidates)} streams). "
            f"Tighten stream_match or set allow_multiple_streams: true. Sample: {names}"
        )

    if spec.get("allow_multiple_streams"):
        return [int(c["id"]) for c in candidates]
    return [int(candidates[0]["id"])]


def apply_remap(
    client: DispatcharrClient,
    cfg: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    rules = cfg.get("remap") or cfg.get("channels")
    if not rules:
        LOG.info("No remap rules in config; skipping remap step")
        return
    if not isinstance(rules, list):
        raise SystemExit("remap (or channels) must be a list")

    default_m3u = cfg.get("default_m3u_account")
    if default_m3u is not None:
        default_m3u = int(default_m3u)

    for entry in rules:
        if not isinstance(entry, dict):
            raise SystemExit("Each remap entry must be an object")

        channel_id = entry.get("channel_id")
        if channel_id is None:
            name_pat = entry.get("channel_name_regex")
            if not name_pat:
                raise SystemExit("Remap entry needs channel_id or channel_name_regex")
            channel_id = _find_channel_id_by_name(client, str(name_pat))
            if channel_id is None:
                raise SystemExit(f"No channel matched regex: {name_pat!r}")

        channel_id = int(channel_id)
        stream_ids = _resolve_stream_ids(client, entry, default_m3u_account=default_m3u)

        LOG.info("Channel %s -> streams %s", channel_id, stream_ids)
        if dry_run:
            continue
        client.patch_channel(channel_id, {"streams": stream_ids})


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    load_dispatcharr_dotenv()

    p = argparse.ArgumentParser(description="Dispatcharr daily M3U refresh + optional remap")
    p.add_argument(
        "--m3u-account",
        type=int,
        default=None,
        help="If set, refresh only this M3U account id; otherwise refresh all",
    )
    p.add_argument("--skip-m3u", action="store_true", help="Do not trigger M3U refresh")
    p.add_argument("--epg-import", action="store_true", help="Also POST /api/epg/import/")
    p.add_argument("--epg-force", action="store_true", help="Pass force=true to EPG import")
    p.add_argument("--remap", type=Path, default=None, help="Path to remap.yaml or remap.json")
    p.add_argument("--dry-run", action="store_true", help="Log actions without PATCHing channels")
    args = p.parse_args(argv)

    cfg = config_from_env()
    client = DispatcharrClient(cfg)

    if not args.skip_m3u:
        if args.m3u_account is not None:
            LOG.info("Refreshing M3U account %s", args.m3u_account)
            out = client.refresh_m3u_account(args.m3u_account)
        else:
            LOG.info("Refreshing all M3U accounts")
            out = client.refresh_all_m3u()
        LOG.info("M3U refresh response: %s", out)

    if args.epg_import:
        LOG.info("Importing EPG")
        out = client.import_all_epg(force=args.epg_force)
        LOG.info("EPG import response: %s", out)

    if args.remap:
        data = _load_remap_config(args.remap)
        apply_remap(client, data, dry_run=args.dry_run)

    LOG.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
