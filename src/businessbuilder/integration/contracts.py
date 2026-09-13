from __future__ import annotations

from typing import Any, Mapping


def tenant_v2_projection(v1: Mapping[str, Any], tenant_id: str) -> dict[str, Any]:
    """Add explicit tenant scope while preserving the shape and meaning of a v1 projection."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    schema_version = v1.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.endswith(".v1"):
        raise ValueError("a v1 contract projection is required")
    return {
        **dict(v1),
        "schema_version": f"{schema_version[:-3]}.v2",
        "tenant_id": tenant_id,
    }
