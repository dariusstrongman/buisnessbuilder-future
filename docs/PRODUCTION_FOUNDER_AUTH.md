# Production Founder Authentication v1

Business Builder keeps authentication provider-neutral. The first production adapter is an Amazon Cognito User Pool. Cognito proves the external identity; the Identity repository remains authoritative for the internal user, membership, tenant, company, and role.

## Trust flow

1. The website BFF starts Cognito managed login with authorization-code flow, S256 PKCE, state, and nonce.
2. The BFF exchanges the code server-side and sends the access token only to `POST /api/v1/auth/sessions`.
3. The backend calls Cognito `GetUser`, validates pool issuer, app client, access-token use and expiry, requires the provider `sub`, a verified normalized email, and a provider session ID.
4. Only a digest of the provider subject/session is persisted. Cognito group, tenant, company, and role claims are ignored.
5. The backend creates or attaches one founder user/profile idempotently and returns a short-lived opaque internal session. Organization, OWNER membership, and residential-cleaning company are created/attached by the existing first-intake bootstrap, where the names and company scope are available.
6. Every API request revalidates current user, organization, membership, tenant, company, and any support grant through the existing trusted-principal authority.

Session rotation revokes the prior internal session before returning its replacement. Logout revokes both the internal session and provider refresh family where available. Deactivated users, removed memberships, suspended organizations, expired grants, and revoked sessions fail closed on the next request.

## Configuration names

Backend:

- `BUSINESS_BUILDER_AUTH_PROVIDER`
- `AWS_REGION`
- `COGNITO_USER_POOL_ID`
- `COGNITO_APP_CLIENT_ID`

Website BFF:

- `BUSINESS_BUILDER_API_URL`
- `BUSINESS_BUILDER_AUTH_PROVIDER`
- `BUSINESS_BUILDER_AUTH_COOKIE_SIGNING_KEY`
- `COGNITO_DOMAIN`
- `COGNITO_APP_CLIENT_ID`
- `COGNITO_REDIRECT_URI`
- `COGNITO_LOGOUT_URI`

Non-production harnesses only:

- `BUSINESS_BUILDER_AUTH_EMULATOR`
- `BUSINESS_BUILDER_TEST_AUTH_MODE`
- `BUSINESS_BUILDER_TEST_FOUNDER_EMAIL`
- `BUSINESS_BUILDER_TEST_FOUNDER_PROOF`
- `BUSINESS_BUILDER_TEST_FOUNDER_SESSION`
- `BUSINESS_BUILDER_TEST_OPERATOR_EMAIL`
- `BUSINESS_BUILDER_TEST_OPERATOR_PROOF`
- `BUSINESS_BUILDER_TEST_OPERATOR_SESSION`
- `BUSINESS_BUILDER_TEST_SUPPORT_SESSION`

No values belong in source, logs, job payloads, or documentation.

## Required Cognito control-plane setup before enablement

- create one dedicated production user pool and public app client;
- authorization-code grant only, with S256 PKCE and exact HTTPS callback/logout URLs;
- require and auto-verify email; configure provider-native signup, verification, and recovery messaging;
- enable token revocation and refresh-token rotation;
- enable user-existence-error suppression;
- set password/passkey/MFA and account recovery policy for the supervised pilot;
- configure security monitoring, delivery identities, quotas, retention, and operator runbooks;
- keep the test adapter and HTTP emulator disabled in production.

This branch does not create the user pool, credentials, DNS, email identities, or deployment.
