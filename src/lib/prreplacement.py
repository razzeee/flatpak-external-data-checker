import hashlib
import json
import re
from enum import Enum, auto
from typing import NamedTuple

from packaging.version import Version

from .externaldata import ExternalBase, ExternalFile


def fingerprint(*values: object) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, default=str).encode()
    ).hexdigest()


class Change(Enum):
    SAME = auto()
    NEWER = auto()
    INCOMPARABLE = auto()


class Target(NamedTuple):
    version: str
    digest: str

    @classmethod
    def from_manifest_source(cls, version: str, source: dict) -> "Target":
        if version == "rotating":
            fields = {
                k: source[k] for k in ("md5", "sha1", "sha256", "sha512") if k in source
            }
            return cls(version, fingerprint(fields))
        return cls(version, fingerprint(source))

    def compare(self, previous: "Target", *, later_observation: bool) -> Change:
        if self == previous:
            return Change.SAME
        if self.version == previous.version == "rotating" and later_observation:
            return Change.NEWER
        if (
            re.fullmatch(_VERSION, self.version)
            and re.fullmatch(_VERSION, previous.version)
            and Version(self.version) > Version(previous.version)
        ):
            return Change.NEWER
        return Change.INCOMPARABLE

    @classmethod
    def from_source(cls, data: ExternalBase) -> "Target":
        assert data.new_version is not None
        version = release_version(data)
        state = data.new_version.json
        state.pop("timestamp")
        return cls(
            version, fingerprint(state["checksum"] if version == "rotating" else state)
        )


Targets = dict[str, Target]
_VERSION = r"v?[0-9]{1,16}(?:\.[0-9]{1,16})*(?:(?:a|b|rc)[0-9]{1,16})?"
_ENTRY = rf"([0-9a-f]{{64}}) ({_VERSION}|unknown|rotating) ([0-9a-f]{{64}})"
MARKER = "<!-- fedc-updates-v3 "


def release_version(data: ExternalBase) -> str:
    assert data.new_version is not None
    value = data.new_version.version
    if (
        value is None
        and data.checker_data.get("type") == "rotating-url"
        and isinstance(data.new_version, ExternalFile)
    ):
        return "rotating"
    return value if value and re.fullmatch(_VERSION, value) else "unknown"


def replacement_marker(head: str, targets: Targets) -> str:
    rows = [
        f"{key} {version} {digest}"
        for key, (version, digest) in sorted(targets.items())
    ]
    return "\n".join([f"{MARKER}{head}", *rows, "-->"])


def supersedes(
    body: str, head: str, targets: Targets, *, later_observation: bool = True
) -> bool:
    """Require full coverage, an untouched head, and at least one newer release."""
    if body.count(MARKER) != 1:
        return False
    marker = re.search(rf"^{MARKER}{re.escape(head)}\n(.*?)\n-->$", body, re.M | re.S)
    if not marker:
        return False
    newer = False
    for row in marker[1].split("\n"):
        entry = re.fullmatch(_ENTRY, row)
        if not entry:
            return False
        key, version, digest = entry.groups()
        if key not in targets:
            return False
        change = targets[key].compare(
            Target(version, digest), later_observation=later_observation
        )
        if change is Change.INCOMPARABLE:
            return False
        newer |= change is Change.NEWER
    return newer
