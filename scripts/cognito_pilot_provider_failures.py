"""Exercise fail-closed behavior against the real pilot Cognito pool.

Only synthetic identities are used. Passwords and tokens remain in memory and
are never emitted. The temporary unverified identity is removed in ``finally``.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from businessbuilder.identity import InMemoryIdentityRepository
from businessbuilder.identity.exceptions import AuthorizationDenied
from businessbuilder.identity.production_auth import (
    CognitoAuthenticationAdapter,
    ProductionFounderSessionService,
)


REGION = "us-east-1"
USER_POOL_ID = "us-east-1_Fp59rCMh2"
APP_CLIENT_ID = "5t30mb06io1c0erboms6s29s5i"


def _error_code(call) -> str:
    try:
        call()
    except ClientError as error:
        return str(error.response.get("Error", {}).get("Code", "ClientError"))
    return "SUCCESS"


def main() -> None:
    cognito = boto3.client("cognito-idp", region_name=REGION)
    secret_client = boto3.client("secretsmanager", region_name=REGION)
    identities = json.loads(
        secret_client.get_secret_value(
            SecretId="businessbuilder-pilot-auth-identities-v1"
        )["SecretString"]
    )
    founder = identities["founder"]
    operator = identities["operator"]
    wrong_password = "Wrong1!" + secrets.token_urlsafe(24)
    unknown = f"unknown-{secrets.token_hex(8)}@example.invalid"

    existing_error = _error_code(
        lambda: cognito.initiate_auth(
            ClientId=APP_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": founder["email"], "PASSWORD": wrong_password},
        )
    )
    unknown_error = _error_code(
        lambda: cognito.initiate_auth(
            ClientId=APP_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": unknown, "PASSWORD": wrong_password},
        )
    )
    if existing_error != unknown_error or existing_error == "SUCCESS":
        raise AssertionError("user-existence suppression responses diverged")

    cognito.admin_disable_user(UserPoolId=USER_POOL_ID, Username=operator["email"])
    try:
        disabled_error = _error_code(
            lambda: cognito.initiate_auth(
                ClientId=APP_CLIENT_ID,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={
                    "USERNAME": operator["email"],
                    "PASSWORD": operator["password"],
                },
            )
        )
        if disabled_error == "SUCCESS":
            raise AssertionError("disabled Cognito account authenticated")
    finally:
        cognito.admin_enable_user(UserPoolId=USER_POOL_ID, Username=operator["email"])

    unverified_username = f"unverified-{secrets.token_hex(8)}@example.invalid"
    unverified_password = "Bb1!" + secrets.token_urlsafe(24)
    try:
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=unverified_username,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": unverified_username},
                {"Name": "email_verified", "Value": "false"},
                {"Name": "name", "Value": "Unverified Pilot Identity"},
            ],
        )
        cognito.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=unverified_username,
            Password=unverified_password,
            Permanent=True,
        )
        access_token = cognito.initiate_auth(
            ClientId=APP_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": unverified_username,
                "PASSWORD": unverified_password,
            },
        )["AuthenticationResult"]["AccessToken"]
        adapter = CognitoAuthenticationAdapter(
            cognito,
            region=REGION,
            user_pool_id=USER_POOL_ID,
            app_client_id=APP_CLIENT_ID,
        )
        service = ProductionFounderSessionService(
            InMemoryIdentityRepository(),
            adapter,
            id_factory=lambda prefix: f"{prefix}_{secrets.token_hex(8)}",
            clock=lambda: datetime.now(timezone.utc),
        )
        try:
            service.establish(access_token)
        except AuthorizationDenied:
            unverified_denied = True
        else:
            unverified_denied = False
        if not unverified_denied:
            raise AssertionError("unverified provider identity established an internal session")
    finally:
        cognito.admin_delete_user(
            UserPoolId=USER_POOL_ID, Username=unverified_username
        )

    print(
        json.dumps(
            {
                "user_existence_suppression": "passed",
                "disabled_account": "denied",
                "unverified_email": "denied",
                "temporary_identity_removed": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
