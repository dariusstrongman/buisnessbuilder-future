# Cognito Pilot Provisioning and Acceptance v1

This is an isolated, non-production authentication environment for one supervised residential-cleaning acceptance proof. It is not production deployment and it does not enable checkout, payment, filings, banking, insurance, permits, domains, merchant activation, customer email, or live customer data.

## Authority boundary

Cognito proves account identity and verified email only. Cognito groups and claims never grant Business Builder tenant, company, membership, role, support, founder, commercial, Runtime, Company Brain, or Verification authority. The backend maps the provider subject to one internal user and revalidates persisted membership and scope on every request.

The public website app client uses Managed Login authorization-code flow with S256 PKCE, state, nonce, and exact HTTPS callback/logout URLs. `aws.cognito.signin.user.admin` is requested because Cognito `GetUser` requires that self-service scope; it conveys no Business Builder authority.

## Pilot policy

- Password: 12 characters minimum with uppercase, lowercase, number, and symbol.
- Email: required, provider-verified, and reverified before email changes.
- MFA: optional software TOTP at the pool for this isolated supervised pilot. MFA must be made mandatory under an approved operator/founder enrollment plan before an unsupervised production launch.
- Passkeys: deferred. The pilot pool permits password first-factor only; passkey relying-party/domain and recovery policy have not been approved.
- Recovery: Cognito verified-email recovery; existence suppression is enabled.
- Sessions: 15-minute access and ID tokens, one-day refresh token, refresh-token rotation with five-second grace, and token revocation enabled. Separate devices receive separate refresh families. Logout revokes the current internal session and provider refresh family. The password-auth API flow was used only during isolated test diagnostics and is removed from the final public app client; browser access remains Hosted Login with authorization code + PKCE. A privileged operator can disable the Cognito account; automated Cognito-to-internal-user disable reconciliation remains a production blocker.
- Support: internal SUPPORT membership plus an owner-attributed, company-scoped, expiring grant is mandatory. Provider groups do not confer the role. Support cannot approve founder decisions, spend, alter billing, perform handoff, manage providers/communications, or bypass Verification.

## Resources

The non-secret resource inventory is in `infra/cognito-pilot/resource-manifest.json`. Runtime signing material and synthetic acceptance credentials exist only in AWS Secrets Manager. The app client has no client secret.

Environment variable names:

- Backend: `BUSINESS_BUILDER_AUTH_PROVIDER`, `AWS_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`, `CUSTOMER_API_PRINCIPAL_KEY`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_SCHEMA`, `DB_SSLMODE`, `DB_USER`, `DB_PASSWORD`.
- Website BFF: `BUSINESS_BUILDER_API_URL`, `BUSINESS_BUILDER_AUTH_PROVIDER`, `BUSINESS_BUILDER_AUTH_COOKIE_SIGNING_KEY`, `COGNITO_DOMAIN`, `COGNITO_APP_CLIENT_ID`, `COGNITO_REDIRECT_URI`, `COGNITO_LOGOUT_URI`, `NEXT_PUBLIC_SITE_URL`.
- Acceptance harness: `COGNITO_PILOT_SITE`, `COGNITO_PILOT_IDENTITY_SECRET`, `COGNITO_PILOT_COMPANY_ID`, `COGNITO_PILOT_SUPPORT_GRANT_ID`, `COGNITO_PILOT_EXPECT_SUPPORT_DENIED`.

No value belongs in source, logs, documentation, browser-readable JavaScript, or chat output.

## Acceptance evidence

- Two synthetic disposable test inboxes completed real Cognito signup email verification. [mail.tm](https://docs.mail.tm/) was used only for the synthetic inbox and is credited per its API terms.
- Founder: Managed Login, internal session establishment, existing organization/OWNER/company bootstrap, persisted residential-cleaning entry, repeat/idempotent intake, refresh rotation, logout, revoked refresh-family rejection, revoked internal-session rejection, login again, and provider-native recovery passed.
- Operator: Managed Login first produced zero customer authority. A persisted SUPPORT membership plus company-scoped eight-hour grant then permitted only the evidence-review path. A forged grant, an expired grant, and commercial access were denied.
- Real-provider failures: unverified email, disabled account login, user-existence probing, forged state, wrong nonce, expired state, forged tenant/company headers, cross-company object access, and revoked-session reuse failed closed.
- Verification remained authoritative: the functioning integration left both Ready and Fully Set false.
- Restart persistence: founder/company/journey state and support membership/grants survived task replacement and were reloaded from the isolated PostgreSQL schema.

Synthetic identity email addresses, passwords, verification/recovery codes, access tokens, refresh tokens, cookies, and signing keys are deliberately omitted.

## Operational caveats before production auth

1. Replace the disposable verification inbox path with an approved production email-delivery setup and branded sender.
2. Approve and enforce MFA policy, especially for operator identities; complete passkey decision separately.
3. Automate or formally runbook provider disable/delete reconciliation into internal user/session revocation. Current short internal-session lifetime bounds but does not eliminate that delay.
4. Replace out-of-band pilot support provisioning with an approved founder-facing grant workflow or audited operator process that updates running repository views safely.
5. Add WAF/rate controls, alarms, CloudTrail/CloudWatch retention, and account-level incident runbooks before public exposure.
6. Replace the default CloudFront hostname with an approved product domain/certificate only after DNS authorization.
7. Complete legal/privacy review for identity records and retention.

## Cost

At low pilot traffic, the incremental standing estimate is approximately USD 47–55/month: one 0.5-vCPU/1-GiB Fargate task, one ALB, public IPv4 addresses, two Secrets Manager secrets, and low-volume CloudFront/log usage. Two Cognito monthly active users are expected to remain inside the applicable free allowance. No NAT gateway or additional database was created. The existing private RDS instance is reused through a dedicated schema.
