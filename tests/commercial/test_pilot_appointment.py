from unittest.mock import Mock, patch

import pytest

from businessbuilder.commercial.operator_authority import OPERATOR_ACTIONS
from businessbuilder.commercial.pilot_appointment import provision_pilot_appointment
from tests.commercial.test_operator_admission import setup


def test_pilot_appointment_requires_actual_founder_owner_and_support_membership():
    context, identity, commercial, _, principal, _, _ = setup()
    identity.close = Mock()
    commercial.close = Mock()
    environment = {
        "ENVIRONMENT": "pilot",
        "PILOT_FOUNDER_USER_ID": context.actor_user_id,
        "PILOT_OPERATOR_USER_ID": principal.operator_user_id,
        "PILOT_COMPANY_ID": context.company_id,
        "PILOT_COMMERCIAL_GRANT_ID": "pilot_actual_operator_grant",
        "CUSTOMER_API_PRINCIPAL_KEY": "test_only_customer_principal_signing_key_32bytes",
    }
    with (patch.dict("os.environ", environment),
          patch("businessbuilder.commercial.pilot_appointment.PostgresIdentityRepository", return_value=identity),
          patch("businessbuilder.commercial.pilot_appointment.PostgresCommercialRepository", return_value=commercial)):
        assert provision_pilot_appointment() == environment["PILOT_COMMERCIAL_GRANT_ID"]
        assert provision_pilot_appointment() == environment["PILOT_COMMERCIAL_GRANT_ID"]
        appointed = commercial.get_operator_grant(
            context.tenant_id, context.company_id, environment["PILOT_COMMERCIAL_GRANT_ID"]
        )
        assert appointed.operator_user_id == principal.operator_user_id
        assert appointed.actions == OPERATOR_ACTIONS
        assert appointed.provisioned_by == "pilot_aws_deployer"
        assert len([grant for grant in commercial.operator_grants.values()
                    if grant[0].grant_id == appointed.grant_id]) == 1
    with patch.dict("os.environ", {**environment, "ENVIRONMENT": "production"}):
        with pytest.raises(RuntimeError):
            provision_pilot_appointment()
    with (patch.dict("os.environ", {**environment, "PILOT_COMPANY_ID": "company_other"}),
          patch("businessbuilder.commercial.pilot_appointment.PostgresIdentityRepository", return_value=identity),
          patch("businessbuilder.commercial.pilot_appointment.PostgresCommercialRepository", return_value=commercial)):
        with pytest.raises(RuntimeError):
            provision_pilot_appointment()
