"""Canonical JSON Schema validation with an explicitly offline registry."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin


def local_schema_errors(document: Any, schema_path: Path) -> list[str]:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
        from referencing.exceptions import NoSuchResource
        from referencing.jsonschema import DRAFT202012
    except ImportError as exc:
        return [f"canonical schema runtime unavailable (jsonschema/referencing required): {exc}"]

    def deny_retrieval(uri: str) -> Any:
        # No HTTP/file retrieval fallback. Only resources loaded below exist.
        raise NoSuchResource(ref=uri)

    try:
        schema_path = schema_path.resolve()
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema.setdefault("$id", schema_path.as_uri())
        Draft202012Validator.check_schema(schema)
        registry = Registry(retrieve=deny_retrieval)
        resources = []
        for path in sorted(schema_path.parent.glob("*.schema.json")):
            contents = json.loads(path.read_text(encoding="utf-8"))
            resource = Resource.from_contents(contents, default_specification=DRAFT202012)
            uris = {path.resolve().as_uri(), urljoin(schema["$id"], path.name)}
            if contents.get("$id"):
                uris.add(contents["$id"])
            resources.extend((uri, resource) for uri in sorted(uris))
        registry = registry.with_resources(resources)
        format_checker = FormatChecker()

        @format_checker.checks("date-time", raises=(ValueError, TypeError))
        def canonical_timestamp(value: Any) -> bool:
            # The offline dependency set may omit the optional RFC3339
            # package. Never let that silently disable date-time validation.
            if not isinstance(value, str):
                return True  # JSON Schema's type keyword handles non-strings.
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value) is None:
                return False
            if value[-1] not in "Zz":
                # datetime.fromisoformat normalizes offset minutes such as
                # +00:99 instead of enforcing RFC3339's numeric ranges.
                if int(value[-5:-3]) > 23 or int(value[-2:]) > 59:
                    return False
            return datetime.fromisoformat(value.replace("z", "+00:00").replace("Z", "+00:00")).tzinfo is not None

        validator = Draft202012Validator(schema, format_checker=format_checker, registry=registry)
        errors = sorted(validator.iter_errors(document), key=lambda item: tuple(map(str, item.absolute_path)))
        return [f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}" for error in errors]
    except Exception as exc:
        return [f"canonical local schema validation failed closed: {type(exc).__name__}: {exc}"]
