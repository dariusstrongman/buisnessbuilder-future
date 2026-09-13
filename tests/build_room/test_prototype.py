from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from businessbuilder.build_room.fixtures import billy_bob_build_room
from businessbuilder.build_room.serialization import render_data_js


ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = ROOT / "prototype"


class BuildRoomPrototypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (PROTOTYPE / "index.html").read_text(encoding="utf-8")
        cls.css = (PROTOTYPE / "styles.css").read_text(encoding="utf-8")
        cls.js = (PROTOTYPE / "app.js").read_text(encoding="utf-8")
        cls.data = (PROTOTYPE / "data.js").read_text(encoding="utf-8")

    def test_offline_assets_exist_and_javascript_parses(self) -> None:
        self.assertFalse(re.search(r'https?://', self.html + self.css + self.js + self.data))
        node = subprocess.run(["node", "--check", str(PROTOTYPE / "app.js")], capture_output=True, text=True, check=False)
        if node.returncode == 127:
            self.skipTest("node is unavailable")
        self.assertEqual(0, node.returncode, node.stderr)
        data = subprocess.run(["node", "--check", str(PROTOTYPE / "data.js")], capture_output=True, text=True, check=False)
        self.assertEqual(0, data.returncode, data.stderr)

    def test_accessible_structure_and_controls(self) -> None:
        for required in ('lang="en"', '<main id="main">', 'aria-label="Build Room sections"', 'role="progressbar"', '<dialog', '<h1'):
            self.assertIn(required, self.html)
        self.assertIn("prefers-reduced-motion", self.css)
        self.assertNotRegex(self.html, r'<img(?![^>]*\balt=)')
        self.assertNotRegex(self.html, r'<button(?![^>]*(?:aria-label|>[^<]+</button>))')

    def test_ui_cannot_become_execution_truth(self) -> None:
        combined = (self.html + self.js + self.data).lower()
        self.assertNotIn("localstorage", combined)
        self.assertNotIn("sessionstorage", combined)
        self.assertNotIn("fetch(", combined)
        self.assertNotIn("xmlhttprequest", combined)
        self.assertNotIn("stromation", combined)
        self.assertIn("read-only projection", combined)
        self.assertIn("this prototype cannot approve, spend, execute, deploy or mutate state", combined)

    def test_browser_fixture_exactly_matches_python_projection(self) -> None:
        self.assertEqual(render_data_js(billy_bob_build_room()), self.data)

    def test_status_and_approval_rendering_are_state_aware(self) -> None:
        self.assertIn('class="work-status"', self.js)
        self.assertIn("approvalIcon", self.js)
        self.assertIn("item.decided_at ?", self.js)
        self.assertIn("data.evidence.map", self.js)
        self.assertIn("renderBooleanState", self.js)
        self.assertIn('element.classList.remove("good", "blocked", "neutral")', self.js)
        self.assertIn('element.classList.add(value ? "good" : falseClass)', self.js)
        self.assertIn('element.setAttribute("aria-label"', self.js)

    def test_billy_bob_fixture_exposes_required_customer_concepts(self) -> None:
        for marker in (
            "dependency_ids", "cost", "approvals", "founder_actions", "evidence_refs",
            "blockers", "ready", "fully_set", "handoff", "package ready", "offline fake",
        ):
            self.assertIn(marker, self.data.lower())


if __name__ == "__main__":
    unittest.main()
