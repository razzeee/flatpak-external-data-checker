"""Identify sources and releases independently of PR display text."""

import dataclasses
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .externaldata import BuilderModule, ExternalBase, ExternalFile, ExternalGitRef

log = logging.getLogger(__name__)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def repository_url(url: str) -> str:
    # Other Git servers may distinguish these URL forms.
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.removeprefix("git@github.com:")
    parts = urlsplit(url)
    if parts.hostname == "github.com" and not parts.query and not parts.fragment:
        path = parts.path.rstrip("/").removesuffix(".git").lower()
        return urlunsplit(("https", "github.com", path, "", ""))
    return url


def restrictions(config: dict) -> dict:
    return {
        name: sorted(set(config[name]))
        for name in ("only-arches", "skip-arches")
        if config.get(name)
    }


def ancestry(module: BuilderModule | None) -> list[BuilderModule]:
    result = []
    while module:
        result.append(module)
        module = module.parent
    return list(reversed(result))


def source_key(
    data: ExternalBase,
    checkout: Path,
    root_config: dict,
    owners: list[BuilderModule],
) -> dict | None:
    metadata = data.checker_data
    scopes = []
    for owner in owners:
        scope = [restrictions(m.module) for m in ancestry(owner)]
        scopes.append([s for s in scope if s])
    arches = {
        "checker": sorted(set(metadata.get("arches") or [])),
        "source": restrictions(data.source),
        "root": restrictions(root_config),
        # Keep unrestricted uses of shared sources when other uses restrict arches.
        "modules": sorted({canonical(s) for s in scopes}) if any(scopes) else [],
    }
    if metadata.get("source-id"):
        return {"id": metadata["source-id"], "arches": arches}

    kind = metadata.get("type")
    selector: dict[str, Any]
    if isinstance(data.current_version, ExternalGitRef):
        selector = {"repository": repository_url(data.current_version.url)}
        target = data.new_version or data.current_version
        assert isinstance(target, ExternalGitRef)
        if not target.tag:
            selector["ref"] = target.branch or "HEAD"
    elif kind == "github":
        repo = metadata.get("repo")
        if not isinstance(repo, str) or not repo:
            return None
        selector = {
            "checker": kind,
            "repo": repo.lower(),
            "asset-name": metadata.get("asset-name"),
            "source-archive": metadata.get("source-archive"),
        }
    elif kind == "rotating-url":
        url = metadata.get("url")
        if not isinstance(url, str) or not url:
            return None
        selector = {"checker": kind, "url": url}
    else:
        # Arbitrary query expressions and version-bearing URLs are not stable IDs.
        return None
    selector["destination"] = data.source.get("dest-filename") or data.source.get(
        "filename"
    )
    return {
        "file": Path(data.source_path).resolve().relative_to(checkout).as_posix(),
        "modules": [m.name for m in ancestry(data.module)],
        "type": data.type.value,
        "selector": selector,
        "arches": arches,
    }


def release_key(data: ExternalBase) -> dict | None:
    new = data.new_version
    if isinstance(new, ExternalGitRef):
        repo = repository_url(new.url)
        if new.tag:
            return {"kind": "tag", "value": new.tag, "repository": repo}
        if new.commit and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", new.commit):
            return {
                "kind": "commit",
                "value": new.commit.lower(),
                "repository": repo,
                "ref": new.branch or "HEAD",
            }
    elif isinstance(new, ExternalFile):
        if new.version:
            return {"kind": "version", "value": new.version}
        for algorithm, length in (("sha256", 64), ("sha512", 128)):
            digest = getattr(new.checksum, algorithm)
            if digest and re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", digest):
                return {"kind": algorithm, "value": digest.lower()}
    return None


def entry_key(entry: dict) -> str:
    return canonical([entry["source"], entry["release"]])


@dataclasses.dataclass
class Candidate:
    data: ExternalBase
    entry: dict[str, Any]

    @property
    def key(self) -> str:
        return entry_key(self.entry)


def build_candidates(
    sources: list[ExternalBase],
    checkout: Path,
    root_config: dict,
    modules: list[BuilderModule],
) -> list[Candidate]:
    candidates = []
    keys: dict[str, int] = {}
    for data in sources:
        owners = [m for m in modules if any(s is data for s in m.sources)]
        source = source_key(data, checkout.resolve(), root_config, owners)
        if source is not None:
            key = canonical(source)
            keys[key] = keys.get(key, 0) + 1
        if data.new_version is None:
            continue
        release = release_key(data)
        if source is None or release is None:
            log.warning(
                "Cannot identify update for %s; set a unique x-checker-data "
                "source-id and provide a version or strong checksum. "
                "Only PRs with the same base and head branch can be matched.",
                data,
            )
            continue
        new = data.new_version
        evidence: dict[str, Any] = {"url": new.url, "version": new.version}
        if isinstance(new, ExternalGitRef):
            evidence["commit"] = new.commit
        elif isinstance(new, ExternalFile):
            evidence["checksum"] = new.checksum._asdict()
        candidate = Candidate(
            data, {"source": source, "release": release, "evidence": evidence}
        )
        candidates.append(candidate)
    ambiguous = {k for k, count in keys.items() if count > 1}
    for key in ambiguous:
        log.warning("Ambiguous source identity %s; set distinct source-id values", key)
    return [c for c in candidates if canonical(c.entry["source"]) not in ambiguous]
