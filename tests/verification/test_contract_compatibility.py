from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from verification.fixtures import proposed_record


ROOT = Path(__file__).resolve().parents[2]


def validate_subset(instance, schema, root_schema, common_schema, path="$"):
    if "$ref" in schema:
        reference = schema["$ref"]
        if reference.startswith("common.schema.json#/"):
            target = common_schema
            parts = reference.split("#/", 1)[1].split("/")
        elif reference.startswith("#/"):
            target = common_schema if reference.startswith("#/$defs/") else root_schema
            parts = reference[2:].split("/")
        else:
            raise AssertionError(f"unsupported test ref: {reference}")
        for part in parts:
            target = target[part]
        return validate_subset(instance, target, root_schema, common_schema, path)
    if "oneOf" in schema:
        errors = []
        for option in schema["oneOf"]:
            try:
                validate_subset(instance, option, root_schema, common_schema, path)
                return
            except AssertionError as exc:
                errors.append(str(exc))
        raise AssertionError(f"{path} does not match oneOf: {errors}")
    if "const" in schema:
        assert instance == schema["const"], f"{path} != const"
    if "enum" in schema:
        assert instance in schema["enum"], f"{path} not in enum"
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if instance is None and "null" in expected_type:
            return
        expected_type = next(item for item in expected_type if item != "null")
    if expected_type == "object":
        assert isinstance(instance, dict), f"{path} is not object"
        for required in schema.get("required", []):
            assert required in instance, f"{path}.{required} missing"
        if schema.get("additionalProperties") is False:
            extras = set(instance) - set(schema.get("properties", {}))
            assert not extras, f"{path} extra properties: {extras}"
        for key, value in instance.items():
            if key in schema.get("properties", {}):
                validate_subset(value, schema["properties"][key], root_schema, common_schema, f"{path}.{key}")
    elif expected_type == "array":
        assert isinstance(instance, list), f"{path} is not array"
        if schema.get("uniqueItems"):
            normalized = [json.dumps(item, sort_keys=True) for item in instance]
            assert len(normalized) == len(set(normalized)), f"{path} not unique"
        for index, value in enumerate(instance):
            validate_subset(value, schema.get("items", {}), root_schema, common_schema, f"{path}[{index}]")
    elif expected_type == "string":
        assert isinstance(instance, str), f"{path} is not string"
        if "minLength" in schema:
            assert len(instance) >= schema["minLength"], f"{path} too short"
        if "maxLength" in schema:
            assert len(instance) <= schema["maxLength"], f"{path} too long"
        if "pattern" in schema:
            assert re.search(schema["pattern"], instance), f"{path} pattern mismatch"
    elif expected_type == "integer":
        assert isinstance(instance, int) and not isinstance(instance, bool), f"{path} is not integer"
        if "minimum" in schema:
            assert instance >= schema["minimum"], f"{path} below minimum"
    elif expected_type == "null":
        assert instance is None, f"{path} is not null"


class ContractCompatibilityTests(unittest.TestCase):
    def test_domain_record_projects_to_verification_v1(self) -> None:
        schema = json.loads((ROOT / "contracts" / "verification.schema.json").read_text(encoding="utf-8"))
        common = json.loads((ROOT / "contracts" / "common.schema.json").read_text(encoding="utf-8"))
        contract = proposed_record("verification_contract", "website.links").to_contract()
        validate_subset(contract, schema, schema, common)

    def test_registry_contains_all_required_initial_definitions(self) -> None:
        from verification.catalog import default_registry

        required = {
            "website.deployed", "website.https", "website.forms", "website.mobile", "website.links",
            "domain.ownership", "email.inbound", "email.outbound", "crm.lead_capture",
            "scheduling.booking", "payment.test", "founder.action_complete",
        }
        self.assertTrue(required.issubset({item.definition_id for item in default_registry().all()}))


if __name__ == "__main__":
    unittest.main()
