from __future__ import annotations

from businessbuilder.identity import AuthenticatedPrincipal, PrincipalContextAuthority, Role
from businessbuilder.runtime.ports import VerifiedApprovalActor


class IdentityApprovalPrincipalVerifier:
    """Maps a signed principal to Runtime roles using current identity records."""

    def __init__(self, authority: PrincipalContextAuthority) -> None:
        self.authority = authority

    def verify_approval(
        self,
        principal: object,
        *,
        tenant_id: str,
        company_id: str,
        required_role: str,
        at,
    ) -> VerifiedApprovalActor:
        verified = self.authority.verify(
            principal, tenant_id=tenant_id, company_id=company_id
        )
        if required_role == "founder":
            self._require_founder(verified)
            runtime_role = "founder"
        elif required_role == "authorized_human":
            if verified.role not in {Role.OWNER, Role.ADMIN}:
                raise PermissionError("runtime approval requires owner or admin membership")
            runtime_role = "authorized_human"
        else:
            raise PermissionError("unsupported runtime approval role")
        return VerifiedApprovalActor(verified.user_id, runtime_role)

    def _require_founder(self, principal: AuthenticatedPrincipal) -> None:
        repository = self.authority.repository
        organization = repository.get_organization(principal.organization_id)
        if principal.role is not Role.OWNER or organization.owner_user_id != principal.user_id:
            raise PermissionError("founder-only approval requires current owner")
        if repository.get_founder_profile_for_user(principal.user_id) is None:
            raise PermissionError("founder-only approval requires founder identity")
