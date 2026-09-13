from __future__ import annotations

import json
from pathlib import Path
import unittest

from businessbuilder.fixtures.billy_bob import TENANT_ID, website_request
from businessbuilder.integration import tenant_v2_projection
from businessbuilder.runtime.contracts import ContractValidator
from businessbuilder.runtime.models import Event


ROOT = Path(__file__).resolve().parents[2]
TENANT_SCOPED = (
    "company",
    "event",
    "job",
    "verification",
    "approval",
    "audit-event",
    "budget-spend",
    "founder-action",
    "artifact",
    "website-capability",
)


class TenantContractV2Tests(unittest.TestCase):
    def test_all_material_company_boundaries_have_additive_tenant_v2(self) -> None:
        for name in TENANT_SCOPED:
            value = json.loads((ROOT / "contracts" / f"{name}.v2.schema.json").read_text())
            if name == "website-capability":
                for definition in value["$defs"].values():
                    self.assertIn("tenant_id", definition["required"])
                    self.assertIn("tenant_id", definition["properties"])
            else:
                self.assertIn("tenant_id", value["required"])
                self.assertIn("tenant_id", value["properties"])

    def test_v1_projection_is_unchanged_and_v2_is_additive(self) -> None:
        v1 = website_request()
        v2 = tenant_v2_projection(v1, TENANT_ID)
        self.assertNotIn("tenant_id", v1)
        self.assertEqual("website.capability.request.v1", v1["schema_version"])
        self.assertEqual(TENANT_ID, v2["tenant_id"])
        self.assertEqual("website.capability.request.v2", v2["schema_version"])
        ContractValidator(ROOT / "contracts").validate("website-capability.schema.json", v1)
        ContractValidator(ROOT / "contracts").validate("website-capability.v2.schema.json", v2)

    def test_runtime_event_v2_validates_with_explicit_tenant(self) -> None:
        from businessbuilder.fixtures.billy_bob import COMPANY_ID, CORRELATION_ID, FIXED_NOW

        event = Event(
            "event_contract_v2", TENANT_ID, COMPANY_ID, CORRELATION_ID, None,
            "company.created", FIXED_NOW, {"version": 1}, "integration-test",
        )
        v1 = event.to_contract()
        v2 = tenant_v2_projection(v1, TENANT_ID)
        ContractValidator(ROOT / "contracts").validate("event.schema.json", v1)
        ContractValidator(ROOT / "contracts").validate("event.v2.schema.json", v2)

    def test_global_capability_definition_remains_unscoped(self) -> None:
        self.assertFalse((ROOT / "contracts" / "capability.v2.schema.json").exists())


if __name__ == "__main__":
    unittest.main()
