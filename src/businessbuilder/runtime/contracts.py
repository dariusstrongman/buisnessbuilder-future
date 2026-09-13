from __future__ import annotations

import json
from pathlib import Path
import re
from datetime import datetime
from typing import Any


class ContractViolation(ValueError):
    pass


class ContractValidator:
    """Small Draft-2020-12 subset covering this repo's checked-in v1 contracts."""

    def __init__(self, contracts_dir: str | Path) -> None:
        self.root = Path(contracts_dir)
        self.schemas = {path.name: json.loads(path.read_text()) for path in self.root.glob("*.schema.json")}

    def validate(self, filename: str, value: Any) -> None:
        if filename not in self.schemas:
            raise ContractViolation(f"unknown contract {filename}")
        self._check(self.schemas[filename], value, "$", filename)

    def _resolve(self, ref: str, current: str) -> tuple[dict[str, Any], str]:
        filename, _, pointer = ref.partition("#")
        filename = filename or current
        schema: Any = self.schemas[filename]
        if pointer:
            for token in pointer.lstrip("/").split("/"):
                schema = schema[token.replace("~1", "/").replace("~0", "~")]
        return schema, filename

    def _check(self, schema: dict[str, Any], value: Any, path: str, current: str) -> None:
        if "$ref" in schema:
            resolved, filename = self._resolve(schema["$ref"], current)
            self._check(resolved, value, path, filename)
            return
        if "oneOf" in schema:
            successes = 0
            for option in schema["oneOf"]:
                try:
                    self._check(option, value, path, current)
                    successes += 1
                except ContractViolation:
                    pass
            if successes != 1:
                raise ContractViolation(f"{path}: expected exactly one matching schema")
            return
        if "const" in schema and value != schema["const"]:
            raise ContractViolation(f"{path}: expected {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            raise ContractViolation(f"{path}: {value!r} is not allowed")
        expected = schema.get("type")
        if expected:
            allowed = expected if isinstance(expected, list) else [expected]
            if not any(self._is_type(value, item) for item in allowed):
                raise ContractViolation(f"{path}: expected {allowed}, got {type(value).__name__}")
        if isinstance(value, dict):
            missing = set(schema.get("required", ())) - set(value)
            if missing:
                raise ContractViolation(f"{path}: missing {sorted(missing)}")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                extra = set(value) - set(properties)
                if extra:
                    raise ContractViolation(f"{path}: unexpected {sorted(extra)}")
            for key, item in value.items():
                if key in properties:
                    self._check(properties[key], item, f"{path}.{key}", current)
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0):
                raise ContractViolation(f"{path}: too few items")
            if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
                raise ContractViolation(f"{path}: duplicate items")
            if "items" in schema:
                for index, item in enumerate(value):
                    self._check(schema["items"], item, f"{path}[{index}]", current)
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**9):
                raise ContractViolation(f"{path}: invalid length")
            if "pattern" in schema and not re.match(schema["pattern"], value):
                raise ContractViolation(f"{path}: does not match {schema['pattern']}")
            if schema.get("format") == "date-time":
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ContractViolation(f"{path}: invalid date-time") from exc
                if parsed.tzinfo is None:
                    raise ContractViolation(f"{path}: date-time must include a timezone")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < schema.get("minimum", value) or value > schema.get("maximum", value):
                raise ContractViolation(f"{path}: outside numeric range")

    @staticmethod
    def _is_type(value: Any, expected: str) -> bool:
        return {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }[expected]
