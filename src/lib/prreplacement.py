import hashlib
import json
import re

from packaging.version import Version


def fingerprint(*values: object) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


Targets = dict[str, tuple[str, str]]
_VERSION = r"v?[0-9]{1,16}(?:\.[0-9]{1,16})*(?:(?:a|b|rc)[0-9]{1,16})?"
_ENTRY = rf"([0-9a-f]{{64}}) ({_VERSION}|unknown) ([0-9a-f]{{64}})"


def release_version(value: str | None) -> str:
    return value if value and re.fullmatch(_VERSION, value) else "unknown"


def replacement_marker(head: str, targets: Targets) -> str:
    rows = [f"{key} {version} {digest}" for key, (version, digest) in targets.items()]
    return "\n".join([f"<!-- fedc-updates-v1 {head}", *rows, "-->"])


def supersedes(body: str, head: str, targets: Targets) -> bool:
    """Require full coverage, an untouched head, and at least one newer release."""
    prefix = "<!-- fedc-updates-v1 "
    if body.count(prefix) != 1:
        return False
    marker = re.search(rf"^{prefix}{re.escape(head)}\n(.*?)\n-->$", body, re.M | re.S)
    if not marker:
        return False
    newer = False
    for row in marker[1].split("\n"):
        entry = re.fullmatch(_ENTRY, row)
        if not entry or entry[1] not in targets:
            return False
        version, digest = targets[entry[1]]
        if (version, digest) == (entry[2], entry[3]):
            continue
        if "unknown" in (version, entry[2]) or Version(version) <= Version(entry[2]):
            return False
        newer = True
    return newer
