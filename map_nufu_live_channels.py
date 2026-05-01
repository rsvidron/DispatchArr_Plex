#!/usr/bin/env python3
"""CLI: map Nufu **Live-Channels** streams onto **Nufu Live Channels 01..N** placeholders.

See map_nufu_live_games.py module docstring for DISPATCHARR_NUFU_LIVE_CHANNELS_* env vars.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dispatcharr_client import DispatcharrClient, config_from_env, load_dispatcharr_dotenv

from map_nufu_live_games import (
    dedupe_live_channel_placeholder_channels,
    run_map_nufu_live_channels,
    write_live_channels_mapping_file,
    _live_channels_prefix,
    _live_channels_stream_group_id,
)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dispatcharr_dotenv()

    ap = argparse.ArgumentParser(description="Map Nufu Live-Channels streams to numbered placeholders")
    ap.add_argument(
        "--write-initial-mapping",
        action="store_true",
        help="Fetch Live-Channels from API and write nufu_live_channels_mapping.json (then edit; re-run with --apply)",
    )
    ap.add_argument(
        "--dedupe-placeholders",
        action="store_true",
        help="DELETE duplicate 'Nufu Live Channels NN' rows when the same dial already has a real channel (needs --apply)",
    )
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
        help="Do not PATCH channel name to stream title (keep Nufu Live Channels NN placeholders)",
    )
    args = ap.parse_args(argv)

    client = DispatcharrClient(config_from_env())
    if args.write_initial_mapping:
        write_live_channels_mapping_file(client)
        return 0

    if args.dedupe_placeholders:
        gid = _live_channels_stream_group_id(client)
        return dedupe_live_channel_placeholder_channels(
            client,
            placeholder_prefix=_live_channels_prefix(),
            channel_group_id=gid,
            apply=args.apply,
        )

    return run_map_nufu_live_channels(
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
