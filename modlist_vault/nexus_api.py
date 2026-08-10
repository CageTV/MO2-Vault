"""
Minimal Nexus API client (just enough to resolve a Premium download link), plus a
generic HTTP downloader used both for Nexus's resolved CDN links and for any other
plain URL recorded in a mod's .meta (e.g. a GitHub release asset).
"""

import http.client
import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from .util import logger


def _sanitize_url(uri: str) -> str:
    """Nexus's CDN links embed the archive's raw display filename - spaces and
    all - directly in the URL path without percent-encoding it. urllib correctly
    refuses to open a URL like that, so encode just the path before use."""
    parts = urlsplit(uri)
    safe_path = quote(parts.path, safe="/")
    return urlunsplit((parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment))


@dataclass
class NexusDownloadLink:
    name: str
    short_name: str
    uri: str


def _download_link_from_response(response_json) -> Optional[NexusDownloadLink]:
    links = [
        NexusDownloadLink(name=item["name"], short_name=item["short_name"], uri=item["URI"])
        for item in response_json
    ]
    if not links:
        return None
    # Prefer Nexus's own CDN over third-party premium mirrors when both are offered.
    preferred = next((link for link in links if link.short_name == "Nexus CDN"), None)
    return preferred or links[0]


class NexusApi:
    _BASE_URL = "api.nexusmods.com"
    _API_KEY_HEADER = "apiKey"
    _DOWNLOAD_LINK_PATH = "/v1/games/{game_domain_name}/mods/{mod_id}/files/{file_id}/download_link.json"
    _FILES_PATH = "/v1/games/{game_domain_name}/mods/{mod_id}/files.json"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def get_download_link(self, game_domain_name: str, mod_id: int, file_id: int) -> Optional[str]:
        """Requires a Premium API key - non-premium keys get a 403 from Nexus here."""
        endpoint = self._DOWNLOAD_LINK_PATH.format(
            game_domain_name=game_domain_name, mod_id=mod_id, file_id=file_id
        )
        try:
            response = self._get(endpoint)
            if not response:
                return None
            link = _download_link_from_response(response)
            return link.uri if link else None
        except Exception as e:
            logger.error("Nexus download-link lookup failed: %s", e)
            return None

    def find_current_file_id(
        self, game_domain_name: str, mod_id: int, old_file_id: int, archive_name: Optional[str] = None
    ) -> Optional[int]:
        """A recorded file_id can go stale (404) even while the mod page itself is
        still up - authors delete and re-upload individual files more often than
        you'd expect, sometimes without Nexus recording it as a formal "update".
        Tries, in order: following Nexus's own old->new update chain (possibly
        more than one hop), an exact archive-filename match among the mod's
        current files, then whichever file is marked as the mod's MAIN file."""
        endpoint = self._FILES_PATH.format(game_domain_name=game_domain_name, mod_id=mod_id)
        try:
            response = self._get(endpoint)
        except Exception as e:
            logger.error("Nexus files.json lookup failed for mod %d: %s", mod_id, e)
            return None
        if not response:
            return None

        files = response.get("files", [])
        valid_ids = {f.get("file_id") for f in files}

        updates = response.get("file_updates", [])
        current = old_file_id
        seen = {current}
        changed = True
        while changed:
            changed = False
            for update in updates:
                new_id = update.get("new_file_id")
                if update.get("old_file_id") == current and new_id not in seen:
                    current = new_id
                    seen.add(current)
                    changed = True
        if current != old_file_id and current in valid_ids:
            return current

        # Some mod pages carry several files with the *identical* file_name
        # (re-uploads left in place as orphaned duplicates, only one of which
        # is actually live) - always prefer whichever is marked MAIN over just
        # taking the first name match, since the non-MAIN duplicates are
        # frequently the dead ones.
        if archive_name:
            exact_matches = [f for f in files if f.get("file_name") == archive_name]
            main_exact = next((f for f in exact_matches if f.get("category_name") == "MAIN"), None)
            if main_exact:
                return main_exact.get("file_id")

        main = next((f for f in files if f.get("category_name") == "MAIN"), None)
        if main:
            return main.get("file_id")

        if archive_name and exact_matches:
            return exact_matches[0].get("file_id")
        return None

    def _get(self, endpoint: str):
        conn = None
        try:
            conn = http.client.HTTPSConnection(self._BASE_URL)
            headers = {self._API_KEY_HEADER: self._api_key}
            conn.request("GET", endpoint, headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                raise Exception(f"Request failed with status: {response.status} {response.reason}")
            return json.loads(response.read().decode("utf-8"))
        finally:
            if conn:
                conn.close()


def download_file(
    uri: str,
    destination: Path,
    chunk_size: int = 1024 * 1024,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> None:
    """Streams any direct-download URL to disk (Nexus CDN links or a generic URL
    recorded in a mod's .meta, e.g. a GitHub release asset). Raises on any
    HTTP/network error - callers decide how to handle failures per mod."""
    request = urllib.request.Request(_sanitize_url(uri), headers={"User-Agent": "mo2-modlist-vault/1.0"})
    with urllib.request.urlopen(request) as response:
        total = int(response.headers.get("Content-Length", 0))
        written = 0
        with open(destination, "wb") as out_file:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                out_file.write(chunk)
                written += len(chunk)
                if progress_callback:
                    progress_callback(written, total)
