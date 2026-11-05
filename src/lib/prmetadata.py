"""Serialize source and release identities in generated pull requests."""

import json
from typing import Any

import jsonschema

from .updateidentity import canonical, entry_key

MARKER = "<!-- flatpak-external-data-checker:updates "
MAX_METADATA_SIZE = 40000
STRING_LIST = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
ARCH_RESTRICTIONS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"only-arches": STRING_LIST, "skip-arches": STRING_LIST},
}
SOURCE_SCHEMA = {
    "type": "object",
    "required": ["arches"],
    "additionalProperties": False,
    "oneOf": [
        {"required": ["id"], "maxProperties": 2},
        {
            "required": ["file", "modules", "type", "selector"],
            "not": {"required": ["id"]},
        },
    ],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "file": {"type": "string", "minLength": 1},
        "modules": {"type": "array", "items": {"type": "string"}},
        "type": {"enum": ["git", "archive", "file", "extra-data"]},
        "selector": {"type": "object", "minProperties": 1},
        "arches": {
            "type": "object",
            "additionalProperties": False,
            "required": ["checker", "source", "root", "modules"],
            "properties": {
                "checker": STRING_LIST,
                "source": ARCH_RESTRICTIONS,
                "root": ARCH_RESTRICTIONS,
                "modules": STRING_LIST,
            },
        },
    },
}
SCHEMA = {
    "type": "object",
    "required": ["schema", "repository", "base", "manifest", "updates"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": 1},
        "repository": {"type": "integer", "minimum": 1},
        "base": {"type": "string", "minLength": 1},
        "manifest": {"type": "string", "minLength": 1},
        "complete": {"type": "boolean"},
        "updates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source", "release", "evidence"],
                "additionalProperties": False,
                "properties": {
                    "source": SOURCE_SCHEMA,
                    "evidence": {"type": "object"},
                    "release": {
                        "type": "object",
                        "required": ["kind", "value"],
                        "additionalProperties": False,
                        "allOf": [
                            {
                                "if": {
                                    "properties": {"kind": {"enum": ["tag", "commit"]}}
                                },
                                "then": {"required": ["repository"]},
                            },
                            {
                                "if": {"properties": {"kind": {"const": "commit"}}},
                                "then": {"required": ["ref"]},
                            },
                        ],
                        "properties": {
                            "kind": {
                                "enum": ["version", "tag", "commit", "sha256", "sha512"]
                            },
                            "value": {"type": "string", "minLength": 1},
                            "repository": {"type": "string"},
                            "ref": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}
METADATA_VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


class MetadataError(Exception):
    """Invalid source or release metadata in a pull request."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate metadata key: {key}")
        result[key] = value
    return result


def parse_metadata(body: str) -> dict[str, Any] | None:
    if MARKER not in body:
        return None
    try:
        if body.count(MARKER) != 1:
            raise ValueError("Multiple metadata blocks")
        payload, closing, _ = body.split(MARKER, 1)[1].partition("-->")
        if not closing or len(payload) > MAX_METADATA_SIZE:
            raise ValueError("Missing terminator or oversized metadata")
        metadata = json.loads(payload, object_pairs_hook=unique_object)
        METADATA_VALIDATOR.validate(metadata)
        assert isinstance(metadata, dict)
        keys = [entry_key(e) for e in metadata["updates"]]
        sources = [canonical(e["source"]) for e in metadata["updates"]]
        if len(keys) != len(set(keys)) or len(sources) != len(set(sources)):
            raise ValueError("Duplicate update identities")
        return metadata
    except (ValueError, RecursionError, jsonschema.ValidationError) as err:
        raise MetadataError(f"Invalid update metadata: {err}") from err


def format_metadata(
    repository: int,
    base: str,
    manifest: str,
    entries: list[dict],
    *,
    complete: bool = True,
) -> str:
    payload = (
        canonical(
            {
                "schema": 1,
                "repository": repository,
                "base": base,
                "manifest": manifest,
                "updates": entries,
                "complete": complete,
            }
        )
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("-", "\\u002d")
    )
    result = MARKER + payload + " -->"
    parse_metadata(result)
    return result
