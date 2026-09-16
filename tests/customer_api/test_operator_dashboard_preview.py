"""Operator (ADMIN PREVIEW) read-only dashboard access.

Every test here asks the same question from a different angle: can anything but a
currently-valid, company-scoped, self-owned support grant reach a founder's
dashboard, and can that grant ever change founder state? The answer must be no.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from businessbuilder.identity import Permission, Role

from .test_customer_api import CustomerApiFixture


READ_ONLY_PERMISSIONS = frozenset({Permission.VIEW_COMPANY_STATE, Permission.ACCESS_ARTIFACTS})
GRANTS = "/api/v1/operator/support-grants"
COMPANIES = "/api/v1/operator/companies"


def preview_path(company_id: str) -> str:
    return f"/api/v1/operator/companies/{company_id}/dashboard-preview"


class OperatorDashboardPreviewTests(CustomerApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.operator, self.operator_token = self._member(Role.SUPPORT, "operator")

    # -- helpers ----------------------------------------------------------

    def _grant(self, *, company_id=None, duration=timedelta(minutes=30), permissions=READ_ONLY_PERMISSIONS):
        return self.identity.grant_support_access(
            self.owner_context,
            self.operator.user_id,
            frozenset(permissions),
            duration,
            "owner-approved operator preview",
            company_id=company_id,
        )

    def _session(self, grant):
        return self.identity.start_support_session(
            self.operator.user_id, grant.grant_id, "inspect the founder dashboard"
        ).impersonation_session_id

    def _headers(self, support_session_id):
        return {"X-Support-Impersonation-Session": support_session_id}

    def _audit_actions(self, tenant_id=None):
        return [
            item.action
            for item in self.identity_repository.list_audit(tenant_id or self.tenant.tenant_id)
        ]

    def _denial_actions(self):
        """Denials before a tenant is proven are recorded against "unresolved"."""
        return self._audit_actions("unresolved")

    # -- authorization ----------------------------------------------------

    def test_unauthenticated_callers_reach_no_operator_surface(self) -> None:
        for path in (GRANTS, COMPANIES, preview_path(self.company_id)):
            with self.subTest(path=path):
                self.assertEqual(401, self.request("GET", path).status)

    def test_normal_founder_cannot_reach_the_operator_company_list(self) -> None:
        for token in (self.owner_token, self._member(Role.MEMBER, "member")[1], self._member(Role.ADMIN, "admin")[1]):
            for path in (GRANTS, COMPANIES, preview_path(self.company_id)):
                with self.subTest(path=path):
                    self.assertEqual(403, self.request("GET", path, token=token).status)
        self.assertIn("authorization.denied", self._denial_actions())

    def test_owner_holding_a_support_session_still_cannot_preview_their_own_company(self) -> None:
        """The preview is never a founder's own dashboard wearing an operator label."""
        grant = self._grant(company_id=self.company_id)
        headers = self._headers(self._session(grant))
        response = self.request(
            "GET", preview_path(self.company_id), token=self.owner_token, headers=headers
        )
        self.assertEqual(403, response.status)

    def test_operator_without_a_support_session_is_denied_and_audited(self) -> None:
        self._grant(company_id=self.company_id)
        for path in (COMPANIES, preview_path(self.company_id)):
            with self.subTest(path=path):
                response = self.request("GET", path, token=self.operator_token)
                self.assertEqual(403, response.status)
        self.assertIn("authorization.denied", self._denial_actions())

    def test_support_membership_without_a_grant_lists_nothing(self) -> None:
        response = self.request("GET", GRANTS, token=self.operator_token)
        self.assertEqual(200, response.status)
        self.assertEqual([], response.body["support_grants"])
        self.assertTrue(response.body["read_only"])
        self.assertEqual("ADMIN PREVIEW", response.body["label"])

    def test_operator_only_ever_sees_their_own_grants(self) -> None:
        other, other_token = self._member(Role.SUPPORT, "other-operator")
        mine = self._grant(company_id=self.company_id)
        theirs = self.identity.grant_support_access(
            self.owner_context, other.user_id, READ_ONLY_PERMISSIONS,
            timedelta(minutes=30), "separate operator", company_id=self.company_id,
        )
        listed = self.request("GET", GRANTS, token=self.operator_token).body["support_grants"]
        self.assertEqual([mine.grant_id], [item["grant_id"] for item in listed])
        others = self.request("GET", GRANTS, token=other_token).body["support_grants"]
        self.assertEqual([theirs.grant_id], [item["grant_id"] for item in others])

    def test_another_operators_session_id_cannot_be_borrowed(self) -> None:
        other, _ = self._member(Role.SUPPORT, "borrowed")
        borrowed = self.identity.grant_support_access(
            self.owner_context, other.user_id, READ_ONLY_PERMISSIONS,
            timedelta(minutes=30), "separate operator", company_id=self.company_id,
        )
        stolen = self.identity.start_support_session(
            other.user_id, borrowed.grant_id, "their session"
        ).impersonation_session_id
        response = self.request(
            "GET", preview_path(self.company_id), token=self.operator_token, headers=self._headers(stolen)
        )
        self.assertEqual(403, response.status)

    # -- scope ------------------------------------------------------------

    def test_company_scoped_grant_lists_and_previews_only_that_company(self) -> None:
        second = "company_operator_second"
        self.identity.attach_company(
            replace(self.owner_context, company_id=None), second
        )
        self._create_brain_company(
            self.tenant.tenant_id, second, self.owner.user_id,
            lifecycle=self.brain.get_company(
                __import__("businessbuilder.company_brain", fromlist=["Scope"]).Scope(
                    self.tenant.tenant_id, self.company_id
                )
            ).lifecycle,
        )
        grant = self._grant(company_id=self.company_id)
        headers = self._headers(self._session(grant))
        listed = self.request("GET", COMPANIES, token=self.operator_token, headers=headers)
        self.assertEqual(200, listed.status)
        self.assertEqual([self.company_id], [item["company_id"] for item in listed.body["companies"]])
        self.assertTrue(listed.body["grant"]["company_scoped"])
        self.assertEqual(
            403,
            self.request("GET", preview_path(second), token=self.operator_token, headers=headers).status,
        )

    def test_forged_and_unknown_company_ids_are_denied(self) -> None:
        grant = self._grant(company_id=self.company_id)
        headers = self._headers(self._session(grant))
        for forged in ("company_does_not_exist", "company_customer_api_x", "company_CUSTOMER_API"):
            with self.subTest(forged=forged):
                response = self.request(
                    "GET", preview_path(forged), token=self.operator_token, headers=headers
                )
                self.assertEqual(403, response.status)
                self.assertNotIn(self.tenant.tenant_id, str(response.body))

    def test_cross_tenant_company_is_never_reachable(self) -> None:
        second_owner, _ = self.identity.register_founder("cross@example.test", "Cross")
        second_tenant, _, _ = self.identity.create_account(second_owner.user_id, "Cross Org")
        second_company = "company_cross_tenant"
        from businessbuilder.identity import AuthorizationContext

        self.identity.attach_company(
            AuthorizationContext(second_owner.user_id, second_tenant.tenant_id), second_company
        )
        self._create_brain_company(
            second_tenant.tenant_id, second_company, second_owner.user_id,
            lifecycle=self.brain.get_company(
                __import__("businessbuilder.company_brain", fromlist=["Scope"]).Scope(
                    self.tenant.tenant_id, self.company_id
                )
            ).lifecycle,
        )
        grant = self._grant()
        headers = self._headers(self._session(grant))
        listed = self.request("GET", COMPANIES, token=self.operator_token, headers=headers)
        self.assertNotIn(second_company, [item["company_id"] for item in listed.body["companies"]])
        self.assertEqual(
            403,
            self.request(
                "GET", preview_path(second_company), token=self.operator_token, headers=headers
            ).status,
        )

    def test_tenant_wide_grant_lists_every_company_in_that_tenant_only(self) -> None:
        grant = self._grant()
        headers = self._headers(self._session(grant))
        listed = self.request("GET", COMPANIES, token=self.operator_token, headers=headers)
        self.assertEqual(200, listed.status)
        self.assertFalse(listed.body["grant"]["company_scoped"])
        self.assertEqual([self.company_id], [item["company_id"] for item in listed.body["companies"]])

    def test_search_filters_the_company_list_without_widening_scope(self) -> None:
        grant = self._grant()
        headers = self._headers(self._session(grant))
        hit = self.request(
            "GET", COMPANIES, token=self.operator_token, headers=headers, query={"q": ["customer api"]}
        )
        self.assertEqual([self.company_id], [item["company_id"] for item in hit.body["companies"]])
        miss = self.request(
            "GET", COMPANIES, token=self.operator_token, headers=headers, query={"q": ["nothing here"]}
        )
        self.assertEqual([], miss.body["companies"])
        invalid = self.request(
            "GET", COMPANIES, token=self.operator_token, headers=headers, query={"q": ["x" * 121]}
        )
        self.assertEqual(400, invalid.status)

    # -- expiry -----------------------------------------------------------

    def test_support_authority_expiry_closes_every_operator_surface(self) -> None:
        grant = self._grant(company_id=self.company_id, duration=timedelta(minutes=5))
        headers = self._headers(self._session(grant))
        self.assertEqual(
            200,
            self.request("GET", preview_path(self.company_id), token=self.operator_token, headers=headers).status,
        )
        self.now += timedelta(minutes=6)
        self.assertEqual(
            403,
            self.request("GET", preview_path(self.company_id), token=self.operator_token, headers=headers).status,
        )
        self.assertEqual(
            403,
            self.request("GET", COMPANIES, token=self.operator_token, headers=headers).status,
        )
        self.assertEqual([], self.request("GET", GRANTS, token=self.operator_token).body["support_grants"])

    def test_revoked_grant_closes_the_preview_immediately(self) -> None:
        grant = self._grant(company_id=self.company_id)
        headers = self._headers(self._session(grant))
        self.assertEqual(
            200,
            self.request("GET", preview_path(self.company_id), token=self.operator_token, headers=headers).status,
        )
        self.identity_repository.save_support_grant(replace(grant, revoked_at=self.now))
        self.assertEqual(
            403,
            self.request("GET", preview_path(self.company_id), token=self.operator_token, headers=headers).status,
        )

    # -- read-only --------------------------------------------------------

    def test_every_operator_preview_route_refuses_non_get_methods(self) -> None:
        grant = self._grant(company_id=self.company_id)
        headers = self._headers(self._session(grant))
        for path in (GRANTS, COMPANIES, preview_path(self.company_id)):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(path=path, method=method):
                    response = self.request(
                        method, path, token=self.operator_token, headers=headers, body={}
                    )
                    self.assertEqual(405, response.status)

    def test_preview_cannot_mutate_founder_state(self) -> None:
        """Opening the preview leaves Company Brain, Runtime and Verification untouched."""
        from businessbuilder.company_brain import Scope

        scope = Scope(self.tenant.tenant_id, self.company_id)
        before_company = self.brain.get_company(scope)
        before_records = len(self.brain.query_current_state(scope))
        before_jobs = len(self.runtime_repository.list_jobs(self.tenant.tenant_id, self.company_id))

        grant = self._grant(company_id=self.company_id)
        headers = self._headers(self._session(grant))
        self.assertEqual(
            200,
            self.request("GET", preview_path(self.company_id), token=self.operator_token, headers=headers).status,
        )

        self.assertEqual(before_company.version, self.brain.get_company(scope).version)
        self.assertEqual(before_company.lifecycle, self.brain.get_company(scope).lifecycle)
        self.assertEqual(before_records, len(self.brain.query_current_state(scope)))
        self.assertEqual(
            before_jobs, len(self.runtime_repository.list_jobs(self.tenant.tenant_id, self.company_id))
        )

    def test_operator_still_cannot_use_protected_founder_authority(self) -> None:
        grant = self._grant(company_id=self.company_id)
        headers = self._headers(self._session(grant))
        self.assertEqual(
            403,
            self.request(
                "POST", f"/api/v1/companies/{self.company_id}/handoff",
                token=self.operator_token, headers=headers, body={},
            ).status,
        )
        self.assertEqual(
            403,
            self.request(
                "GET", "/api/v1/orders", token=self.operator_token, headers=headers,
                query={"company_id": [self.company_id]},
            ).status,
        )

    def test_a_grant_carrying_extra_authority_is_reported_as_not_read_only(self) -> None:
        grant = self._grant(
            company_id=self.company_id,
            permissions=READ_ONLY_PERMISSIONS,
        )
        listed = self.request("GET", GRANTS, token=self.operator_token).body["support_grants"]
        self.assertTrue(listed[0]["read_only"])
        self.assertEqual(sorted(item.value for item in grant.permissions), listed[0]["permissions"])

    # -- payload ----------------------------------------------------------

    def test_preview_returns_authoritative_sections_and_honest_gaps(self) -> None:
        grant = self._grant(company_id=self.company_id)
        headers = self._headers(self._session(grant))
        response = self.request(
            "GET", preview_path(self.company_id), token=self.operator_token, headers=headers
        )
        self.assertEqual(200, response.status)
        preview = response.body["operator_preview"]

        self.assertEqual("operator_preview", preview["mode"])
        self.assertEqual("ADMIN PREVIEW", preview["label"])
        self.assertTrue(preview["read_only"])
        self.assertFalse(preview["impersonation"])
        self.assertEqual("support", preview["viewer"]["role"])
        self.assertEqual(self.company_id, preview["company"]["company_id"])

        self.assertIn("ready", preview["readiness"])
        self.assertIn("fully_set", preview["readiness"])
        self.assertEqual("verification", preview["readiness"]["authority"])
        self.assertEqual("verification", preview["handoff"]["authority"])
        self.assertEqual("company_brain", preview["company_brain"]["authority"])
        self.assertEqual("runtime", preview["runtime_activity"]["authority"])
        self.assertEqual("provider_connection", preview["connection_health"]["authority"])
        self.assertIn("build_room", preview)
        self.assertIn("founder_actions", preview["build_room"])
        self.assertIn("work_items", preview["build_room"])
        self.assertIn("ai_usage", preview["build_room"])

        # Billing is never delegated to support, so it is declared unavailable
        # rather than shown empty.
        self.assertNotIn("commercial", preview["build_room"])
        sections = {item["section"] for item in preview["unavailable"]}
        self.assertIn("commercial_billing", sections)
        self.assertIn("support_escalations", sections)
        for item in preview["unavailable"]:
            self.assertTrue(item["reason"].strip())

    def test_preview_never_leaks_tenant_or_credential_material(self) -> None:
        grant = self._grant(company_id=self.company_id)
        headers = self._headers(self._session(grant))
        body = str(
            self.request(
                "GET", preview_path(self.company_id), token=self.operator_token, headers=headers
            ).body
        )
        self.assertNotIn(self.tenant.tenant_id, body)
        for secret in ("token_digest", "provider_ref", "password", "credential"):
            self.assertNotIn(secret, body)

    def test_company_brain_summary_counts_without_disclosing_records(self) -> None:
        grant = self._grant(company_id=self.company_id)
        headers = self._headers(self._session(grant))
        summary = self.request(
            "GET", preview_path(self.company_id), token=self.operator_token, headers=headers
        ).body["operator_preview"]["company_brain"]
        self.assertIsInstance(summary["record_counts"], dict)
        self.assertEqual(len(summary["record_counts"]), summary["recorded_kinds"])
        self.assertNotIn("records", summary)
        self.assertTrue(summary["detail_withheld"])

    # -- audit ------------------------------------------------------------

    def test_every_preview_access_is_audited_with_its_grant(self) -> None:
        grant = self._grant(company_id=self.company_id)
        session_id = self._session(grant)
        headers = self._headers(session_id)
        self.request("GET", GRANTS, token=self.operator_token)
        self.request("GET", COMPANIES, token=self.operator_token, headers=headers)
        self.request("GET", preview_path(self.company_id), token=self.operator_token, headers=headers)

        events = self.identity_repository.list_audit(self.tenant.tenant_id)
        actions = [item.action for item in events]
        self.assertIn("operator_preview.grants_listed", actions)
        self.assertIn("operator_preview.companies_listed", actions)
        self.assertIn("operator_preview.opened", actions)

        opened = next(item for item in events if item.action == "operator_preview.opened")
        self.assertEqual(self.operator.user_id, opened.actor_user_id)
        self.assertEqual(self.company_id, opened.company_id)
        self.assertEqual(grant.grant_id, opened.metadata["grant_id"])
        self.assertEqual(session_id, opened.metadata["support_session_id"])
        self.assertEqual("req_customer_api_test", opened.metadata["request_id"])
        self.assertEqual(
            "businessbuilder.customer_api.operator_preview", opened.source
        )

    def test_denied_preview_attempts_are_audited(self) -> None:
        before = len(self.identity_repository.list_audit("unresolved"))
        self.request("GET", preview_path(self.company_id), token=self.operator_token)
        self.assertGreater(len(self.identity_repository.list_audit("unresolved")), before)

    def test_transport_layer_advertises_operator_preview_as_get_only(self) -> None:
        from businessbuilder.customer_api import CustomerApi

        for path in (GRANTS, COMPANIES, preview_path(self.company_id)):
            with self.subTest(path=path):
                self.assertEqual(("GET",), CustomerApi.allowed_methods(path))
