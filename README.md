# DispatchArr Plex automation

Python helpers for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr): refresh M3U playlists, remap channel→stream IDs after provider churn, reserve a high dial block (default **500–550**) for same-day **Live-Games** (Nufu), map **Live-Channels** from dial **101** (dials **1–100** free for manual channels), optional **channel display names** for Plex, prune, and sync **`tvg_id`** for EPG/XMLTV matching.

Official API docs: [Dispatcharr API overview](https://mintlify.wiki/Dispatcharr/Dispatcharr/api/overview).

## Requirements

- Python **3.10+** (uses `py -3` on Windows)
- A Dispatcharr instance reachable from this machine (configure URL via `.env`; nothing is hardcoded)
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
| `DISPATCHARR_BASE_URL` | Full server URL, no trailing slash (e.g. `http://lan-host:9191`). **Preferred.** |
| `DISPATCHARR_HOST` | If `BASE_URL` is unset: hostname or IP only; optional with `DISPATCHARR_PORT` and `DISPATCHARR_SCHEME` (default `http`). |
| `DISPATCHARR_PORT` | Optional; used only when building URL from `DISPATCHARR_HOST`. |
| `DISPATCHARR_SCHEME` | Optional; `http` or `https` when using `HOST`/`PORT`. |
| `DISPATCHARR_API_KEY` | Preferred for scripts / Task Scheduler |
| `DISPATCHARR_POST_REFRESH_WAIT` | Seconds to wait after M3U refresh before rebuilding stream index (default `60`) |

Optional Nufu-related variables are documented in `.env.example`.

## New setup (step by step)

Use this when cloning the repo onto a new machine or standing up Dispatcharr from scratch.

1. **Dispatcharr / provider**
   - Install Dispatcharr and add your Nufu (or other) M3U account so playlists import.
   - In the UI, note the **M3U account ID** (used as `DISPATCHARR_NUFU_M3U_ACCOUNT_ID`).
   - Confirm your provider exposes **Live-Games** and **Live-Channels** (or adjust `DISPATCHARR_NUFU_LIVE_STREAM_GROUP` / `DISPATCHARR_NUFU_LIVE_CHANNELS_*` in `.env`).

2. **Python environment**
   - Install Python **3.10+** and clone this repository.
   - `py -3 -m pip install -r requirements.txt`
   - `copy .env.example .env` and fill in `DISPATCHARR_BASE_URL` (or host/port) and `DISPATCHARR_API_KEY`.

3. **Nufu env (minimum)**
   - Set `DISPATCHARR_NUFU_M3U_ACCOUNT_ID` to the account from step 1.
   - Defaults: live games **500–550**; **Live-Channels** slot 1 at dial **101** (`DISPATCHARR_NUFU_LIVE_CHANNELS_CHANNEL_START=101`); dials **1–100** unused by scripts for manual adds. After `reserve_nufu_live_block.py`, other automated channels start at **101** (`DISPATCHARR_NUFU_MAIN_LIBRARY_START`). Override in `.env` if needed.

4. **One-time channel layout (if needed)**
   - If the **500–550** game block is not set up yet, run `reserve_nufu_live_block.py` **once** to create **Nufu Live Games 01..NN** on those dials and renumber other channels from **101** upward (leaving **1–100** free unless you change env).
   - Or use `nufu_live_slot_maintenance.py ensure` to create any missing **Nufu Live Games** placeholders.

5. **Live-Channels mapping (optional but recommended for stable Plex order)**
   - Generate a local mapping file (not committed to git; machine-specific):
     - `py -3 map_nufu_live_channels.py --write-initial-mapping`
   - Edit `nufu_live_channels_mapping.json` if you need to fix slot→`tvg_id` / name matching after playlist changes.
   - Set `DISPATCHARR_NUFU_LIVE_CHANNELS_MODE=by_channel_number` if your M3U already created channels on dials **101+** (typical).

6. **Guide / Plex labels for live games (optional)**
   - With defaults, mapping live games also PATCHes each slot’s **channel name** to the stream title so clients like Plex can show the game in the guide (`DISPATCHARR_NUFU_LIVE_SYNC_CHANNEL_NAME`, `DISPATCHARR_NUFU_LIVE_CHANNEL_NAME_TEMPLATE` in `.env.example`).
   - Plex picks this up on its normal **guide refresh** cycle; you do not need to call the Plex API unless you want an immediate refresh after each sync.

7. **Verify before writing changes**
   - `py -3 sync_streams_after_m3u.py --map-nufu-live-games --map-nufu-live-channels --dry-run`
   - Inspect the log; then remove `--dry-run` to apply.

8. **Automate (Windows)**
   - Schedule `run_dispatcharr_daily.bat` (see **Windows Task Scheduler** below). Ensure **Start in** is the folder that contains `.env`.

## Scripts

| Script | Purpose |
|--------|---------|
| `dispatcharr_client.py` | Thin REST client (auth, channels, streams, M3U refresh, PATCH channels). |
| `sync_streams_after_m3u.py` | Refresh M3U(s), wait, remap every channel’s stream list to fresh rows (`tvg_id` / name match). Optional `--map-nufu-live-games`, prune flags. |
| `map_nufu_live_games.py` | Assign Nufu **Live-Games** streams to **Nufu Live Games 01..N** on the configured dial block; syncs **`tvg_id`** and (by default) **channel display name** unless disabled. |
| `map_nufu_live_channels.py` | Helpers for **Live-Channels** mapping JSON (`--write-initial-mapping`). |
| `nufu_live_channels_mapping.py` | Loads slot mapping and resolves streams by `tvg_id`/name (used by `sync_streams_after_m3u.py`). |
| `reserve_nufu_live_block.py` | One-time: create live-game placeholders on the **high dial block** (default 500–550), renumber other channels from **1** (see `.env`). |
| `nufu_live_slot_maintenance.py` | `prune` / `ensure` / `fix-game-groups` — maintenance for the live-game placeholder block. |
| `daily_refresh.py` | Older workflow: M3U refresh + optional YAML remap rules. |
| `run_dispatcharr_daily.bat` | Windows **Task Scheduler** entry: full sync + Nufu map + logging under `logs\`. |

### Typical daily command

Full daily sync (matches `run_dispatcharr_daily.bat`: refresh, remap, live games + live channels):

```powershell
py -3 sync_streams_after_m3u.py --map-nufu-live-games --map-nufu-live-channels
```

Live games only:

```powershell
py -3 sync_streams_after_m3u.py --map-nufu-live-games
```

Dry run (no PATCH):

```powershell
py -3 sync_streams_after_m3u.py --map-nufu-live-games --dry-run
```

## Windows Task Scheduler

Use `run_dispatcharr_daily.bat`. Logs go to `logs\run_YYYY-MM-dd_HHmmss.log`. See comments inside the `.bat` for **Program** / **Start in** settings.

Scripts load **`.env` from the repository folder** (next to `dispatcharr_client.py`), not from the process “current directory”. That way scheduled tasks still pick up secrets even if **Start in** is wrong. You still need a real **`.env`** on the machine (copy from `.env.example`); **`.env.example` is never loaded by the code**—only documentation.

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
