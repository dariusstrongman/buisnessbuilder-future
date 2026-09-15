"""Provision two synthetic Cognito pilot identities through real email verification.

The generated credentials are written directly to AWS Secrets Manager and are
never printed. mail.tm is used only as a disposable test inbox for this isolated
acceptance proof; no customer or personal addresses are used.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import string
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError


REGION = "us-east-1"
USER_POOL_ID = "us-east-1_Fp59rCMh2"
APP_CLIENT_ID = "5t30mb06io1c0erboms6s29s5i"
SECRET_NAME = "businessbuilder-pilot-auth-identities-v1"
MAIL_API = "https://api.mail.tm"
CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")


@dataclass(frozen=True)
class Mailbox:
    address: str
    password: str
    token: str


def _request(path: str, *, method: str = "GET", body=None, token: str | None = None):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(MAIL_API + path, method=method, headers=headers, data=data)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        value = "Bb1!" + "".join(secrets.choice(alphabet) for _ in range(24))
        if any(char.isupper() for char in value) and any(char.islower() for char in value):
            return value


def _mailbox(label: str) -> Mailbox:
    response = _request("/domains?page=1")
    domains = response if isinstance(response, list) else response.get("hydra:member", [])
    usable = [item["domain"] for item in domains if item.get("isActive") and not item.get("isPrivate")]
    if not usable:
        raise RuntimeError("no disposable test inbox domain available")
    address = f"bb-{label}-{secrets.token_hex(6)}@{usable[0]}"
    password = _password()
    _request("/accounts", method="POST", body={"address": address, "password": password})
    token = _request("/token", method="POST", body={"address": address, "password": password})["token"]
    return Mailbox(address, password, token)


def _verification_code(
    mailbox: Mailbox, *, timeout_seconds: int = 180, ignore_ids: set[str] | None = None
) -> str:
    deadline = time.monotonic() + timeout_seconds
    seen: set[str] = set(ignore_ids or ())
    while time.monotonic() < deadline:
        listing = _request("/messages?page=1", token=mailbox.token)
        messages = listing if isinstance(listing, list) else listing.get("hydra:member", [])
        for message in messages:
            message_id = message.get("id")
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            detail = _request(f"/messages/{message_id}", token=mailbox.token)
            content = " ".join(
                str(value) for value in (detail.get("subject"), detail.get("text"), detail.get("html"))
            )
            matched = CODE_PATTERN.search(content)
            if matched:
                return matched.group(1)
        time.sleep(3)
    raise TimeoutError("Cognito verification message was not received")


def _existing(secret_client):
    try:
        response = secret_client.get_secret_value(SecretId=SECRET_NAME)
    except secret_client.exceptions.ResourceNotFoundException:
        return None
    return json.loads(response["SecretString"])


def _message_ids(mailbox: Mailbox) -> set[str]:
    listing = _request("/messages?page=1", token=mailbox.token)
    messages = listing if isinstance(listing, list) else listing.get("hydra:member", [])
    return {str(item["id"]) for item in messages if item.get("id")}


def _store(secret_client, identities: dict[str, dict[str, str]]) -> str:
    body = json.dumps(identities, separators=(",", ":"))
    try:
        response = secret_client.create_secret(
            Name=SECRET_NAME,
            Description="Synthetic founder/operator credentials for isolated Cognito pilot acceptance",
            SecretString=body,
            Tags=[
                {"Key": "Application", "Value": "businessbuilder"},
                {"Key": "Environment", "Value": "pilot"},
                {"Key": "ManagedBy", "Value": "Codex"},
                {"Key": "Purpose", "Value": "founder-auth-acceptance-v1"},
            ],
        )
    except secret_client.exceptions.ResourceExistsException:
        response = secret_client.put_secret_value(SecretId=SECRET_NAME, SecretString=body)
        return secret_client.describe_secret(SecretId=SECRET_NAME)["ARN"]
    return response["ARN"]


def _recover(role: str) -> None:
    cognito = boto3.client("cognito-idp", region_name=REGION)
    secret_client = boto3.client("secretsmanager", region_name=REGION)
    identities = _existing(secret_client)
    if identities is None or role not in identities:
        raise RuntimeError("requested synthetic pilot identity is not provisioned")
    identity = identities[role]
    mailbox = Mailbox(
        identity["email"],
        identity["mailbox_password"],
        _request(
            "/token",
            method="POST",
            body={"address": identity["email"], "password": identity["mailbox_password"]},
        )["token"],
    )
    existing_messages = _message_ids(mailbox)
    cognito.forgot_password(ClientId=APP_CLIENT_ID, Username=identity["email"])
    code = _verification_code(mailbox, ignore_ids=existing_messages)
    replacement = _password()
    cognito.confirm_forgot_password(
        ClientId=APP_CLIENT_ID,
        Username=identity["email"],
        ConfirmationCode=code,
        Password=replacement,
    )
    identity["password"] = replacement
    _store(secret_client, identities)
    authenticated = cognito.initiate_auth(
        ClientId=APP_CLIENT_ID,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": identity["email"], "PASSWORD": replacement},
    ).get("AuthenticationResult")
    print(json.dumps({"role": role, "recovery": "passed", "authenticated": bool(authenticated)}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recover", choices=("founder", "operator"))
    args = parser.parse_args()
    if args.recover:
        _recover(args.recover)
        return
    cognito = boto3.client("cognito-idp", region_name=REGION)
    secret_client = boto3.client("secretsmanager", region_name=REGION)
    identities = _existing(secret_client)
    if identities is None:
        identities = {}
        for role in ("founder", "operator"):
            mailbox = _mailbox(role)
            password = _password()
            try:
                cognito.sign_up(
                    ClientId=APP_CLIENT_ID,
                    Username=mailbox.address,
                    Password=password,
                    UserAttributes=[
                        {"Name": "email", "Value": mailbox.address},
                        {"Name": "name", "Value": f"Pilot {role.title()}"},
                    ],
                )
            except ClientError as error:
                raise RuntimeError(f"Cognito signup failed for synthetic {role}") from error
            code = _verification_code(mailbox)
            cognito.confirm_sign_up(ClientId=APP_CLIENT_ID, Username=mailbox.address, ConfirmationCode=code)
            identities[role] = {
                "email": mailbox.address,
                "password": password,
                "mailbox_password": mailbox.password,
            }
        arn = _store(secret_client, identities)
    else:
        arn = secret_client.describe_secret(SecretId=SECRET_NAME)["ARN"]

    results = {}
    for role, identity in identities.items():
        user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=identity["email"])
        attributes = {item["Name"]: item["Value"] for item in user.get("UserAttributes", [])}
        results[role] = {
            "user_status": user["UserStatus"],
            "enabled": user["Enabled"],
            "email_verified": attributes.get("email_verified") == "true",
        }
    print(json.dumps({"secret_arn": arn, "identities": results}, sort_keys=True))


if __name__ == "__main__":
    main()
