"""
Minimal Dispatcharr REST client for daily M3U refresh and channel stream remapping.

API reference: https://mintlify.wiki/Dispatcharr/Dispatcharr/api/overview
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

import requests


def _python_dotenv_installed() -> bool:
    try:
        import dotenv  # noqa: F401
        return True
    except ImportError:
        return False


def load_dispatcharr_dotenv() -> None:
    """Load `.env` from the repository folder containing this file.

    ``python-dotenv``'s default ``load_dotenv()`` reads only the *current working directory*,
    which breaks Task Scheduler, systemd, or SSH runs that don't ``cd`` into the repo.
    """
    env_file = Path(__file__).resolve().parent / ".env"
    try:
        from dotenv import load_dotenv as _load_dotenv
    except ImportError:
        print(
            "WARNING: python-dotenv is not installed — .env files are ignored. "
            "Run: py -3 -m pip install python-dotenv",
            file=sys.stderr,
        )
        return
    if env_file.is_file():
        _load_dotenv(env_file)


@dataclass
class DispatcharrConfig:
    base_url: str
    """Server root from DISPATCHARR_BASE_URL or HOST/PORT (no trailing slash)."""

    api_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    timeout: int = 120

    channel_list_paths: tuple[str, ...] = (
        "/api/channels/channels/",
        "/api/channels/",
    )
    """Try in order until a list request succeeds (Dispatcharr versions differ slightly)."""


class DispatcharrClient:
    def __init__(self, config: DispatcharrConfig):
        self._cfg = config
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._resolved_channel_list_path: Optional[str] = None

        if config.api_key:
            self._session.headers["Authorization"] = f"ApiKey {config.api_key}"
        elif config.username and config.password:
            self._login()
        else:
            raise ValueError("Set api_key or username+password")

    def _url(self, path: str) -> str:
        base = self._cfg.base_url.rstrip("/") + "/"
        return urljoin(base, path.lstrip("/"))

    def _login(self) -> None:
        r = self._session.post(
            self._url("/api/accounts/token/"),
            json={"username": self._cfg.username, "password": self._cfg.password},
            timeout=self._cfg.timeout,
        )
        r.raise_for_status()
        data = r.json()
        self._access_token = data["access"]
        self._refresh_token = data.get("refresh")
        self._session.headers["Authorization"] = f"Bearer {self._access_token}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict[str, Any]] = None,
    ) -> requests.Response:
        r = self._session.request(
            method,
            self._url(path),
            json=json,
            params=params,
            timeout=self._cfg.timeout,
        )
        if r.status_code == 401 and self._refresh_token and not self._cfg.api_key:
            refresh = self._session.post(
                self._url("/api/accounts/token/refresh/"),
                json={"refresh": self._refresh_token},
                timeout=self._cfg.timeout,
            )
            refresh.raise_for_status()
            self._access_token = refresh.json()["access"]
            self._session.headers["Authorization"] = f"Bearer {self._access_token}"
            r = self._session.request(
                method,
                self._url(path),
                json=json,
                params=params,
                timeout=self._cfg.timeout,
            )
        return r

    def refresh_all_m3u(self) -> dict[str, Any]:
        """POST /api/m3u/refresh/ — refresh every active M3U account."""
        r = self._request("POST", "/api/m3u/refresh/")
        r.raise_for_status()
        return r.json() if r.content else {}

    def refresh_m3u_account(self, account_id: int) -> dict[str, Any]:
        """POST /api/m3u/refresh/{id}/ — refresh one M3U account."""
        r = self._request("POST", f"/api/m3u/refresh/{account_id}/")
        r.raise_for_status()
        return r.json() if r.content else {}

    def import_all_epg(self, *, force: bool = False) -> dict[str, Any]:
        """POST /api/epg/import/ — optional companion to M3U refresh."""
        r = self._request("POST", "/api/epg/import/", json={"force": force})
        r.raise_for_status()
        return r.json() if r.content else {}

    def _channel_list_path(self) -> str:
        if self._resolved_channel_list_path:
            return self._resolved_channel_list_path
        for p in self._cfg.channel_list_paths:
            test = self._request("GET", p, params={"page_size": 1})
            if test.status_code == 200:
                self._resolved_channel_list_path = p
                return p
        last = self._cfg.channel_list_paths[-1]
        r = self._request("GET", last, params={"page_size": 1})
        r.raise_for_status()
        self._resolved_channel_list_path = last
        return last

    def iter_m3u_accounts(self, *, page_size: int = 100) -> Iterator[dict[str, Any]]:
        """Walk M3U accounts (GET /api/m3u/accounts/)."""
        path = "/api/m3u/accounts/"
        url: Optional[str] = None
        page = 1
        while True:
            if url:
                r = self._session.get(url, timeout=self._cfg.timeout)
            else:
                r = self._request("GET", path, params={"page": page, "page_size": page_size})
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                for item in data:
                    yield item
                break
            for item in data.get("results", []):
                yield item
            next_url = data.get("next")
            if not next_url:
                break
            url = next_url
            page += 1

    def iter_channels(self, *, page_size: int = 100) -> Iterator[dict[str, Any]]:
        """Walk all channels (handles DRF pagination)."""
        path = self._channel_list_path()
        url: Optional[str] = None
        page = 1
        while True:
            if url:
                r = self._session.get(url, timeout=self._cfg.timeout)
            else:
                r = self._request(
                    "GET",
                    path,
                    params={"page": page, "page_size": page_size},
                )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                for item in data:
                    yield item
                break
            for item in data.get("results", []):
                yield item
            next_url = data.get("next")
            if not next_url:
                break
            url = next_url
            page += 1

    def get_channel_streams_detail(self, channel_id: int) -> list[dict[str, Any]]:
        """GET /api/channels/channels/{id}/streams/ — full stream rows for one channel."""
        r = self._request("GET", f"/api/channels/channels/{channel_id}/streams/")
        if r.status_code == 404:
            r = self._request("GET", f"/api/channels/{channel_id}/streams/")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "results" in data:
            return list(data["results"])
        return []

    def iter_streams(
        self,
        *,
        page_size: int = 500,
        m3u_account: Optional[int] = None,
        channel_group: Optional[str] = None,
        search: Optional[str] = None,
        hide_stale: bool = True,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"page_size": page_size, "hide_stale": str(hide_stale).lower()}
        if m3u_account is not None:
            params["m3u_account"] = m3u_account
        if channel_group:
            params["channel_group"] = channel_group
        if search:
            params["search"] = search

        path = "/api/channels/streams/"
        url: Optional[str] = None
        page = 1
        while True:
            if url:
                r = self._session.get(url, timeout=self._cfg.timeout)
            else:
                r = self._request("GET", path, params={**params, "page": page})
            r.raise_for_status()
            data = r.json()
            for item in data.get("results", []):
                yield item
            next_url = data.get("next")
            if not next_url:
                break
            url = next_url
            page += 1

    def patch_channel(self, channel_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH channel (e.g. {\"streams\": [1,2,3]} for failover order)."""
        for path in (f"/api/channels/channels/{channel_id}/", f"/api/channels/{channel_id}/"):
            r = self._request("PATCH", path, json=body)
            if r.status_code != 404:
                r.raise_for_status()
                return r.json() if r.content else {}
        r.raise_for_status()
        return {}

    def create_channel(
        self,
        *,
        name: str,
        channel_number: float,
        channel_group_id: int,
    ) -> dict[str, Any]:
        """POST /api/channels/channels/ — returns created channel including id."""
        body = {
            "name": name,
            "channel_number": float(channel_number),
            "channel_group_id": int(channel_group_id),
        }
        r = self._request("POST", "/api/channels/channels/", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    def delete_channel(self, channel_id: int) -> None:
        """DELETE channel."""
        for path in (f"/api/channels/channels/{channel_id}/", f"/api/channels/{channel_id}/"):
            r = self._request("DELETE", path)
            if r.status_code != 404:
                r.raise_for_status()
                return
        r.raise_for_status()


def channel_group_id_from_stream_group(
    client: DispatcharrClient,
    *,
    m3u_account_id: int,
    stream_group: str,
    hide_stale: bool = False,
) -> int:
    """Resolve ``channel_group_id`` from any stream in that M3U playlist group (e.g. ``Live-Games``).

    Dispatcharr ties channels to the same numeric group id as streams in that category.
    Do **not** use ``iter_streams`` without a group filter — the first row may be another group.
    """
    s = next(
        client.iter_streams(
            page_size=30,
            m3u_account=m3u_account_id,
            channel_group=stream_group,
            hide_stale=hide_stale,
        ),
        None,
    )
    if not s:
        raise ValueError(
            f"No stream in group {stream_group!r} for M3U account {m3u_account_id}. "
            "Refresh M3U or check DISPATCHARR_NUFU_* group env names.",
        )
    cg = s.get("channel_group")
    if not isinstance(cg, int):
        raise ValueError("Stream row missing numeric channel_group.")
    return cg


def resolve_dispatcharr_base_url() -> str:
    """Prefer DISPATCHARR_BASE_URL; otherwise build from HOST (+ optional PORT, SCHEME)."""
    raw = os.environ.get("DISPATCHARR_BASE_URL", "").strip().rstrip("/")
    if raw:
        return raw

    host = os.environ.get("DISPATCHARR_HOST", "").strip()
    if not host:
        return ""

    scheme = os.environ.get("DISPATCHARR_SCHEME", "http").strip().rstrip(":/") or "http"
    port = os.environ.get("DISPATCHARR_PORT", "").strip()
    if port:
        return f"{scheme}://{host}:{port}".rstrip("/")
    return f"{scheme}://{host}".rstrip("/")


def config_from_env() -> DispatcharrConfig:
    base = resolve_dispatcharr_base_url()
    if not base:
        env_path = Path(__file__).resolve().parent / ".env"
        lines = [
            "Set DISPATCHARR_BASE_URL (full URL, no trailing slash), or DISPATCHARR_HOST with",
            "optional DISPATCHARR_PORT and DISPATCHARR_SCHEME (default http).",
            "",
        ]
        if not _python_dotenv_installed():
            lines.append(
                "This install is missing python-dotenv, so .env is never read. Run:\n"
                "  py -3 -m pip install -r requirements.txt"
            )
        elif not env_path.is_file():
            lines.append(
                f"No .env file at:\n  {env_path}\n"
                "Copy .env.example to .env on the machine that runs the script, or set the variables in Windows / Task Scheduler."
            )
        else:
            lines.append(
                f".env exists at {env_path} but DISPATCHARR_BASE_URL (or HOST) is still empty.\n"
                "Check: one line like DISPATCHARR_BASE_URL=http://192.168.1.1:9191 (no spaces around =), file saved as UTF-8, and the same folder as dispatcharr_client.py on the run host."
            )
        raise SystemExit("\n".join(lines))

    api_key = os.environ.get("DISPATCHARR_API_KEY")
    user = os.environ.get("DISPATCHARR_USERNAME")
    password = os.environ.get("DISPATCHARR_PASSWORD")

    timeout = int(os.environ.get("DISPATCHARR_TIMEOUT", "120"))

    return DispatcharrConfig(
        base_url=base,
        api_key=api_key or None,
        username=user or None,
        password=password or None,
        timeout=timeout,
    )
