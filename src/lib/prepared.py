"""Transfer regular-file changes between independent checkouts, never Git config."""

import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 1024
REGULAR = {"100644", "100755"}


def git(root, *args, data=None, env=None):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=data,
        stdout=subprocess.PIPE,
        env=env,
        check=True,
    ).stdout


def identity(root, manifest):
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Prepared updates require a clean checkout")
    branch = git(root, "symbolic-ref", "--short", "HEAD").decode().strip()
    origin = git(root, "remote", "get-url", "origin").decode().strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+)", origin
    )
    if not match:
        raise ValueError("Prepared updates require a GitHub origin without credentials")
    return {
        "repository": match[1].removesuffix(".git"),
        "branch": branch,
        "base": git(root, "rev-parse", "HEAD").decode().strip(),
        "manifest": str(Path(manifest).relative_to(root)),
    }


def check_path(name):
    if not isinstance(name, str) or any(
        part in {"", ".", ".."}
        or part.casefold().startswith((".git", "git~"))
        or "\\" in part
        or ":" in part
        or any(ord(char) < 32 for char in part)
        for part in name.split("/")
    ):
        raise ValueError(f"Unsupported prepared path: {name!r}")
    if name.casefold() == "flathub.json":
        raise ValueError("Prepared updates cannot change flathub.json")


def delta(root, base, env=None):
    raw = git(
        root,
        "diff",
        "--cached",
        "--raw",
        "--no-renames",
        "--no-abbrev",
        "-z",
        base,
        "--",
        env=env,
    )
    fields = iter(raw.split(b"\0")[:-1])
    result = {}
    for header, path in zip(fields, fields, strict=True):
        old, new, _, blob, _ = header.decode().removeprefix(":").split()
        name = path.decode()
        check_path(name)
        if not {old, new} <= REGULAR | {"000000"}:
            raise ValueError(f"Unsupported prepared file type: {name}")
        result[name] = (new, blob)
    return result


def write(root, destination, target, changes, details):
    git(root, "add", "-u", "--", ".")
    files = {}
    total = 0
    modifications = delta(root, target["base"])
    if len(modifications) > MAX_FILES:
        raise ValueError("Too many files in prepared update")
    for name, (mode, blob) in modifications.items():
        if mode == "000000":
            files[name] = None
            continue
        total += int(git(root, "cat-file", "-s", blob))
        if total > MAX_BYTES:
            raise ValueError("Prepared update is too large")
        content = base64.b64encode(git(root, "cat-file", "blob", blob)).decode()
        files[name] = [mode, content]
    if not files:
        raise ValueError("Post-update script left no changes")
    packet = json.dumps(
        {
            "version": 1,
            "target": target,
            "changes": changes,
            "details": details,
            "files": files,
        }
    ).encode()
    if len(packet) > MAX_BYTES:
        raise ValueError("Prepared update is too large")
    with destination.open("xb") as output:
        output.write(packet)


def load(root, source, target):
    with source.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Prepared update is too large")
    packet = json.loads(data)
    if (
        not isinstance(packet, dict)
        or packet.get("version") != 1
        or packet.get("target") != target
    ):
        raise ValueError("Prepared update does not match checkout, branch or base")
    changes = packet.get("changes")
    files = packet.get("files")
    if not (
        isinstance(changes, list)
        and changes
        and all(isinstance(item, str) and item for item in changes)
        and isinstance(packet.get("details"), str)
        and isinstance(files, dict)
        and 0 < len(files) <= MAX_FILES
    ):
        raise ValueError("Invalid prepared update")

    # Build a tree in a private index. Never check out artifact contents or
    # invoke clean/smudge filters, repository scripts or commit hooks.
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(root, "read-tree", target["base"], env=env)
        records = bytearray()
        for name, record in files.items():
            check_path(name)
            if record is None:
                mode, blob = "0", "0" * len(target["base"])
            else:
                if not (
                    isinstance(record, list)
                    and len(record) == 2
                    and isinstance(record[0], str)
                    and record[0] in REGULAR
                    and isinstance(record[1], str)
                ):
                    raise ValueError(f"Invalid prepared file: {name}")
                mode, content = record
                blob = (
                    git(
                        root,
                        "hash-object",
                        "-w",
                        "--stdin",
                        data=base64.b64decode(content, validate=True),
                    )
                    .decode()
                    .strip()
                )
            records.extend(f"{mode} {blob}\t{name}\0".encode())
        git(root, "update-index", "-z", "--index-info", data=bytes(records), env=env)
        if delta(root, target["base"], env).keys() != files.keys():
            raise ValueError("Prepared update produced unexpected tree changes")
        tree = git(root, "write-tree", env=env).decode().strip()
    return tree, changes, packet["details"]
