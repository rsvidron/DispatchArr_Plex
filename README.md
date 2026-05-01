# DispatchArr Plex automation

Python helpers for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr): refresh M3U playlists, remap channel→stream IDs after provider churn, reserve channel numbers **1–50** for same-day live games (Nufu), map **Live-Games** streams into those slots, and optionally prune unused slots or sync **`tvg_id`** for EPG/XMLTV matching.

Official API docs: [Dispatcharr API overview](https://mintlify.wiki/Dispatcharr/Dispatcharr/api/overview).

## Requirements

- Python **3.10+** (uses `py -3` on Windows)
- A Dispatcharr instance reachable from this machine (e.g. `http://192.168.x.x:9191`)
- An API key (**Profile** in the UI) or username/password for JWT

## Setup

```powershell
cd path\to\DispatchArr_Plex
py -3 -m pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:

| Variable | Description |
|----------|-------------|
| `DISPATCHARR_BASE_URL` | Server root, no trailing slash (e.g. `http://192.168.5.82:9191`) |
| `DISPATCHARR_API_KEY` | Preferred for scripts / Task Scheduler |
| `DISPATCHARR_POST_REFRESH_WAIT` | Seconds to wait after M3U refresh before rebuilding stream index (default `60`) |

Optional Nufu-related variables are documented in `.env.example`.

## Scripts

| Script | Purpose |
|--------|---------|
| `dispatcharr_client.py` | Thin REST client (auth, channels, streams, M3U refresh, PATCH channels). |
| `sync_streams_after_m3u.py` | Refresh M3U(s), wait, remap every channel’s stream list to fresh rows (`tvg_id` / name match). Optional `--map-nufu-live-games`, prune flags. |
| `map_nufu_live_games.py` | Assign Nufu **Live-Games** group streams to **Nufu Live Games 01–50**; clears extra slots; syncs channel **`tvg_id`** for guide/XMLTV unless disabled. |
| `reserve_nufu_live_block.py` | One-time: create placeholders **1–50**, shift existing channels to **51+** (preserving previous order). |
| `nufu_live_slot_maintenance.py` | `prune` — delete inactive live placeholders; `ensure` — recreate missing **01–50** channels. |
| `daily_refresh.py` | Older workflow: M3U refresh + optional YAML remap rules. |
| `run_dispatcharr_daily.bat` | Windows **Task Scheduler** entry: full sync + Nufu map + logging under `logs\`. |

### Typical daily command

```powershell
py -3 sync_streams_after_m3u.py --map-nufu-live-games
```

Dry run (no PATCH):

```powershell
py -3 sync_streams_after_m3u.py --map-nufu-live-games --dry-run
```

## Windows Task Scheduler

Use `run_dispatcharr_daily.bat`. Logs go to `logs\run_YYYY-MM-dd_HHmmss.log`. See comments inside the `.bat` for **Program** / **Start in** settings.

The scheduled account needs network access to your Dispatcharr host and `py` on `PATH`.

## Security

- **Never commit `.env`** (see `.gitignore`). Only `.env.example` is tracked.
- Rotate API keys if they were exposed.

## Repository

Remote: [github.com/rsvidron/DispatchArr_Plex](https://github.com/rsvidron/DispatchArr_Plex)

```powershell
git remote add origin https://github.com/rsvidron/DispatchArr_Plex.git
git push -u origin main
```

Use a [personal access token](https://github.com/settings/tokens) as the password when Git prompts for HTTPS credentials (GitHub no longer accepts account passwords for Git).
