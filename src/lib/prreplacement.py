import hashlib
import json
import re
from typing import NamedTuple

from packaging.version import Version

from .externaldata import ExternalBase, ExternalFile


def fingerprint(*values: object) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class Target(NamedTuple):
    version: str
    digest: str

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
MARKER = "<!-- fedc-updates-v2 "


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


def advances(version: str, previous: str) -> bool:
    if version == previous == "rotating":
        return True
    return bool(
        re.fullmatch(_VERSION, version)
        and re.fullmatch(_VERSION, previous)
        and Version(version) > Version(previous)
    )


def replacement_marker(head: str, targets: Targets) -> str:
    rows = [
        f"{key} {version} {digest}"
        for key, (version, digest) in sorted(targets.items())
    ]
    return "\n".join([f"{MARKER}{head}", *rows, "-->"])


def supersedes(body: str, head: str, targets: Targets) -> bool:
    """Require full coverage, an untouched head, and at least one newer release."""
    if body.count(MARKER) != 1:
        return False
    marker = re.search(rf"^{MARKER}{re.escape(head)}\n(.*?)\n-->$", body, re.M | re.S)
    if not marker:
        return False
    newer = False
    for row in marker[1].split("\n"):
        entry = re.fullmatch(_ENTRY, row)
        if not entry or entry[1] not in targets:
            return False
        target = targets[entry[1]]
        if target == Target(entry[2], entry[3]):
            continue
        if not advances(target.version, entry[2]):
            return False
        newer = True
    return newer
