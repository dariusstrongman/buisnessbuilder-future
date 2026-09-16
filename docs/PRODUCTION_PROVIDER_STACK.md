# Production Provider Stack

Canonical production-integration plan for Business Builder's customer-facing provider Connections. Researched 2026-09-15 against official provider documentation and the repositories at `codex/founder-dashboard-connections-v1` (backend `74ee909`, flagship site `5d9e4b0`). This document is research and planning only. It enables no live integration, changes no production behaviour, and records no secret values. Per-provider detail (38 items per provider) lives in `PROVIDER_INTEGRATION_MATRIX.md`.

Authorities are unchanged: Company Brain is business truth, Runtime authorizes execution, Commercial owns Business Builder's own orders and entitlements, Identity derives scope, Verification alone decides Ready and Fully Set, and Build Room is the permanent Founder Dashboard as a projection only (`docs/FOUNDER_DASHBOARD_AND_CONNECTIONS.md`).

## 1. Executive recommendation

1. **The provider-neutral Connections spine is real and should not be rebuilt.** `ProviderConnection`, the OAuth and scoped-key ports, the exact-ARN Secrets Manager store, refresh leases, callback dedupe, the ten-state health model, plain-English messages and the job-time fail-closed gate all exist and are tested (517 backend tests). What does not exist is any real provider adapter: the three adapters in tree are process-local sandboxes.
2. **Four spine gaps block every real provider equally and must be closed first**, before any adapter: grants are only installed for `EMAIL`; secret types exist only for email and CRM capabilities; the signed provider-callback verifier has no HTTP route; reconciliation schedules are built but never persisted or dispatched; and connection Founder Actions cannot reach evidence review or Verification.
3. **Phase 1 (launch-critical) is narrower than the preferred stack**: private S3 evidence with GuardDuty scanning (a release gate), Stripe Connect in test mode (highest customer value, no external approval beyond the platform profile), Google Calendar on a dedicated business calendar (the checkpoint's stated next task and the foundation for correct scheduling), and Cloudflare DNS as a customer-owned scoped-token connection. Google Workspace mail splits by scope class: `gmail.send` is a *sensitive* scope (days of verification) while every read scope is *restricted* (verification plus an annual CASA assessment, weeks). Inbound read/classify ships in Phase 1 only if the restricted-scope review starts now; sending remains disabled regardless under the existing communications gates.
4. **Three preferred-stack choices need correction, not replacement.** GA4 stays, but Business Builder must not create GA accounts (the Provisioning API agreement forbids public-app use); it creates properties inside customer-owned accounts. Google Business Profile stays, but as a Founder Action-first channel: policy requires the owner to initiate verification, video verification has no API path, there is no sandbox, and a 30-day content-storage clause may forbid a review archive. Calendly stays as the month-one front door but Jobber should be planned as the vertical successor for cleaning because Calendly cannot set booking questions, pricing, crews, recurring jobs or service areas.
5. **Two commercial gates start now regardless of build order**: the Google Business Profile API access application (needs a 60-day-old verified profile, ~14-day review, no sandbox) and the HubSpot 25-install ceiling (Marketplace listing needs three real installs first). Both are on the critical path of later phases and neither can be shortened by engineering.
6. **Cost ownership is clean.** Customers pay every customer-owned provider. Business Builder pays AWS, its own Stripe platform account, model providers, and two provider program fees that scale with usage: Intuit's platform tier (free to 500,000 metered reads per month, then blocked) and, only if listing is ever wanted, HubSpot partner membership.

## 2. Final recommended v1 provider stack

| Capability | v1 provider | Decision | Phase | Notes |
|---|---|---|---|---|
| Domain / DNS | Cloudflare | Keep | 1 | Customer-owned account and zone; Business Builder holds a zone-scoped DNS token. See §16 for registrar caveats. |
| Business email | Google Workspace (Gmail API) | Keep, gated | 1 (read) / 2 (send) | Read scopes are restricted (OAuth verification plus CASA); `gmail.send` is sensitive only; dedicated mailbox model. |
| Calendar | Google Calendar | Keep | 1 | Dedicated `bookings@` secondary calendar, least-privilege scopes, watch channels. |
| CRM | HubSpot | Keep | 2 | Public OAuth app; 25-install cap until Marketplace listing. |
| Scheduling | Calendly, then Jobber | Keep with successor | 1 (Calendly) / 3 (Jobber) | Calendly Standard+ needed for webhooks. |
| Payments | Stripe Connect (full-dashboard controller defaults) | Keep | 1 test-mode, live gated | Separate from Business Builder billing. |
| Analytics | GA4 | Keep, corrected | 2 | Properties in customer-owned accounts; service-account access binding preferred. |
| Search | Google Search Console | Keep | 2 | New `SEARCH` capability; domain property via Cloudflare DNS TXT. |
| Bookkeeping | QuickBooks Online | Keep | 3 | Questionnaire-gated production keys; metered reads. |
| Local presence | Google Business Profile | Keep, corrected | 1 manual / 3 API | Owner-initiated verification; Manager via Organization. |
| Evidence storage | Private S3 | Keep | 1 | Single bucket, tenant prefixes, SSE-KMS, GOVERNANCE lock at ACCEPT. |
| Malware scanning | GuardDuty Malware Protection for S3 | Keep | 1 | Async tag/EventBridge result path; TBAC read gate. |

Phone, SMS and AI voice remain out of scope.

## 3. Detailed provider matrix

See `PROVIDER_INTEGRATION_MATRIX.md`. Summary of the decisive facts per provider:

| Provider | Auth | Approval gate | Webhooks | Quota visible | Difficulty |
|---|---|---|---|---|---|
| Cloudflare | Scoped API token (customer-issued) | None | Notifications only; no per-record webhook | Rate-limit headers only | 2 |
| Google Workspace | OAuth (restricted Gmail scopes) | OAuth verification + CASA (weeks) | Pub/Sub `users.watch` | Per-user/day quota units; no balance | 4 |
| Google Calendar | OAuth (sensitive scopes) | OAuth verification (days) | `events.watch` channels | Per-minute quotas; 403 signals | 2 |
| HubSpot | OAuth public app | 25 installs unlisted; listing needs 3 installs | App subscriptions; uninstall via journal | None for OAuth apps | 4 |
| Calendly | OAuth 2.1 PKCE, rotating refresh | None | Org webhooks (Standard+), auto-disable after 24 h | Rate-limit headers on every call | 2 |
| Stripe Connect | Platform key + `Stripe-Account`; Account Links; OAuth for existing | Platform profile | Connect-scoped endpoint, `account.updated` | Balance API | 3 |
| GA4 | Service-account binding (preferred) or OAuth | None (SA) / verification (OAuth) | None | `returnPropertyQuota` | 2 |
| Search Console | OAuth + Site Verification | OAuth verification | None | None | 3 |
| QuickBooks | OAuth, rotating refresh | Questionnaire (weeks) | CloudEvents | None (metered reads) | 4 |
| Business Profile | OAuth `business.manage` on Org identity | API access application (~14 d) + OAuth verification | Pub/Sub | None | 5 |
| S3 | IAM | None | EventBridge | CloudWatch | 4 (design) |
| GuardDuty | IAM | None | EventBridge | CloudWatch | 2 |

## 4. Connection-health matrix

The durable states are `HEALTHY`, `WARNING`, `ACTION_REQUIRED`, `DISCONNECTED`, `AUTH_EXPIRED`, `PERMISSION_ERROR`, `RATE_LIMITED`, `BILLING_OR_CREDITS`, `PROVIDER_OUTAGE`, `UNKNOWN` (`provider_connection/models.py`). `normalize_health` already maps `invalid_grant`, `refresh_invalid_grant`, `refresh_token_revoked`, `token_revoked` → `AUTH_EXPIRED`; `scope_missing`, `permission_denied` → `PERMISSION_ERROR`; `rate_limit` → `RATE_LIMITED`; `quota_exceeded`, `insufficient_credits`, `billing_required` → `BILLING_OR_CREDITS`; `provider_unavailable`, `provider_5xx` → `PROVIDER_OUTAGE`; `customer_disconnect` → `DISCONNECTED`; `reconnect_required` → `ACTION_REQUIRED`; unknown codes → `ACTION_REQUIRED`; no error and not usable → `UNKNOWN`. Adapters must emit those normalized codes, never raw provider payloads. Detection is **proactive** when the reconcile schedule or a webhook produces the signal, **failure-driven** when only a job attempt reveals it.

| Provider | HEALTHY | WARNING | ACTION_REQUIRED | DISCONNECTED | AUTH_EXPIRED | PERMISSION_ERROR | RATE_LIMITED | BILLING_OR_CREDITS | PROVIDER_OUTAGE | UNKNOWN |
|---|---|---|---|---|---|---|---|---|---|---|
| Cloudflare | token verify `active`, zone `active`, NS delegated (proactive) | token expiry within 14 d; zone `pending` (proactive) | zone `moved`/deleted; expected record missing (proactive) | token `disabled`/deleted (proactive) | token `expired` (proactive) | 10000 on a scoped call (failure) | 429 (failure) | not exposed | 5xx (failure) | never reconciled |
| Google Workspace | `users.getProfile` ok, MX correct (proactive) | refresh token unused ~5 months; watch channel expiring (proactive) | admin blocked the app; mailbox suspended (proactive on reconcile) | admin removed app; token revoked (proactive) | `invalid_grant` (failure or reconcile) | scope missing; `insufficientPermissions` (failure) | 429/403 `userRateLimitExceeded` (failure) | not exposed (Workspace billing suspension surfaces as 403/`failedPrecondition`) | 5xx (failure) | never reconciled |
| Google Calendar | calendar reachable, ACL intact, channel active (proactive) | channel expiry within 24 h; sync token invalid (proactive) | calendar deleted or ACL downgraded (proactive) | token revoked (proactive) | `invalid_grant` | 403 `insufficientPermissions` | 403 `usageLimits`/429 | not exposed | 5xx | never reconciled |
| HubSpot | introspection `active`, scopes ⊇ required (proactive) | limits percentage > 80% (proactive) | app-not-approved 403; declined optional scope (proactive) | uninstall event in journal; refresh fails (proactive via journal poll) | `BAD_REFRESH_TOKEN`/`invalid_grant` | `MISSING_SCOPES` | 429 `TEN_SECONDLY_ROLLING`/`DAILY` | tier-limit hit (e.g. Free pipeline cap) classified `billing_required` (failure) | 5xx | never reconciled |
| Calendly | introspection `active`, webhook `state: active` (proactive daily) | `X-RateLimit-Remaining` < 15% (proactive on any call) | webhook `disabled` (auto-repair), not-admin 403 | token revoked (password/email change) | `invalid_grant` | `InsufficientScopeError` | 429 (`X-RateLimit-Reset`) | 403 "upgrade to Standard" (failure) | 5xx | never reconciled |
| Stripe Connect | `charges_enabled` and `payouts_enabled`, no `currently_due` (proactive via `account.updated`) | `future_requirements` or `under_review`; `current_deadline` within 14 d (proactive) | `requirements.past_due`, `rejected.*`, disabled capability (proactive) | `account.application.deauthorized`; `account_invalid` (proactive) | not applicable (no per-account token) | operator-only platform key errors | 429 with reason header (failure) | negative balance / payout failure (`payout.failed`) (proactive) | 5xx | never reconciled |
| GA4 | property/stream present, realtime hit seen (proactive) | quota remaining < 15%; no hits in 7 d (proactive) | binding removed (`PERMISSION_DENIED` on reconcile) | binding deleted | OAuth `invalid_grant` (OAuth mode only) | 403 | 429 `RESOURCE_EXHAUSTED` | not applicable (free) | 5xx (count toward hourly block) | never reconciled |
| Search Console | `permissionLevel` owner/full, sitemap ok (proactive) | sitemap errors > 0 | `siteUnverifiedUser` | owner removed | `invalid_grant` | 403 `insufficientPermissions` | 403/429 quota | not applicable | 5xx | never reconciled |
| QuickBooks | refresh ok, `SUBSCRIBED`, service item present (proactive) | refresh runway < 14 d; `TRIAL` (proactive) | 120 admin lost; 6200 closed period; no service item | 403 `ApplicationAuthorizationFailed`; 7001 | `invalid_grant` | 3100 | 429 (60 s) | `RESTRICTED`/`SUSPENDED`/`EXPIRED`/`CANCELLED` (6190) (proactive) | 140/150/5xx | never reconciled |
| Business Profile | `hasVoiceOfMerchant` true, Manager present (proactive + Pub/Sub) | `verify.hasPendingVerification`; pending Google updates | `BUSINESS_LOCATION_SUSPENDED`/`DISABLED`; invitation pending | Manager removed | `invalid_grant` | 403 (Workspace disabled GBP) | 429 | not applicable | 5xx | API access not yet approved (0 QPM) |
| S3 evidence (internal) | bucket checks pass | 4xx spike | objects without scan tag > 15 min | n/a | n/a | task-role denied | n/a | n/a | S3 outage | n/a |
| GuardDuty (internal) | `CompletedScanCount` tracks uploads | `FailedScanCount` > 0 | `Skipped{MissingPermissions}`; `Post Scan Action Failed` | plan deleted | n/a | role broken | `s3Throttled` | n/a | service event | plan status alone is never proof |

Founder-facing copy already exists per state (`provider_connection/connections.py:_MESSAGES`) and should stay generic; provider adapters add one provider-specific sentence and the restoring action (`reconnect`, `review_permissions`, `top_up`, `contact_provider`, which are the four `action_required` values the service accepts). Examples that meet the locked policy: "Google Calendar needs attention. Reconnect it to restore scheduling." "QuickBooks is read-only because of the QuickBooks subscription. Fix billing in QuickBooks." "Stripe needs one more document before payouts resume. Continue Stripe setup." Balance or credits warnings are emitted only from a measured value (Stripe balance, GA4 property quota, Calendly rate-limit headers, HubSpot limits API); Cloudflare, Google Workspace, Calendar, Search Console, QuickBooks and Business Profile expose no balance and must never show one.

## 5. Founder Action boundaries

Business Builder automates configuration and reads. Founders perform identity, consent, money and judgment steps. Providers and authorities decide verification, approval and suspension.

| Provider | Business Builder automates | Founder Action | Provider or authority |
|---|---|---|---|
| Cloudflare | DNS records, zone checks, TXT for verification, SSL mode | Create Cloudflare account, register or transfer the domain, issue the scoped token, unlock/transfer on handoff | Registry, ICANN 60-day locks |
| Google Workspace | MX/SPF/DKIM/DMARC record placement (via Cloudflare), profile checks, inbox read/classify, watch renewal | Buy Workspace, verify domain, create `ops@`/`support@`, authorize scopes, admin-allow the app | Google OAuth verification, CASA lab |
| Google Calendar | Create `bookings@` secondary calendar, ACLs, events, watch channels | Authorize scopes on the business account, confirm staff sharing | Google verification |
| HubSpot | Contacts, deals, properties, default pipeline stages, notes | Create account, install app, choose tier, connect inbox in HubSpot | Marketplace review |
| Calendly | Solo event types, org webhook, single-use links, cancellations | Create account, upgrade to Standard+, connect calendar inside Calendly, booking questions, payment collection | Support for lost keys |
| Stripe Connect | Account object, Account Links, products/prices/payment links, balance/payout reads | All KYC inside Stripe-hosted onboarding: entity, EIN/SSN, ID, bank, agreement acceptance | Stripe verification, review, rejection |
| GA4 | Property, stream, key events, retention, tag on hosted site, reports | Create GA account and accept ToS, add Business Builder identity, click user-data acknowledgement, install snippet on external CMS | none |
| Search Console | `sites.add`, verification via DNS/META, sitemap, reads, delegated-owner management | DNS TXT at a registrar we cannot reach; META on external CMS | Google indexing decisions |
| QuickBooks | Customers, invoices from approved template with founder-chosen item, send, reads, cache | Income account for any new item, chart of accounts, sales tax enablement and taxability, closed periods, deletions, subscription billing | Intuit questionnaire, AST rates, CPA |
| Business Profile | Hours, categories, service areas, photos, posts, review reads, founder-approved replies | Create profile, initiate and complete verification (video), invite Organization as Manager, approve each reply, appeal suspensions | Google VoM, verification, suspension |
| S3 / GuardDuty | Everything except release of infected objects | Upload, re-upload | Operator break-glass only |

## 6. Account ownership model

- **Customer-owned, customer-authorized**: Cloudflare account and zone, Workspace tenant and mailboxes, calendars, HubSpot portal, Calendly org, Stripe connected account (full dashboard), GA account, Search Console verified owner, QuickBooks company, Business Profile primary owner. Business Builder holds a token, a binding or a Manager/delegated role. `account_ownership` stays `unverified` until an adapter can attest a dedicated business identity (for example the connected email domain equals the business domain, or the Stripe account's business profile URL equals the business domain); `founder_owned_legacy` is recorded honestly when a personal account was used.
- **Business Builder-owned**: AWS (S3, KMS, GuardDuty, Secrets Manager), the Stripe platform account, model-provider accounts, the Cloudflare account used only for Business Builder's own hosting and Cloudflare for SaaS custom hostnames, the GBP Organization and the GA/GSC service-account pool.
- **Never**: Business Builder registering domains, buying Workspace seats, or creating Stripe/QBO/GA accounts in its own name for a customer. Where a provider offers "on behalf of" creation, the account object must still be customer-controlled (Stripe full-dashboard controller defaults are the model).

## 7. Billing and cost ownership model

| Cost | Bearer | Notes |
|---|---|---|
| Domain registration and renewal | Customer | At-cost at Cloudflare Registrar in the customer's account. |
| Google Workspace seats | Customer | Per user per month. |
| HubSpot, Calendly, QuickBooks plans | Customer | Free/Starter tiers are viable starting points except Calendly webhooks (Standard+). |
| Stripe processing fees, disputes | Customer | 2.9% + $0.30 domestic; $15 dispute. Business Builder takes no application fee in v1. |
| GA4, Search Console, Business Profile | Nobody | Free. |
| AWS S3, KMS, CloudTrail, GuardDuty | Business Builder | ≈$6–20/mo per 1,000 companies at 200 MB each; egress dominates. |
| Stripe Connect platform fees | Business Builder: $0 | Stripe-owned pricing model; buy-rate fees do not apply. |
| Intuit App Partner Program | Business Builder | $0 to 500,000 reads/mo (blocked beyond), $300/mo Silver. |
| HubSpot Solutions Partner | Business Builder, optional | $400/mo only to widen private distribution; not needed. |
| CASA assessment for Gmail restricted scopes | Business Builder | Annual, lab-priced; see §8. |
| Model providers (Bedrock etc.) | Business Builder | Existing Runtime budget model; customers never bring keys. |

Business Builder's own founder billing (Stripe Checkout, test mode today) remains a Commercial concern and is never shown as a customer `PAYMENTS` Connection.

## 8. Provider approval requirements

| Provider | Approval | Lead time | Start when |
|---|---|---|---|
| Google (Workspace mail) | OAuth brand + sensitive verification; **restricted Gmail scopes need CASA Tier 2** | Verification days; CASA weeks and annual | Immediately if inbound mail is in Phase 1 |
| Google (Calendar, GA4 OAuth, Search Console) | Sensitive-scope verification (3–5 business days typical; result lapses after 7 days if unpublished) | Days | Phase 1 kickoff |
| Google Business Profile | API access application + Organization registration | ~14 days, needs 60-day-old verified profile | Immediately |
| HubSpot | None to 25 installs; Marketplace listing after 3 real installs (10 bd first response, ≤60 d) | Weeks | After three customers use CRM |
| Calendly | None | 0 | Phase 1 |
| Stripe | Platform profile completion; live activation | Days (UNVERIFIED SLA) | Immediately |
| QuickBooks | App Assessment Questionnaire for production keys (unlisted is fine) | 1–3 weeks (UNVERIFIED) | Phase 2 prerequisites |
| Cloudflare | None | 0 | Phase 1 |
| AWS | None (GuardDuty independent enablement has a console-only first step) | 0 | Phase 1 |

## 9. OAuth and scope requirements

| Provider | Scopes or permissions (least privilege) |
|---|---|
| Cloudflare token | `Zone:DNS:Edit` + `Zone:Zone:Read` restricted to the customer's single zone; optional `Zone:Zone Settings:Read`; expiry set; no account-level permissions. |
| Google Workspace | Inbound: `gmail.readonly` or `gmail.metadata` (both restricted, CASA) plus `userinfo.email`; send later: `gmail.send` (sensitive, no CASA). Never `gmail.modify` or `mail.google.com`. Admin SDK `admin.directory.user` only if mailbox creation is automated (deferred). |
| Google Calendar | `calendar.app.created` (secondary calendars the app creates) plus `calendar.calendarlist.readonly` and `calendar.events.freebusy`; `calendar.events.owned` for an existing customer calendar; `calendar.acls` only during setup; never the full `calendar` scope. |
| HubSpot | `crm.objects.contacts.read/write`, `crm.objects.companies.read/write`, `crm.objects.deals.read/write`, `crm.schemas.deals.read/write`, `crm.objects.owners.read`; optional `crm.objects.tickets.*`. |
| Calendly | `users:read`, `event_types:read`, `scheduled_events:read`, `scheduling_links:write`, `webhooks:write`; `event_types:write` optional. |
| Stripe | OAuth `read_write` for existing accounts; Account Links otherwise; platform key with `Stripe-Account`. |
| GA4 | Service-account binding `predefinedRoles/editor` at setup then `analyst`; OAuth fallback `analytics.edit`, `analytics.readonly`. |
| Search Console | `webmasters`, `siteverification` (full, for owner management) or `webmasters.readonly` steady state. |
| QuickBooks | `com.intuit.quickbooks.accounting` only. |
| Business Profile | `business.manage` on the Organization identity. |

All Google clients must be published (not Testing) before any customer connects, or refresh tokens expire in seven days.

## 10. Secrets and credential strategy

Already enforced in code and kept: opaque `secretref_` IDs in PostgreSQL, values only in Secrets Manager under exact ARNs (`AwsSecretsManagerStore` refuses wildcards), tombstone-on-revoke, `EphemeralSecret` zeroization, jobs carry refs only, Company Brain redacts sensitive keys, dashboard projections strip refs. Additions required:

1. **Per-provider secret types** in `CAPABILITY_SECRET_TYPES` for `SCHEDULING`, `PAYMENTS`, `ACCOUNTING`, `ANALYTICS`, `SEARCH`, `LOCAL_PRESENCE`, `DOMAIN_DNS`, and role policies so `_install_grants` no longer returns early for non-email capabilities.
2. **Rotation-aware refresh** for Calendly (single-use rotation) and QuickBooks (daily rotation, 24-hour overlap): persist the newest refresh token atomically under the existing lease; a lost rotation fails closed to `reconnect_required`.
3. **Platform-level secrets** (Stripe platform key, Google client secrets, Calendly signing key, HubSpot client secret, QuickBooks verifier token, Cloudflare account token for our own hosting, GA/GSC service-account keys) live in Secrets Manager under environment-specific exact ARNs, referenced by the task role policy per adapter. The staging execution-role wildcard `businessbuilder-staging-*` should be replaced with exact ARNs to match the documented rule.
4. **Stripe holds no per-customer secret**: store `acct_…` as safe account identity; the platform key is the only secret.
5. **Webhook secrets** are per endpoint (Stripe Connect endpoint separate from the billing endpoint) and never logged.
6. **Never store**: customer Stripe keys, QuickBooks data beyond functional need, Google refresh tokens in PostgreSQL columns, card data (the Stromation `.env` contains card-detail variables in plaintext; that pattern must not be carried into Business Builder), GBP content beyond 30 days.

Credential probe results (names only, 2026-09-15, Stromation `.env`): `STRO_SECRET_CF_KV_TOKEN` is an active Cloudflare token with zone read on zero zones and no registrar permission (registrar list returned 403 code 10000), which demonstrates the scope model; `STRO_WEBSITE_STRIPE_TEST_KEY` reaches a US Standard account with charges and payouts enabled and Connect account listing returns 200; `STRO_SECRET_STRIPE_KEY` is a live **restricted** key that cannot read the account (correct hygiene); `STRO_SECRET_GA4_SA_JSON` parses as a service account; the AWS user in that file is Bedrock-only and cannot inspect the Business Builder staging infrastructure. None of these are Business Builder production credentials and none were modified.

## 11. Dashboard requirements

The card contract exists (`safe_connection()`: provider, capability, connected account, ownership, auth method, scopes, status, health, message, timestamps, action required, reconnect/disconnect availability, measured quota). Required additions:

- **Per-state rendering** in the flagship: distinct copy, colour and icon for each of the ten states (today all render through one title-case formatter) and a distinct low-quota style when `quota` is present with a warning.
- **Capability split**: `SEARCH` alongside `ANALYTICS` so GA4 and Search Console do not collide.
- **Provider-specific action panels**: Stripe payout/KYC status with deadline countdown and "Continue Stripe setup" (mints a fresh Account Link); Calendly plan-gate notice; QuickBooks read-only banner and income-account chooser; GBP verification and review-reply consent; Cloudflare nameserver/records status; Google admin-approval notice.
- **Ownership and handoff** per card: `business_owned` / `founder_owned_legacy` / `unverified`, plus a handoff checklist entry (revoke, remove member/owner/manager, export) that feeds the existing `handoff.complete` verification definition.
- **Existing-business audit**: drive KEEP/IMPROVE/REPLACE/MISSING rows from `connection_providers` rather than the five hardcoded rows, adding domain, analytics, search, bookkeeping and local presence.
- **Copy location**: move the inline Connections strings into `src/content/` per the flagship's rule.
- **Never shown**: secret refs, locators, raw provider payloads, token metadata, IAM details.

## 12. Verification and evidence requirements

Verification stays the only authority. Connection health is evidence or a blocker, never readiness. Required wiring:

- Route connection Founder Actions (`founder_action_connection_*`) through the same evidence-submission, operator-review and Verification path as residential-cleaning actions; today they are excluded by the `founder_action_cleaning_` prefix filter and can never close.
- Provider receipts become `PROVIDER_RECEIPT` evidence for existing definitions: `domain.ownership` (Cloudflare zone active + TXT observed + Search Console owner), `email.inbound`/`email.outbound` (Gmail profile + watch + test round trip; outbound stays blocked by communications gates), `scheduling.booking` (Calendly webhook + Calendar event round trip), `crm.lead_capture` (HubSpot upsert + webhook), `payment.test` (Stripe `charges_enabled` + test Payment Link + signed `account.updated`), `handoff.complete` (revocations and removals proven).
- New definitions: `calendar.dedicated` (secondary calendar exists, ACL correct, watch active), `analytics.tag_live` (realtime hit observed), `search.verified` (owner/full permission, sitemap ok), `accounting.connected` (subscription read/write, service item confirmed by founder attestation), `local_presence.verified` (`hasVoiceOfMerchant`), `evidence.scan_clean` (GuardDuty tag, checksum match, lock applied).
- Evidence records gain `s3_version_id`, `sha256_s3`, `scan_status`, `lock_mode`, `locked_until`; acceptance order is scan → accept → lock.

## 13. Implementation phases

**Phase 1, launch-critical.** Spine closure (grants and secret types for all capabilities, callback route, dispatched reconciliation, Founder Action closure). S3 baseline hardening (SSE-KMS, versioning, Object Lock GOVERNANCE at ACCEPT, deny-delete policy, CloudTrail) and GuardDuty async scan path with TBAC gate; this satisfies the malware-scanner release gate. Stripe Connect in test mode with full-dashboard controller defaults, Account Links, Connect-scoped webhook and requirements→health mapping; live charging stays behind the paid-pilot release gate. Google Calendar on a dedicated `bookings@` calendar with watch channels and sync tokens. Cloudflare DNS via customer-issued zone token (records, verification TXT, health). Google Workspace inbound read/classify only if OAuth verification and CASA are in progress; otherwise Phase 2. Calendly OAuth with org webhooks (zero approval lead time) once Calendar is correct. Business Profile and QuickBooks appear as honest manual Founder Actions with evidence, no adapters. Start GBP API access and Stripe platform profile applications on day one.

**Phase 2, immediately after launch.** Google Workspace send (still gated by communications approvals), GA4 (service-account binding, property/stream/key events, realtime liveness), Search Console (domain property via Cloudflare TXT, sitemap, reporting), HubSpot public app (unlisted, ≤25 installs, tier probe, journal poller), QuickBooks questionnaire prerequisites (legal pages, four URLs), flagship per-state rendering and provider panels, KEEP/IMPROVE/REPLACE data-driven audit.

**Phase 3, customer demand.** QuickBooks adapter (cache-first, CloudEvents webhooks, income-account Founder Action), Business Profile API management (Organization Manager, VoM reconcile, Pub/Sub, consented review replies, 30-day purge), Jobber as vertical scheduling successor (Draft app to 5 paying accounts then App Review), HubSpot Marketplace listing after three installs, optional Stripe `application_fee`, Accounts v2 migration when it leaves preview.

## 14. Provider-by-provider repo readiness

Spine (all providers): EXISTS for model, ports, secret store, refresh lease, callback dedupe, health normalization, card projection, job-time gate, Founder Action creation; PARTIAL for grants (EMAIL only), secret types (email/CRM only), reconciliation (built, never dispatched), callback verifier (no route), Founder Action closure (excluded from evidence/Verification); MISSING for any real adapter.

| Provider | Adapter | OAuth/auth flow | Secret ref | Health | Webhook | Card | Founder Action | Verification |
|---|---|---|---|---|---|---|---|---|
| Cloudflare | MISSING | PARTIAL (scoped-key path exists) | PARTIAL | MISSING | MISSING | PARTIAL | PARTIAL (`domain` checklist) | PARTIAL (`domain.ownership`) |
| Google Workspace | MISSING (`DisabledGoogleWorkspaceAdapter` is a contract stub) | PARTIAL (PKCE sandbox) | PARTIAL | MISSING | MISSING (Pub/Sub described only) | PARTIAL | PARTIAL (`business_email`) | PARTIAL (`email.*`) |
| Google Calendar | MISSING (`SandboxCalendarProvider` is `.invalid`) | PARTIAL | PARTIAL | MISSING | MISSING | PARTIAL | PARTIAL (`scheduling`) | MISSING |
| HubSpot | MISSING | PARTIAL | PARTIAL (`crm.oauth` type exists) | MISSING | MISSING | PARTIAL | PARTIAL (`crm`) | PARTIAL (`crm.lead_capture`) |
| Calendly | MISSING | PARTIAL | PARTIAL | MISSING | MISSING | PARTIAL | PARTIAL | PARTIAL (`scheduling.booking`) |
| Stripe Connect | MISSING | PARTIAL (billing verifier reusable) | PARTIAL (no payments type) | MISSING | PARTIAL (billing route pattern) | PARTIAL | PARTIAL (`payments`) | PARTIAL (`payment.test`) |
| GA4 | MISSING | PARTIAL | PARTIAL | MISSING | n/a | PARTIAL | MISSING | MISSING |
| Search Console | MISSING | PARTIAL | PARTIAL | MISSING | n/a | PARTIAL (collides) | PARTIAL (`domain`) | PARTIAL (`domain.ownership`) |
| QuickBooks | MISSING | PARTIAL | PARTIAL | MISSING | MISSING | PARTIAL | MISSING | MISSING |
| Business Profile | MISSING | PARTIAL | PARTIAL | MISSING | MISSING | PARTIAL | MISSING (marketing only) | MISSING |
| S3 evidence | EXISTS (`S3ArtifactStore`, SSE-S3, exact prefix) | n/a | EXISTS | PARTIAL | n/a | n/a | PARTIAL | PARTIAL |
| GuardDuty | MISSING (`PendingMalwareScanner` fail-closed stub) | n/a | n/a | MISSING | MISSING | n/a | PARTIAL (quarantine gate) | MISSING |

Frontend: generic card EXISTS; per-state copy MISSING; quota styling MISSING; capability split MISSING; provider panels MISSING; data-driven audit rows MISSING; connections integration script covers only HEALTHY/DISCONNECTED on the sandbox calendar.

## 15. Exact recommended implementation order

1. Spine closure: capability secret types and role grants for all capabilities; `POST /api/v1/provider-callbacks/{provider}` route wired to `handle_provider_callback`; persist and dispatch `reconciliation_schedule`; connection Founder Action closure through evidence review and Verification.
2. S3 baseline hardening plus GuardDuty async scan state machine and TBAC gate (release gate).
3. Stripe Connect test mode (Account + Account Links + Connect webhook + requirements health + Payment Link proof).
4. Google Calendar dedicated-calendar adapter with watch channels (the checkpoint's stated next task), published OAuth client.
5. Cloudflare DNS scoped-token adapter and `domain.ownership` runner.
6. Calendly OAuth adapter with rotating refresh and webhook auto-repair.
7. Google Workspace inbound (after verification/CASA), send still gated.
8. Flagship per-state rendering, provider panels, data-driven audit.
9. GA4 then Search Console.
10. HubSpot unlisted public app.
11. QuickBooks (after questionnaire).
12. Business Profile API (after access approval).
13. Jobber.

## 16. Providers to replace or correct in the preferred stack

- **Replace nothing outright.** Every preferred provider is the right default for its capability.
- **Cloudflare Registrar: correct the expectation.** The Registrar API only lists, gets and updates existing domains; there is no API operation that registers a new domain. Business Builder therefore cannot and should not register domains for customers; the customer registers in their own Cloudflare account (at-cost, transfer-lock aware) and grants a zone-scoped DNS token, preferably an account-owned token so it survives staff changes.
- **GA4: correct the flow.** No account provisioning by Business Builder; property creation inside the customer's account, service-account binding preferred.
- **Google Business Profile: correct the ambition.** Founder-initiated verification with evidence is the v1 product; API management is Phase 3 after access approval; review archive must respect the 30-day storage clause.
- **Calendly: plan the successor.** Jobber (public GraphQL API, OAuth, `APP_DISCONNECT` webhook, cost-based rate limits with visible budget) is the recommended vertical scheduling and job system for cleaning; Housecall Pro is partner-gated; ZenMaid, Booking Koala and Launch27 have no usable public API.
- **Stripe: correct the vocabulary.** "Standard account" is a deprecated type name; build on controller properties (`losses.payments=stripe`, `fees.payer=account`, `requirement_collection=stripe`, `stripe_dashboard.type=full`).

## 17. Risks that could block launch

1. Gmail read scopes are restricted and require OAuth verification plus an annual CASA assessment ("several weeks") before any real customer mailbox can be read; if not started now, inbound email is not in Phase 1. Sending needs only sensitive-scope verification but stays behind the communications approvals.
2. Google OAuth clients left in Testing status issue 7-day refresh tokens; every Google connection would silently break.
3. The evidence path has no production scanner; the GuardDuty result path is asynchronous and the current scanner port is synchronous, so the evidence state machine must change before the release gate can be met.
4. Stripe live mode is gated by the nine paid-pilot approvals; Connect in live mode also requires platform activation and profile review with no published SLA.
5. Connection Founder Actions cannot be closed today, so a degraded connection would create an action that never verifies.
6. `NotPrincipal`-based TBAC bucket policy and tag-under-retention behaviour are documented patterns but must be proven in staging before evidence acceptance depends on them.
7. Cloudflare account/zone ownership: if a customer's domain is registered elsewhere or under a Business Builder account, handoff and 60-day transfer locks become a support burden.
8. GBP verification is a human step that can stall onboarding for service-area businesses; the product must tolerate an unverified profile indefinitely.
9. Calendly Free tier has no webhooks; booking capture silently degrades to polling unless the founder upgrades.
10. Staging execution role uses a wildcard secret ARN contrary to the documented policy; production IAM must be exact.

## 18. Exact next engineering task

**Connections Spine Closure v1** in the backend, before any adapter:

1. Extend `CAPABILITY_SECRET_TYPES` and `ROLE_OPERATION_SCOPES`-driven `_install_grants` to every capability with an AI Workforce role policy, and add `SEARCH` to the capability set.
2. Add an authenticated-by-signature inbound route `POST /api/v1/provider-callbacks/{provider}` that calls `handle_provider_callback`, with per-provider verifier selection and the existing `bb_provider_callback_events` dedupe.
3. Persist `reconciliation_schedule()` on connection activation and dispatch it through the existing Runtime scheduler so health becomes proactive.
4. Route `founder_action_connection_*` through evidence submission, operator review and Verification so reconnect/scope/credits actions can complete, and add the connection definitions listed in §12.
5. Add tests proving: a `PAYMENTS`/`CALENDAR`/`SCHEDULING` connection can pass `_authorize_secret_record` with a grant; a signed callback marks a connection `REVOKED` through HTTP; a due reconcile runs and demotes health; a connection Founder Action reaches `VERIFIED` only through the evidence path.

Immediately in parallel, non-engineering: publish Google OAuth clients and begin sensitive/restricted verification; submit the GBP API access application; complete the Stripe platform profile.
