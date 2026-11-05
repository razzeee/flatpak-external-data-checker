"""Read and cache update records from generated pull requests."""

import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import tempfile
from email.utils import parsedate_to_datetime
from pathlib import Path

from github.Repository import Repository
from requests.utils import parse_header_links

from .prmetadata import MARKER, MetadataError, parse_metadata
from .updateidentity import canonical, entry_key

log = logging.getLogger(__name__)
RETRY_LABEL = "external-data-checker:retry"


class HistoryError(Exception):
    """Unreadable PR history prevents publication."""


def timestamp(value: str) -> dt.datetime:
    result = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    if result.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("Expected a UTC timestamp")
    return result


@dataclasses.dataclass
class Record:
    number: int
    url: str
    state: str
    retry: bool
    entries: dict[str, dict]
    complete: bool


@dataclasses.dataclass
class PRHistory:
    repository: Repository
    base: str
    manifest: str
    authors: set[int]
    records: list[Record] = dataclasses.field(default_factory=list)
    cache_dir: Path | None = None
    _scan_started: str = dataclasses.field(default="", init=False)

    def _pulls(self, cutoff: str = ""):
        # PullRequest.raw_data makes an extra GET per PR. The list response
        # already contains the required fields, including merged_at.
        page = 1
        while True:
            headers, pulls = self.repository.requester.requestJsonAndCheck(
                "GET",
                f"{self.repository.url}/pulls",
                parameters={
                    "state": "all",
                    "sort": "updated" if cutoff else "created",
                    "direction": "desc" if cutoff else "asc",
                    "per_page": 100,
                    "page": page,
                },
            )
            if not isinstance(pulls, list) or any(
                not isinstance(p, dict) for p in pulls
            ):
                raise HistoryError("Invalid pull request list response")
            if page == 1:
                started = (
                    parsedate_to_datetime(headers["date"])
                    if "date" in headers
                    else dt.datetime.now(dt.timezone.utc)
                )
                self._scan_started = started.astimezone(dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            for raw in pulls:
                if self.cache_dir is not None:
                    try:
                        timestamp(raw["updated_at"])
                    except (KeyError, TypeError, ValueError) as err:
                        raise HistoryError("Invalid PR update timestamp") from err
                if cutoff and raw.get("updated_at", "") < cutoff:
                    return
                yield raw
            links = parse_header_links(str(headers.get("link", "")))
            if not any(link.get("rel") == "next" for link in links):
                break
            page += 1

    def load(self):
        if self.cache_dir is None:
            self._load_records(self._pulls())
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        scope = [str(self.repository.url), self.repository.id, sorted(self.authors)]
        name = hashlib.sha256(canonical(scope).encode()).hexdigest()
        path = self.cache_dir / f"{name}.json"
        with (self.cache_dir / f"{name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._refresh_cache(path)

    def _refresh_cache(self, path: Path):
        cached: dict[str, dict] = {}
        watermark = ""
        try:
            content = json.loads(path.read_text())
            if content["schema"] != 1:
                raise ValueError("Unknown cache schema")
            watermark = content["updated_at"]
            timestamp(watermark)
            cached = content["pulls"]
            if not isinstance(cached, dict) or any(
                not isinstance(raw, dict) or str(raw["number"]) != number
                for number, raw in cached.items()
            ):
                raise ValueError("Invalid cached pull requests")
            self._load_records(cached.values())
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError, HistoryError):
            log.warning("Rebuilding invalid PR history cache %s", path)
            cached, watermark = {}, ""

        # Re-read timestamp ties and updates made while the last scan was running.
        cutoff = (
            (timestamp(watermark) - dt.timedelta(minutes=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            if watermark
            else ""
        )
        for raw in self._pulls(cutoff):
            try:
                number = str(raw["number"])
            except KeyError as err:
                raise HistoryError("PR is missing its number") from err
            cached.pop(number, None)
            if (raw.get("user") or {}).get("id") in self.authors and MARKER in (
                raw.get("body") or ""
            ):
                cached[number] = raw
        self._load_records(cached.values())
        watermark = self._scan_started
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(
                    {"schema": 1, "updated_at": watermark, "pulls": cached}, stream
                )
                # CI archives this cache outside the root-owned Docker action.
                os.fchmod(stream.fileno(), 0o644)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _load_records(self, pulls):
        records = []
        for raw in pulls:
            if (raw.get("user") or {}).get("id") not in self.authors:
                continue
            try:
                if raw["base"]["ref"] != self.base:
                    continue
                metadata = parse_metadata(raw.get("body") or "")
                if metadata is None:
                    continue
                if metadata["repository"] != self.repository.id:
                    raise HistoryError(
                        "Metadata repository does not match PR repository"
                    )
                if metadata["base"] != raw["base"]["ref"]:
                    raise HistoryError(
                        "Retargeted PR needs its metadata base refreshed"
                    )
                if metadata["manifest"] != self.manifest:
                    continue
                if "merged_at" not in raw:
                    raise HistoryError("PR is missing merged_at")
                if raw["merged_at"] is not None:
                    continue
                if raw["state"] not in ("open", "closed"):
                    raise HistoryError("Unknown PR state")
                records.append(
                    Record(
                        raw["number"],
                        raw["html_url"],
                        raw["state"],
                        any(
                            label["name"] == RETRY_LABEL
                            for label in raw.get("labels", [])
                        ),
                        {entry_key(e): e for e in metadata["updates"]},
                        metadata.get("complete", False),
                    )
                )
            except (MetadataError, HistoryError, KeyError, TypeError) as err:
                raise HistoryError(
                    f"Cannot read PR {raw.get('html_url')}: {err}"
                ) from err
        self.records = records
