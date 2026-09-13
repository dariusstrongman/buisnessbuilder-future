import json
import re
import unittest
from pathlib import Path

from businessbuilder.company_brain import CompanyBrainService, SQLiteCompanyBrainRepository, load_billy_bob


ROOT = Path(__file__).resolve().parents[1]


class ContractCompatibilityTest(unittest.TestCase):
    def setUp(self):
        repo = SQLiteCompanyBrainRepository(); repo.migrate()
        self.service = CompanyBrainService(repo)
        self.scope = load_billy_bob(self.service)

    def test_company_export_has_all_required_contract_fields(self):
        contract = json.loads((ROOT / "contracts/company.schema.json").read_text())
        exported = self.service.get_company(self.scope).to_contract()
        self.assertEqual(exported["schema_version"], contract["properties"]["schema_version"]["const"])
        self.assertTrue(set(contract["required"]).issubset(exported))
        self.assertFalse(set(exported) - set(contract["properties"]))
        self.assertIn(exported["lifecycle"], contract["properties"]["lifecycle"]["enum"])
        self.assertIn(exported["archetype"], contract["properties"]["archetype"]["enum"])
        common = json.loads((ROOT / "contracts/common.schema.json").read_text())
        self.assertRegex(exported["company_id"], re.compile(common["$defs"]["id"]["pattern"]))
        self.assertRegex(exported["jurisdiction"]["country"], r"^[A-Z]{2}$")
        self.assertGreaterEqual(len(exported["provenance"]), 1)

    def test_company_brain_supports_every_required_product_entity(self):
        expected = {"party","goal","strategy","decision","offer","service","market","policy","capacity","asset",
                    "account","vendor","customer_ref","lead_ref","workflow","agent","approval_ref","verification_ref",
                    "evidence_ref","risk","obligation","metric","founder_action","audit_event","artifact_ref"}
        from businessbuilder.company_brain import RecordKind
        self.assertEqual(expected, {kind.value for kind in RecordKind})

    def test_shared_contracts_remain_unmodified_and_parse(self):
        expected = {"company.schema.json","event.schema.json","job.schema.json","capability.schema.json",
                    "verification.schema.json","approval.schema.json","artifact.schema.json","budget-spend.schema.json",
                    "founder-action.schema.json","audit-event.schema.json","website-capability.schema.json","common.schema.json"}
        found = {path.name for path in (ROOT / "contracts").glob("*.json")}
        self.assertEqual(expected, found)
        for name in found:
            self.assertEqual(json.loads((ROOT / "contracts" / name).read_text())["$schema"], "https://json-schema.org/draft/2020-12/schema")


if __name__ == "__main__":
    unittest.main()
