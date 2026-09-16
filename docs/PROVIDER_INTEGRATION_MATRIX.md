# Provider Integration Matrix

Companion to `PRODUCTION_PROVIDER_STACK.md`. Researched 2026-09-15 against official provider documentation and the repositories at `codex/founder-dashboard-connections-v1` (backend `74ee909`, site `5d9e4b0`). Anything not confirmed from a primary source is marked **UNVERIFIED**. Nothing in this document enables a live integration.

Readiness legend used throughout: **EXISTS** (code seen in-tree), **PARTIAL** (generic machinery exists, nothing provider-specific), **MISSING** (zero occurrences).

Repo facts that apply to every provider row (see `PRODUCTION_PROVIDER_STACK.md` §14 for file references):

- The provider-neutral spine exists: `ProviderConnection` with capability, health, ownership and opaque `secretref_` IDs; `OAuthProviderPort` / `ScopedKeyProviderPort`; `SecretStorePort` with an exact-ARN Secrets Manager adapter; refresh lease; callback dedupe; `safe_connection()` card; `_ensure_founder_action`; job-time fail-closed health gate.
- No real provider adapter exists for any of the twelve providers. The three adapters in tree are process-local sandboxes.
- `_install_grants` only installs grants for `EMAIL`; `CAPABILITY_SECRET_TYPES` has no payments, accounting, analytics, scheduling or DNS entry. Every non-email capability therefore needs a role policy and secret-type entry before a job could run.
- `handle_provider_callback` has a verifier and durable dedupe but no HTTP route; `reconciliation_schedule()` is never persisted or dispatched.
- Connection Founder Actions (`founder_action_connection_*`) cannot reach evidence review or Verification.
- Frontend renders one generic card per connection; all ten health states render through the same title-case formatter with no state-specific copy, colour or icon; the existing-business KEEP/IMPROVE/REPLACE/MISSING form is hardcoded to five rows.

---

## 1. Cloudflare (domain registration and DNS)

| # | Item | Finding |
|---|---|---|
| 1 | Provider | Cloudflare (Registrar, DNS, optional Cloudflare for SaaS) |
| 2 | Business capability | `DOMAIN_DNS`: zone and record management for the business domain, verification TXT records (Search Console, Workspace), MX/SPF/DKIM/DMARC, pointing the site at Business Builder hosting. |
| 3 | Recommended role | Default DNS provider. **The customer holds the domain and zone in their own Cloudflare account and issues Business Builder a zone-scoped token.** Business Builder does not register domains for customers. |
| 4 | Default for new businesses | Yes for DNS. Registration is a Founder Action inside the customer's own Cloudflare account. |
| 5 | KEEP alternatives | Yes. A domain registered at GoDaddy/Namecheap/Google-Squarespace is KEEP: either move nameservers to a customer-owned Cloudflare zone (IMPROVE) or record the registrar as manual status with a Founder Action for each DNS change. |
| 6 | Account ownership | Customer-owned Cloudflare account, zone and registrar record. Business Builder holds a token only. Business Builder's own Cloudflare account is used solely for its hosting and, if adopted, Cloudflare for SaaS custom hostnames (customer CNAMEs to Business Builder's fallback origin; UNVERIFIED current pricing tiers for custom hostnames beyond the free allotment). |
| 7 | Who creates account | Founder creates the Cloudflare account and registers or transfers the domain. |
| 8 | Who pays | Founder pays registration and renewal at Cloudflare's at-cost pricing; DNS on the Free plan is $0. |
| 9 | Authentication | Scoped API token (Cloudflare has no OAuth for third parties). Preferred: an **account-owned token** ("Account API tokens" exist alongside user tokens: "Use Account API tokens if you prefer service tokens that are not associated with users") so the token survives staff changes; permissions `Zone:DNS:Edit` + `Zone:Zone:Read` (add `Zone:Zone Settings:Read` for SSL mode) restricted to the single zone ("Any other zone will return an error"), TTL set, optional client-IP filter to Business Builder's egress IPs ([create token](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)). Token secret is shown once; the founder pastes it into the existing scoped-key connection flow. |
| 10 | OAuth availability | None. |
| 11 | Scopes | As in row 9. Never request account-level permissions, registrar permissions, or `*` zone scope. |
| 12 | Token fallback | Global API key is forbidden (full-account). |
| 13 | Public app approval | None. |
| 14 | Partner/reseller approval | None. |
| 15 | API gating | **The Registrar API exposes only list, get and update of existing domains (`/accounts/{account_id}/registrar/domains[/{domain_name}]`); there is no API operation that registers a new domain** ([Registrar domains API](https://developers.cloudflare.com/api/resources/registrar/subresources/domains/)). Registration and transfers-in are dashboard actions in the registrant's account. This settles the "register on behalf of a customer" question: not automatable, and not desirable. |
| 16 | API families | `POST/GET /zones` (status `pending` until nameservers are delegated, then `active`; `moved` when delegation leaves), `GET/POST/PATCH/DELETE /zones/{zone_id}/dns_records`, `POST /zones/{zone_id}/dns_records/batch`, `GET /zones/{zone_id}/ssl/universal/settings`, `GET /user/tokens/verify` (status `active`/`expired`/`disabled`), registrar list/get/update (auto-renew, lock, privacy fields; exact writable set UNVERIFIED). |
| 17 | Webhooks | No per-record change webhook. Cloudflare Notifications can deliver alerts (including domain expiration and some zone events) to a webhook destination; treat as supplementary. Audit Logs API for change history. |
| 18 | Rate limits | 1,200 requests per five minutes per user, cumulative across dashboard and API; exceeding blocks all calls for five minutes with 429 ([limits](https://developers.cloudflare.com/fundamentals/api/reference/limits/)). Account tokens: UNVERIFIED whether the same window applies per token. |
| 19 | Quota/balance visibility | None beyond 429 and registrar expiry dates (`expires_at` on the domain record). Never show a balance. |
| 20 | Health signals | Token verify `active` (proactive); zone `status == active`; public DNS resolution shows Cloudflare nameservers; expected records present with expected values (A/CNAME to Business Builder host, MX for Workspace, TXT for verifications); Universal SSL active; registrar `expires_at` > 30 days and auto-renew on (when the token can read registrar data, which a zone-scoped token cannot; treat as UNKNOWN otherwise). Probe result 2026-09-15: a Stromation Cloudflare token verified `active`, listed zero zones and was denied registrar reads with error 10000, confirming the scope behaviour. |
| 21 | Errors to normalize | 10000 (authentication error on a resource outside token scope) → PERMISSION_ERROR "Business Builder's Cloudflare token doesn't cover this zone. Re-issue the token for your domain."; 9109 / verify `expired` → AUTH_EXPIRED "Your Cloudflare token expired. Create a new one and reconnect."; verify `disabled` → DISCONNECTED; 429 → RATE_LIMITED; 81057 (record already exists) → WARNING and treat as idempotent success; 1004 (DNS validation) → WARNING with field copy; zone `pending` → ACTION_REQUIRED "Point your domain's nameservers at Cloudflare to finish setup."; zone `moved` → ACTION_REQUIRED "Your domain no longer uses Cloudflare DNS."; 5xx → PROVIDER_OUTAGE. |
| 22 | Safe automation | Create, update and verify DNS records (idempotent by name/type/content); place verification TXT records for Search Console and Workspace; MX/SPF/DKIM/DMARC from provider-supplied values; check nameserver delegation and SSL status; read registrar expiry where permitted. |
| 23 | Founder Action | Create the Cloudflare account; register or transfer the domain (at-cost, 60-day ICANN transfer lock after registration or registrant change); change nameservers at an external registrar; create and paste the scoped token; confirm auto-renew and payment method; unlock and export the auth code on handoff or move away. |
| 24 | Provider/authority action | Registry and ICANN rules (transfer locks, WHOIS/registrant verification emails). |
| 25 | Evidence of setup | Token verify `active` with the safe token ID; zone ID and `active` status; delegated nameservers observed from public DNS; required records observed resolving; Universal SSL active. |
| 26 | Verification should check | `domain.ownership` (exists: `dns-control` test, `TEST_RESULT` + `PROVIDER_RECEIPT` + `FOUNDER_ATTESTATION`, 365-day TTL): the receipts above plus a control test (write and read back a short-lived TXT record), never a screenshot alone. |
| 27 | Disconnect/revocation | Founder deletes or rolls the token in Cloudflare; Business Builder tombstones the secret ref and marks `DISCONNECTED`. Records created remain in the customer's zone. |
| 28 | Handoff | Nothing to transfer: the domain and zone were always the customer's. The handoff checklist confirms the token is revoked and documents each record Business Builder created. If a site moves off Business Builder hosting, only the A/CNAME changes. |
| 29 | Customer ownership | Full, by construction. This is the way to avoid domain lock-in; registering in a Business Builder account would create registrant, billing-card and transfer-lock entanglement. |
| 30 | Compliance/security | Token scoped to one zone with expiry; rotate on staff change; never store the global API key; audit every record write with before/after values (no secrets). Cloudflare Registrar terms restrict registrations to the account holder's own use; registering for third parties is not the intended model (UNVERIFIED exact clause). |
| 31 | Cost structure | Registration at cost (a `.com` is roughly $10–11 per year; exact current wholesale UNVERIFIED); DNS Free plan $0; Cloudflare for SaaS custom hostnames pricing UNVERIFIED for 2026. |
| 32 | Who bears cost | Founder for the domain; Business Builder for its own hosting account. |
| 33 | Production prerequisites | Token-paste flow copy that names the exact permissions; record templates for Workspace and Business Builder hosting; public DNS resolver checks from the worker; Cloudflare Notifications webhook (optional). |
| 34 | Repo readiness | Adapter MISSING; auth PARTIAL (`ScopedKeyProviderPort` and `scoped-key-connections` route exist and fit exactly); secret-ref PARTIAL (no `DOMAIN_DNS` secret type); health MISSING; webhook n/a; card PARTIAL; Founder Action PARTIAL (`domain` checklist points at provider-connections); Verification PARTIAL (`domain.ownership` definition, `DNS_OBSERVATION` evidence type, no runner). Company Brain fixture already models a `domain_registrar` account record with `ownership: founder`. |
| 35 | Missing backend | `CloudflareDnsProvider(ScopedKeyProviderPort)` with `validate_key` via `/user/tokens/verify` + zone list; record upsert operations behind capability grants; public-DNS observation job; `DOMAIN_DNS` secret type and role policy; `domain.ownership` runner. |
| 36 | Missing frontend | Token-issuance instructions with exact permissions; nameserver status card; audit-form row for domain. |
| 37 | Difficulty | 2/5. |
| 38 | Order | Phase 1 (prerequisite for Workspace MX and Search Console verification). |

## 2. Google Workspace (business email)

| # | Item | Finding |
|---|---|---|
| 1 | Provider | Google Workspace (Gmail API; Admin SDK optional; Reseller API not used) |
| 2 | Business capability | `EMAIL`: dedicated business mailboxes (`ops@`, `support@`, `bookings@`), inbound read/classify for the Inbox Assistant, preapproved sending later. |
| 3 | Recommended role | Default business email. The founder buys Workspace and creates the dedicated mailboxes (Founder Action); Business Builder places DNS records through Cloudflare and connects each mailbox by OAuth. Mailbox creation via Admin SDK is deferred; the Reseller API is not needed (it exists only to sell and provision subscriptions and requires Google partner status). |
| 4 | Default for new businesses | Yes. |
| 5 | KEEP alternatives | Yes: Microsoft 365 (no adapter in v1; manual status), an existing Workspace tenant (KEEP; connect the dedicated mailbox), personal Gmail (documented `founder_owned_legacy` exception, never the default). |
| 6 | Account ownership | Customer-owned Workspace tenant and mailboxes. Business Builder holds per-mailbox OAuth tokens. |
| 7 | Who creates account | Founder (tenant, domain verification, users). |
| 8 | Who pays | Founder, per user per month (Business Starter list price UNVERIFIED for 2026; historically about $7–8.40 per user per month depending on term). |
| 9 | Authentication | OAuth 2.0 per mailbox with PKCE (the existing flow). **Scope classification from Google ([Gmail scopes](https://developers.google.com/gmail/api/auth/scopes)): `gmail.readonly`, `gmail.metadata`, `gmail.modify`, `gmail.compose`, `gmail.insert`, `gmail.settings.*` and `mail.google.com` are RESTRICTED; `gmail.send` is SENSITIVE; `gmail.labels` is non-sensitive.** Consequence: inbound read requires restricted-scope verification plus an annual CASA security assessment; preapproved sending needs only sensitive-scope verification. |
| 10 | OAuth availability | Yes. Restricted-scope verification "can potentially take several weeks"; CASA by a Google-empanelled assessor, renewed every 12 months from the Letter of Assessment ([restricted-scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)). Exemptions (internal-only apps, domain-wide delegation for enterprise-only installs) do not fit a multi-tenant SaaS without accepting org-wide impersonation risk. Publishing status must be "In production"; Testing issues 7-day refresh tokens. |
| 11 | Scopes | Phase 1 inbound: `gmail.readonly` (or `gmail.metadata` if bodies are not needed) + `userinfo.email`; Phase 2 send: `gmail.send`. Never `mail.google.com` or `gmail.modify`. Admin SDK `admin.directory.user` only if automated user creation is later approved. |
| 12 | Token fallback | None acceptable. App passwords/IMAP are a reviewed legacy exception only. |
| 13 | Public app approval | Brand verification (2–3 business days), sensitive verification for send, restricted verification + CASA for read. |
| 14 | Partner/reseller approval | Not needed. |
| 15 | API gating | Workspace admins can set API controls to block or limit third-party apps; the customer admin may need to mark Business Builder's client as trusted. Mailboxes must have Gmail enabled (`failedPrecondition` otherwise). |
| 16 | API families | `users.getProfile`, `users.messages.list/get/send`, `users.labels`, `users.history.list` (incremental sync), `users.watch` via Cloud Pub/Sub (7-day expiry, renew daily), `users.stop`; Admin SDK Directory (deferred); DNS records for MX, SPF, DKIM (`google._domainkey` from the Admin console), DMARC via Cloudflare. |
| 17 | Webhooks | Pub/Sub push from `users.watch`; verify the OIDC JWT on the push (`DisabledGoogleWorkspaceAdapter` already describes this verification method). Signed callback handling exists in the service but has no HTTP route. |
| 18 | Rate limits | 6,000 quota units per user per minute; 80,000,000 units per project per day; `messages.list` 5, `messages.get` 20, `messages.send` 100, `users.getProfile` 1, `users.watch` 100 ([quota](https://developers.google.com/workspace/gmail/api/reference/quota)). Errors 429 and 403 `userRateLimitExceeded`/`dailyLimitExceeded`. |
| 19 | Quota visibility | Project quota in Cloud Console only; no per-customer balance. Warn only on classified 429/403 signals. |
| 20 | Health signals | `users.getProfile` (address, `historyId`); `tokeninfo` scopes; watch channel expiry; MX records observed via public DNS; last successful `history.list`; admin block surfaces as 403 on reconcile. |
| 21 | Errors to normalize | `invalid_grant` (also fired when the mailbox password changes or the admin revokes) → AUTH_EXPIRED "Google Workspace needs attention. Reconnect the mailbox to restore email."; 403 `insufficientPermissions` → PERMISSION_ERROR; 403 admin-blocked app → ACTION_REQUIRED "Your Workspace admin must allow Business Builder under API controls."; 429/403 rate → RATE_LIMITED; 400 `failedPrecondition` → ACTION_REQUIRED "Gmail isn't enabled for this mailbox."; 5xx → PROVIDER_OUTAGE; watch expiry missed → WARNING then re-watch. |
| 22 | Safe automation | DNS record placement; profile and scope checks; read/classify/attachment metadata under `mail.read` grants (already modelled); watch renewal; preapproved replies under `mail.send` grants once communications approvals allow. |
| 23 | Founder Action | Buy Workspace; verify the domain (TXT via Cloudflare, Business Builder can place it once the token exists); create `ops@`/`support@`/`bookings@`; authorize each mailbox; approve the app under admin API controls; enable DKIM in the Admin console. |
| 24 | Provider/authority action | Google verification and CASA outcome. |
| 25 | Evidence of setup | MX/SPF/DKIM/DMARC observed; `users.getProfile` on the dedicated address whose domain equals the business domain (attests `business_owned`); granted scopes ⊆ requested; watch active; inbound test message received (`email.inbound`); outbound stays blocked by the communications gates. |
| 26 | Verification should check | `email.inbound` / `email.outbound` (exist): provider receipts from the round trips; ownership attestation from the address domain. |
| 27 | Disconnect/revocation | Business Builder revokes the token (`oauth2.googleapis.com/revoke`), stops the watch, tombstones the ref; the admin can also remove the app. |
| 28 | Handoff | Customer already owns the tenant; handoff removes Business Builder's app and documents records placed. |
| 29 | Customer ownership | Full. |
| 30 | Compliance/security | Restricted data handling under Google's User Data Policy (limited use, no ads, no human reading beyond consent); CASA scope covers the systems that store or transmit mailbox data; no domain-wide delegation; outbound sending remains under the existing consent/suppression/canary approvals. |
| 31 | Cost | Workspace per seat (customer); CASA assessment (Business Builder; lab-priced, UNVERIFIED range). |
| 32 | Who bears cost | Founder for seats; Business Builder for verification and CASA. |
| 33 | Production prerequisites | Published OAuth client with privacy policy and homepage on the Business Builder domain, demo video, restricted-scope verification and CASA for read; Pub/Sub topic and push endpoint with JWT verification; callback route; Secrets Manager exact ARNs per environment. |
| 34 | Repo readiness | Adapter MISSING (`DisabledGoogleWorkspaceAdapter` in `live_canary/ports.py` is a contract stub implementing `EmailDeliveryAdapter`, not `OAuthProviderPort`); OAuth PARTIAL (production-shaped PKCE flow and sandbox email adapter exist; `EMAIL` is the only capability with grants); secret-ref PARTIAL (`email.oauth` type exists); health MISSING; webhook MISSING (no route); card PARTIAL; Founder Action PARTIAL (`business_email` checklist); Verification PARTIAL (`email.*` definitions without runners). |
| 35 | Missing backend | `GoogleWorkspaceOAuthProvider` (authorization URL, code exchange, refresh, revoke, `reconcile_connection` via profile + tokeninfo), Pub/Sub push route with JWT verification, watch renewal schedule, MX/SPF/DKIM templates for the Cloudflare adapter, ownership attestation rule. |
| 36 | Missing frontend | Mailbox-creation Founder Action card, admin-approval notice, distinct copy for restricted-scope consent. |
| 37 | Difficulty | 4/5 (verification and CASA lead time, not code). |
| 38 | Order | Phase 1 only if verification and CASA are started immediately; the checkpoint already names this the next task. Sending is Phase 2 and remains gated by communications approvals. |

## 3. Google Calendar

| # | Item | Finding |
|---|---|---|
| 1 | Provider | Google Calendar API v3 |
| 2 | Business capability | `CALENDAR`: a dedicated business booking calendar, event create/read/update/cancel, availability, change notifications. |
| 3 | Recommended role | Default calendar. **Dedicated-calendar model:** a secondary calendar ("Bookings") owned by the `bookings@` or `ops@` Workspace user, created by Business Builder under `calendar.app.created`, shared to staff by ACL. Never the founder's primary calendar. |
| 4 | Default for new businesses | Yes. |
| 5 | KEEP alternatives | Yes: Outlook/Microsoft 365 calendar (no adapter in v1), an existing shared Google calendar (KEEP; grant `calendar.events` on that calendar only). |
| 6 | Account ownership | The Workspace user owns the calendar; Business Builder holds a scoped token. Calendar ownership can be transferred between users by ACL `owner` role if the business changes hands. |
| 7 | Who creates account | Founder (Workspace user); Business Builder creates the secondary calendar. |
| 8 | Who pays | Nobody beyond the Workspace seat. |
| 9 | Authentication | OAuth 2.0 with PKCE on the dedicated business account. Least-privilege scopes confirmed on Google's scope list ([Calendar auth](https://developers.google.com/workspace/calendar/api/auth)): `calendar.app.created` ("Make secondary Google calendars, and see, create, change, and delete events on them") is the primary scope; add `calendar.calendarlist.readonly` for discovery and `calendar.events.freebusy` for availability across calendars. For an existing customer calendar use `calendar.events.owned` instead of the broad `calendar.events`. `calendar.acls` only during setup to share with staff. Sensitivity labels are not printed on the page; treat as sensitive (verification required, no CASA). |
| 10 | OAuth availability | Yes; sensitive-scope verification (days), publishing status "In production". |
| 11 | Scopes | As row 9. Never `https://www.googleapis.com/auth/calendar` (full). |
| 12 | Token fallback | None. |
| 13 | Public app approval | OAuth verification. |
| 14 | Partner/reseller approval | None. |
| 15 | API gating | None beyond quotas. |
| 16 | API families | `calendars.insert` (secondary calendar), `acl.insert` (`reader`/`writer` for staff), `events.insert/patch/update/delete` with `attendees` + `sendUpdates`, `conferenceData`, `recurrence` (RRULE) + `events.instances` for exceptions, `freebusy.query`, `events.list` with `syncToken` (410 Gone → full resync), `events.watch` / `channels.stop`. |
| 17 | Webhooks | `events.watch` push to an HTTPS endpoint with a valid public certificate (self-signed rejected); headers `X-Goog-Channel-ID`, `X-Goog-Message-Number`, `X-Goog-Resource-ID`, `X-Goog-Resource-State`, `X-Goog-Resource-URI`, sometimes `X-Goog-Channel-Expiration`/`X-Goog-Channel-Token`; expiration is the more restrictive of the request and Google's internal default (exact maximum not published on the guide; UNVERIFIED, commonly observed around one week); no automatic renewal, replace with a new `watch` before expiry ([push guide](https://developers.google.com/workspace/calendar/api/guides/push)). Notifications carry no payload; follow with an incremental `events.list`. Verify the channel token and ID against the stored channel before acting. |
| 18 | Rate limits | Per-project and per-user per-minute quotas set in Cloud Console (default project quota historically 1,000,000 queries/day; current numbers UNVERIFIED on this pass); errors 403 `usageLimits`/`rateLimitExceeded`, 429. |
| 19 | Quota visibility | Cloud Console only; no balance. |
| 20 | Health signals | Calendar reachable (`calendars.get`); ACL still includes Business Builder's principal; channel active and not within 24 h of expiry; sync token valid; token scopes ⊇ required. |
| 21 | Errors to normalize | 401 `invalid_credentials`/`invalid_grant` → AUTH_EXPIRED "Google Calendar needs attention. Reconnect it to restore scheduling."; 403 `insufficientPermissions` → PERMISSION_ERROR; 403 `usageLimits`/429 → RATE_LIMITED; 404 calendar not found → ACTION_REQUIRED "The bookings calendar was deleted. Reconnect to create it again."; 410 gone → WARNING (full resync); 412 `conditionMismatch` → retry with fresh etag; 5xx → PROVIDER_OUTAGE. |
| 22 | Safe automation | Create the secondary calendar; share with staff (from Company Brain roster, founder-approved); create/update/cancel events for bookings; recurring events with exceptions; free/busy checks; watch renewal; incremental sync. |
| 23 | Founder Action | Authorize on the business account; confirm staff sharing list; decide whether the founder's personal calendar is also consulted for availability (default no). |
| 24 | Provider/authority action | Google verification. |
| 25 | Evidence of setup | Calendar ID owned by an address on the business domain; ACL list; a test event round trip (insert, patch, instance, delete); active watch channel with expiry; sync token stored. |
| 26 | Verification should check | New `calendar.dedicated` definition plus `scheduling.booking` (exists) for the booking/conflict round trip using provider receipts and the verified watch notification. |
| 27 | Disconnect/revocation | `channels.stop` for every channel, revoke the token, tombstone the ref. The calendar and events stay with the customer. |
| 28 | Handoff | Calendar already belongs to the business user; document the calendar ID and staff ACLs; if ownership must move, the owner transfers by ACL in the Google UI. |
| 29 | Customer ownership | Full. |
| 30 | Compliance/security | Event bodies contain client addresses and access notes; keep the same PII posture as Calendly; verify push channel identity; never grant `calendar` full scope. |
| 31 | Cost | Free within Workspace. |
| 32 | Who bears cost | Nobody. |
| 33 | Production prerequisites | Published OAuth client (can share the Workspace client), HTTPS push endpoint with a public certificate, channel renewal schedule, Secrets Manager exact ARNs. |
| 34 | Repo readiness | Adapter MISSING (`SandboxCalendarProvider` uses `.invalid` URLs and forbids execution); OAuth PARTIAL (flow exists; `allowed_scopes` currently `{"calendar.read"}` placeholder); secret-ref PARTIAL (no `CALENDAR` secret type, `_install_grants` skips it); health MISSING; webhook MISSING; card PARTIAL (the only provider exercised end to end in the flagship integration script, HEALTHY/DISCONNECTED only); Founder Action PARTIAL (`scheduling` checklist); Verification MISSING (no `calendar.*` definition). |
| 35 | Missing backend | `GoogleCalendarOAuthProvider`, `CALENDAR` secret type and role grants for the scheduling worker, push route with channel validation, renewal schedule, sync-token storage, `calendar.dedicated` definition. |
| 36 | Missing frontend | "Bookings calendar" card with staff sharing status; state-specific copy. |
| 37 | Difficulty | 2/5. |
| 38 | Order | Phase 1 (the checkpoint's named next task; prerequisite for Calendly correctness). |

## 4. HubSpot (CRM)

| # | Item | Finding |
|---|---|---|
| 1 | Provider | HubSpot |
| 2 | Business capability | `CRM`: contacts, companies, deals (jobs/quotes), tickets, activities, deal pipelines. |
| 3 | Recommended role | Default CRM for new businesses that want a real CRM. **Not launch-critical**: the residential-cleaning journey already has Company Brain lead/quote records, and HubSpot's Free tier limits (1 pipeline, 10 custom properties) undercut most setup automation. Phase 2. |
| 4 | Default for new businesses | Yes, as the recommended CRM once Phase 2 ships; "no CRM yet" remains an honest state. |
| 5 | KEEP alternatives | Yes. Jobber (field-service) is often the better system of record for cleaning; Pipedrive/Zoho are KEEP-eligible with no adapter (manual status). |
| 6 | Account ownership | Customer-owned portal (`hub_id`). Business Builder is an installed OAuth app. |
| 7 | Who creates account | Founder (Founder Action). |
| 8 | Who pays | Founder pays HubSpot directly. Business Builder developer account is free. |
| 9 | Authentication | OAuth 2.0 public app. Access token 30 min, refresh token non-expiring, non-rotating. |
| 10 | OAuth availability | Yes; mandatory for distribution. Legacy private-app creation is disabled 2026-09-28 (new portals) / 2026-10-26 (existing) ([changelog](https://developers.hubspot.com/changelog/legacy-private-app-creation-sunset)). |
| 11 | Scopes | `crm.objects.contacts.read/write`, `crm.objects.companies.read/write`, `crm.objects.deals.read/write`, `crm.schemas.deals.read/write` (pipelines are covered by deals scopes; there is **no** `crm.pipelines.read`), `crm.objects.owners.read`, optional `crm.objects.tickets.*`, `timeline.*` (partner-approval gated). Put Enterprise-only scopes in `optional_scope` or the install fails. |
| 12 | Token fallback | Service Keys (per-portal, pasted by admin) replace legacy private apps. API keys removed 2022. |
| 13 | Public app approval | Unlisted app works but is capped at **25 production installs** ([changelog](https://developers.hubspot.com/changelog/new-marketplace-distribution-app-install-limits)). Marketplace listing needs ≥3 unaffiliated active installs, OAuth-only auth, privacy/ToS/support URLs; 10 business days first response, ≤60 days cycle. |
| 14 | Partner/reseller approval | Not required. Timeline/app-events APIs require partner approval. |
| 15 | API gating | Free-tier portals have full API access. Sandboxes are Enterprise-only; developer test accounts (10, free) are the test surface. |
| 16 | API families | Date-versioned (`/crm/objects/2026-09/{objectType}`, batch ≤100, `.../search` 5 req/s and 10k results), associations, pipelines, properties, owners, `GET /crm/v3/limits/*`, `POST /oauth/2026-09/token`, `/token/introspect`, `/token/revoke`, `GET /account-info/v3/details`, `POST /crm/v3/objects/contacts/gdpr-delete`, `DELETE /appinstalls/2026-09/external-install`. OAuth v1 endpoints sunset 2027-02-16. |
| 17 | Webhooks | App-level subscriptions (`contact|company|deal|ticket.{creation|deletion|propertyChange|associationChange|merge|restore}`, `contact.privacyDeletion`). Signature v3: HMAC-SHA256 over method+uri+body+timestamp with client secret, base64, 5-minute window. Retries 10× over 24 h, at-least-once, unordered, ≤5 s response required. **No `app.uninstalled` webhook**; uninstall arrives only via the pull-based Webhooks Journal (`APP_LIFECYCLE_EVENT`, eventTypeId `4-1916193`). |
| 18 | Rate limits | Marketplace OAuth apps: **110 requests / 10 s per portal**, no daily cap documented, search excluded. The $500/mo API add-on does not raise OAuth-app limits. Headers `X-HubSpot-RateLimit-Max/-Remaining/-Interval-Milliseconds`; daily headers are not returned for OAuth. |
| 19 | Quota visibility | **No API usage endpoint for OAuth apps.** Client-side accounting against 110/10 s is required. `GET /crm/v3/limits/{records|pipelines|custom-properties}` gives founder-facing data-ceiling percentages. |
| 20 | Health signals | Token introspection (`active`, `hub_id`, `scopes[]`), refresh success/failure, account details (`portalId`, `accountType`, `dataHostingLocation`), limits percentages, rate-limit headers. Tokens leaked to public GitHub are auto-revoked. |
| 21 | Errors to normalize | 401 `EXPIRED_AUTHENTICATION` → refresh silently; `invalid_grant`/`BAD_REFRESH_TOKEN` → AUTH_EXPIRED "Business Builder was disconnected from HubSpot. Reconnect HubSpot."; 403 `MISSING_SCOPES` → PERMISSION_ERROR; 429 `RATE_LIMIT`/`RATE_LIMITS` `TEN_SECONDLY_ROLLING` → RATE_LIMITED; 429 `DAILY` → RATE_LIMITED with reset-at-midnight copy; 400 `VALIDATION_ERROR` → WARNING on the record, not the connection; 403 app-not-approved → ACTION_REQUIRED "Ask your HubSpot admin to approve Business Builder under Connected Apps."; 423 → retry after 2 s. |
| 22 | Safe automation | Upsert contacts/companies/deals via batch upsert on a custom unique property; create notes/tasks; associate records; create custom properties/groups; edit default pipeline stages; read owners, limits, account details; search with backoff. Probe tier first (Free = 1 pipeline, 10 custom properties). |
| 23 | Founder Action | Create HubSpot account; install the app (Super Admin or App Marketplace Access); choose/pay tier; connect their email inbox in HubSpot; approve scope additions; GDPR permanent deletes in UI. |
| 24 | Provider/authority action | Marketplace listing review; certification; timeline-events approval; data-center migration. |
| 25 | Evidence of setup | Introspection `active:true` with granted ⊇ required scopes; `portalId` matches; tier probe result; marked test contact round-trip then archived; expected pipeline stages present; a v3-signed webhook received. |
| 26 | Verification should check | `crm.lead_capture` (exists in catalog): lead-capture round-trip evidenced by provider receipt + webhook receipt, never by introspection alone. |
| 27 | Disconnect/revocation | Customer: Settings → Integrations → Connected Apps (up to 30 min to take effect; access tokens stay valid ≤30 min after uninstall). Business Builder: `POST /oauth/2026-09/token/revoke` + tombstone secret ref. Treat refresh failure as final. |
| 28 | Handoff | Nothing to hand back; data lives in the customer's portal. Developer Terms require deleting all customer content and tokens on termination. |
| 29 | Customer ownership | Full. |
| 30 | Compliance/security | Developer Terms prohibit training AI on customer data by default; DPA breach notification 48 h; `gdpr-delete` fires two webhooks; no EU API host (`dataHostingLocation` only affects UI/tracking URLs). |
| 31 | Cost structure | Free tier $0 (1,000 contacts, 2 users); Starter ~$20/seat/mo list; Pro $100/seat + $1,500 onboarding. |
| 32 | Who bears cost | Founder. Business Builder: $0. |
| 33 | Production prerequisites | Developer account; Projects platform app (v2026.03+); hosted OAuth with `hub_id`-keyed refs; webhook receiver with v3 validation, idempotency, ≤5 s; privacy/ToS/support URLs; plan for the 25-install cap and a Marketplace submission after 3 real installs. |
| 34 | Repo readiness | Adapter MISSING; OAuth flow PARTIAL (generic PKCE flow exists, HubSpot uses no PKCE, needs client-secret exchange); secret-ref PARTIAL; health MISSING; webhook verifier MISSING (no route); card PARTIAL; Founder Action PARTIAL (`crm` checklist exists); Verification PARTIAL (`crm.lead_capture` definition, no runner). `SandboxScopedKeyProvider` returns `CRM` and proves the ref path only. |
| 35 | Missing backend | `HubSpotOAuthProvider(OAuthProviderPort)`; `CAPABILITY_SECRET_TYPES` already has `crm.oauth` for `customer.*` capabilities but `_install_grants` skips non-EMAIL; inbound webhook route + v3 verifier; journal poller for uninstall; introspection-based reconcile; 110/10 s client-side limiter; tier probe. |
| 36 | Missing frontend | Provider-specific connect copy and scope list; tier-limit warning; KEEP/IMPROVE/REPLACE row already exists for `crm`. |
| 37 | Difficulty | 4/5 (install cap, platform mid-migration, tier-dependent failures, no quota API). |
| 38 | Order | Phase 2, after Google and Stripe Connect. |

## 5. Calendly (scheduling)

| # | Item | Finding |
|---|---|---|
| 1 | Provider | Calendly |
| 2 | Business capability | `SCHEDULING`: booking pages, single-use scheduling links, booking/cancel events. |
| 3 | Recommended role | Month-one inbound booking front door for solo founders. **Not the system of record** for crews, recurring cleans or service areas; Jobber is the recommended vertical successor. |
| 4 | Default for new businesses | Yes for v1 scheduling capability, with the plan-gate warning (Free tier has no webhooks). |
| 5 | KEEP alternatives | Yes. Jobber (public GraphQL API + OAuth; Draft apps limited to 5 paying accounts before App Review), Housecall Pro (OAuth partner-gated; API key requires customer on MAX plan), ZenMaid/Booking Koala/Launch27 (no usable public API; KEEP as manual status only). |
| 6 | Account ownership | Customer's Calendly organization. Business Builder developer account is separate. |
| 7 | Who creates account | Founder. |
| 8 | Who pays | Founder. Business Builder: $0. |
| 9 | Authentication | OAuth 2.1 with PKCE S256; access token 2 h; **single-use rotating refresh tokens (enforced since 2026-08-31)**; introspect and revoke endpoints at `auth.calendly.com`. |
| 10 | OAuth availability | Yes; no review or production-promotion step. Sandbox and Production are two self-created apps; production requires HTTPS redirect. |
| 11 | Scopes | Minimum: `users:read`, `event_types:read`, `scheduled_events:read`, `scheduling_links:write`, `webhooks:write` (plus `event_types:write` if Business Builder creates event types). 23 granular scopes; users can decline individual scopes; new apps get zero access until scopes are requested ([scopes](https://developer.calendly.com/docs/authentication/scopes)). |
| 12 | Token fallback | Personal Access Token (per-user, static, shown once, cannot be introspected). Single-customer fallback only. v1 API keys retired 2025-08-27. |
| 13 | Public app approval | None required. |
| 14 | Partner/reseller approval | None. Marketplace listing optional and sales-led. |
| 15 | API gating | Plan-gated: webhooks and `POST /invitees` need **Standard+** (Free → 403); activity log and data-compliance deletion are Enterprise-only. Pricing page says "Teams" for webhooks; developer docs and the API error say Standard. Gate on the 403, not on plan names. |
| 16 | API families | `GET /users/me`; `POST/PATCH /event_types` (solo 1:1 only; `custom_questions`, `is_paid`, `pooling_type` are read-only); `GET /event_type_available_times` (≤31 days); `GET /scheduled_events[/{uuid}/invitees]`; `POST /scheduled_events/{uuid}/cancellation`; `POST /scheduling_links` (`max_event_count=1`, unused links expire after 90 days); `POST /invitees` (Standard+, 100/day); `POST|GET|DELETE /webhook_subscriptions`; `GET /sample_webhook_data`. No reschedule endpoint; no group/round-robin creation; no payment or custom-question configuration. |
| 17 | Webhooks | `invitee.created`, `invitee.canceled` at `scope=organization` (org URI always required). Signature `Calendly-Webhook-Signature: t=…,v1=…`, HMAC-SHA256 over `t + "." + raw_body`, 3-minute tolerance; one signing key per app. **Subscriptions are permanently disabled after 24 h of failed delivery and there is no PATCH**; recovery is DELETE + POST. Reschedules arrive as cancel + create pairs. Payloads omit custom-question answers (one extra GET per booking). |
| 18 | Rate limits | Per user: 500/min paid, 50/min Free; 8 OAuth token mints per user per minute; `POST /invitees` 10/min, 50/h, 100/day on paid non-Enterprise. Headers `X-RateLimit-Limit/-Remaining/-Reset` on every response; no `Retry-After`. |
| 19 | Quota visibility | Good: rate-limit headers on all responses; `X-RateLimit-Limit` (50 vs 500) doubles as a paid-plan probe. No credits/balance concept. |
| 20 | Health signals | `GET /users/me`; `POST /oauth/introspect` (`active`, `scope`, `organization`); `GET /webhook_subscriptions/{uuid}` `state` ∈ `active|disabled` (poll daily); rate-limit headers; `GET /sample_webhook_data` for parser tests. Calendar connection inside Calendly is a blind spot; proxy via `user_busy_times` / available-times shape. |
| 21 | Errors to normalize | `invalid_grant` → AUTH_EXPIRED, clear tokens, "Calendly signed you out. Reconnect Calendly." (also fires when the founder changes their Calendly email, password or login method); 401 → AUTH_EXPIRED; 403 "upgrade to Standard" → BILLING_OR_CREDITS "Real-time booking notifications need a paid Calendly plan."; 403 `InsufficientScopeError` + `required_scopes` → PERMISSION_ERROR; 403 not-admin → ACTION_REQUIRED "Ask the Calendly account owner to connect."; 404 → WARNING and re-sync; 409 on webhook create → success; 429 → RATE_LIMITED using `X-RateLimit-Reset`; webhook `state:disabled` → ACTION_REQUIRED auto-repair then HEALTHY. |
| 22 | Safe automation | Resolve user/org; list event types and availability; create/update solo event types; create/verify/repair the org webhook; mint single-use links per lead; fetch invitee detail per webhook; cancel; mark no-show; refresh with a per-connection mutex; introspection checks. |
| 23 | Founder Action | Create account; upgrade to Standard+; **connect their calendar inside Calendly** (separate consent Business Builder cannot perform); set hours; configure payment collection and custom booking questions (address, beds/baths, access) in the UI; grant OAuth without declining scopes. |
| 24 | Provider/authority action | Re-issuing a lost client secret or signing key (support email). |
| 25 | Evidence of setup | Introspection scopes ⊇ required; `users/me` URI + org; `X-RateLimit-Limit == 500`; ≥1 active event type (record `is_paid`, `custom_questions`); org-scoped webhook with both events `state:active`; raw-body signature round-trip; a `scheduling_links` 201; availability shape not 24/7. |
| 26 | Verification should check | `scheduling.booking` (exists): booking round-trip and booking-conflict tests using provider receipts + the verified webhook; never Calendly UI screenshots alone. |
| 27 | Disconnect/revocation | Order: DELETE webhook subscriptions → `POST /oauth/revoke` → tombstone secret ref. Webhook fate on token revocation is UNVERIFIED, hence delete first. |
| 28 | Handoff | Nothing held; everything stays in the customer's org. |
| 29 | Customer ownership | Full. |
| 30 | Compliance/security | Invitee PII includes home address and access instructions; GDPR deletion API is Enterprise-only, so Business Builder must satisfy erasure from its own store; enforce PKCE, exact redirect match, raw-body HMAC. |
| 31 | Cost structure | Free $0 (no webhooks); Standard $10/seat/mo; Teams $16/seat/mo; Enterprise from $15k/yr. |
| 32 | Who bears cost | Founder. |
| 33 | Production prerequisites | Sandbox + production apps; capture secret and signing key into Secrets Manager at creation; refresh mutex; webhook state poller with auto-recreate; `X-RateLimit-Reset` backoff. Approval lead time zero. |
| 34 | Repo readiness | Adapter MISSING; OAuth flow PARTIAL (PKCE flow exists and matches Calendly's model well); secret-ref PARTIAL; health MISSING; webhook verifier MISSING (no route); card PARTIAL; Founder Action PARTIAL (`scheduling` checklist); Verification PARTIAL (`scheduling.booking` definition). |
| 35 | Missing backend | `CalendlyOAuthProvider`; single-use refresh rotation under the existing lease (the lease exists, rotation persistence does not); inbound webhook route with `t.`-body HMAC; daily webhook-state reconcile using the (currently undispatched) `reconciliation_schedule`; `SCHEDULING` secret types and grants. |
| 36 | Missing frontend | Plan-gate warning copy; "connect your calendar in Calendly" Founder Action card; KEEP/IMPROVE/REPLACE row exists (`scheduling`). |
| 37 | Difficulty | 2/5. |
| 38 | Order | Phase 1 candidate for scheduling because approval lead time is zero, but only after the Google Calendar dedicated-calendar model, which is what makes availability correct. |

## 6. Stripe Connect (customer-owned payments)

This is separate from Business Builder's own Stripe Checkout billing relationship with the founder (`commercial/stripe_test.py`, test mode only, `livemode` refused). Both live on the same Stripe platform account with no shared objects.

| # | Item | Finding |
|---|---|---|
| 1 | Provider | Stripe Connect |
| 2 | Business capability | `PAYMENTS`: the customer business collects card payments from its clients on its own Stripe account (payment links, checkout, invoices, subscriptions), with payouts to its own bank. |
| 3 | Recommended role | Default payments provider. Business Builder is a Connect platform using the **Stripe-owned pricing model**: connected account is merchant of record, pays Stripe fees, bears its own negative balances ([SaaS guide](https://docs.stripe.com/connect/saas)). |
| 4 | Default for new businesses | Yes. |
| 5 | KEEP alternatives | Yes: Square, PayPal, or an existing Stripe account created under another platform (OAuth to such accounts fails; use Account Links or KEEP as manual status). |
| 6 | Account ownership | Customer-owned connected account with full Stripe Dashboard. Create with controller defaults: `losses.payments=stripe`, `fees.payer=account`, `requirement_collection=stripe`, `stripe_dashboard.type=full` ([migrate to controller properties](https://docs.stripe.com/connect/migrate-to-controller-properties)). Legacy `type=standard|express|custom` are deprecated; Accounts v2 is preview-only and OAuth-incompatible, so build on v1 + controller properties and plan a v2 migration. `stripe_dashboard.type` is immutable. |
| 7 | Who creates account | Business Builder creates the Account object (empty controller defaults) and mints an Account Link; the founder completes Stripe-hosted onboarding. Existing accounts connect via Connect OAuth (`read_write`), keeping only `stripe_user_id`. |
| 8 | Who pays | Founder pays Stripe processing fees directly. Business Builder pays **$0** for Connect in this model (the $2/active-account and payout fees apply only to the buy-rate model). `application_fee_amount` optional; recommend 0 in v1. |
| 9 | Authentication | Platform secret key + `Stripe-Account: acct_…` header. No per-account token to store; OAuth `access_token`/`refresh_token` are deprecated. |
| 10 | OAuth availability | Yes for pre-existing accounts (`connect.stripe.com/oauth/authorize`, code single-use, 5-min expiry, second use revokes the connection). Account Links are Stripe's recommended path. |
| 11 | Scopes | OAuth `read_write`. Account Links: `type=account_onboarding`, `collection_options[fields]=eventually_due`. |
| 12 | Token fallback | None. Never request the customer's Stripe keys. |
| 13 | Public app approval | Platform profile must be completed before sandbox Connect use; live mode needs platform activation. Review turnaround UNVERIFIED (no published SLA). |
| 14 | Partner/reseller approval | Not required. |
| 15 | API gating | `account_update` links cannot be created for full-dashboard accounts (re-issue `account_onboarding`). KYC values are unreadable when Stripe collects them (desired). `account.external_account.updated` and `person.updated` are unavailable for full-dashboard accounts. |
| 16 | API families | `POST/GET /v1/accounts`, `POST /v1/account_links`, `GET /v1/balance`, `/v1/payouts`, `/v1/disputes`; direct charges via Checkout Sessions, Payment Intents, Payment Links, Invoices, Subscriptions created with the `Stripe-Account` header. Direct charges do not appear in platform exports. |
| 17 | Webhooks | Two endpoints with separate signing secrets: platform events (own billing) and **connected-account events** (`connect=true`, top-level `account` field). Key events: `account.updated` (primary health signal), `account.application.deauthorized`, `capability.updated`, `payout.failed/paid`, `charge.dispute.created`, `payment_intent.succeeded`. Check `livemode`. |
| 18 | Rate limits | 100 req/s live, 25 sandbox (not split read/write); account creation 30/s live, 5/s sandbox; 429 carries `Stripe-Rate-Limited-Reason`; a 429 without it is a `lock_timeout`. **Read allocation: ≤500 reads per transaction over rolling 30 days** aggregated across connected accounts, which penalizes polling-based monitoring; prefer webhooks. |
| 19 | Quota/balance visibility | `GET /v1/balance` with `Stripe-Account` → `available`, `pending`, `instant_available` (a real balance signal). Payouts, disputes, `requirements`. No API-quota endpoint. |
| 20 | Health signals | `details_submitted`, `charges_enabled`, `payouts_enabled`, `capabilities.card_payments/transfers`, `requirements.currently_due/past_due/pending_verification/current_deadline/errors`, `requirements.disabled_reason` (15 values), `future_requirements`. Re-retrieve the Account after `account.updated`; never trust the return redirect. |
| 21 | Errors to normalize | `account_invalid` / `platform_api_key_expired` → DISCONNECTED "We've lost access to your Stripe account. Reconnect Stripe."; `authentication_error`, `platform_account_required`, `not_allowed_on_standard_account` → operator-only (never blame founder); `rate_limit` → RATE_LIMITED; `lock_timeout` → retry; `testmode_charges_only`, `charge_disabled_for_account`, `capability_not_active` → ACTION_REQUIRED "Finish Stripe setup to accept real payments" with a fresh onboarding link; `payouts_not_allowed`, bank-account errors → ACTION_REQUIRED "Update your payout details in Stripe."; `disabled_reason: under_review` → WARNING "Stripe is reviewing your account; usually clears in days."; `disabled_reason: rejected.*` → ACTION_REQUIRED "Stripe has declined this account. Contact Stripe Support." (never retry, never promise a fix); `disabled_reason: requirements.past_due` → ACTION_REQUIRED with a deadline countdown (not BILLING_OR_CREDITS: nothing is owed, information is missing). |
| 22 | Safe automation | Create Account with controller defaults; prefill non-KYC fields before the first link; mint and refresh Account Links; consume `account.updated`; read balance/payouts/disputes; create products, prices, payment links, checkout sessions, invoices, subscriptions on the connected account; set branding; refunds as the connected account; test-mode verification suite using Stripe's documented test identities and trigger cards. |
| 23 | Founder Action | Everything inside Stripe-hosted onboarding: legal entity, EIN/SSN, DOB, addresses, ID documents, bank account, representative, Connected Account Agreement acceptance, country choice, responding to verification requests. Business Builder must never collect these. |
| 24 | Provider/authority action | KYC/AML verification, `under_review` holds, rejections, platform-profile review. |
| 25 | Evidence of setup | Stored `acct_…`; `details_submitted`, `charges_enabled`, `payouts_enabled` true; `currently_due` and `past_due` empty; `card_payments == active`; `disabled_reason == null`; a Payment Link/Checkout Session created on the connected account with a fetchable URL; a test charge; a signature-verified `account.updated` on the Connect endpoint. |
| 26 | Verification should check | `payment.test` (exists, EXTERNAL): the evidence above from provider receipts, plus a live-mode readiness gate that stays closed until the nine paid-pilot approvals are recorded. |
| 27 | Disconnect/revocation | `POST connect.stripe.com/oauth/deauthorize` (OAuth-linked) or the customer removes the platform in their Dashboard, or platform Dashboard → Remove account. `DELETE /v1/accounts` is not the mechanism. Listen for `account.application.deauthorized`. Account persists independently. |
| 28 | Handoff | Nothing to transfer; the customer already owns the account, history, customers and funds. Removal permanently resets platform controls. |
| 29 | Customer ownership | Full and verified by design. |
| 30 | Compliance/security | Hosted surfaces keep card data off Business Builder servers; annual PCI attestation in the Stripe Dashboard (specific SAQ level to confirm there); Stripe handles Connected Account Agreement acceptance; never email/SMS Account Links; separate webhook secrets; HTTPS `return_url`/`refresh_url`; US-only v1. |
| 31 | Cost structure | Customer: 2.9% + $0.30 domestic card, $15 dispute, Payment Links/Checkout included. Business Builder: $0; possible 0.25% referral revenue share. |
| 32 | Who bears cost | Founder for processing; Business Builder nothing. |
| 33 | Production prerequisites | Platform account live-activated; Connect enabled and platform profile complete; brand set; two webhook endpoints; HTTPS URLs; fee policy decided. Probe on 2026-09-15: the test-mode secret key in the Stromation `.env` reaches a US Standard account with charges and payouts enabled and Connect account listing returns 200 (no connected accounts); the live key is a restricted key that returns `more_permissions_required` for account reads, which is correct hygiene for that use. |
| 34 | Repo readiness | Adapter MISSING; OAuth PARTIAL (Stripe's OAuth has no PKCE; Account Links need a new "hosted onboarding" port shape); secret-ref PARTIAL (`CAPABILITY_SECRET_TYPES` has no payments entry, so the broker would deny); health MISSING; webhook verifier PARTIAL (`StripeTestPaymentProvider.verify_webhook` exists for our billing and can be reused for a second Connect endpoint); card PARTIAL; Founder Action PARTIAL (`payments` checklist); Verification PARTIAL (`payment.test`). |
| 35 | Missing backend | `StripeConnectProvider` with Account/Account Link creation, `account.updated` ingestion on a Connect-scoped webhook route, requirements→health mapping, balance signal, deauthorize; `PAYMENTS` secret type and grant policy; connected-account receipt idempotency; live-mode gate tied to paid-pilot release. |
| 36 | Missing frontend | Dedicated payout/KYC status panel (deadline countdown, `disabled_reason` copy), "Continue Stripe setup" button that mints a fresh link, clear separation from Business Builder billing card. |
| 37 | Difficulty | 3/5. |
| 38 | Order | Phase 1 in test mode (customer value is highest; no external approval beyond platform profile); live charges remain gated by the existing release gate. |

## 7. Google Analytics 4

| # | Item | Finding |
|---|---|---|
| 1 | Provider | Google Analytics 4 (Admin API v1beta/v1alpha, Data API v1beta, Measurement Protocol) |
| 2 | Business capability | `ANALYTICS`: site traffic, key events (lead form, call click, booking click), realtime tag-liveness check, dashboard reporting. Recommend splitting the capability into `ANALYTICS` (GA4) and `SEARCH` (Search Console) so two connections do not collide under one capability string. |
| 3 | Recommended role | Default web analytics. Business Builder creates the property and web stream **inside an account the customer already owns**, marks key events, sets 14-month retention, injects the tag on Business Builder-hosted sites, and reads reports. |
| 4 | Default for new businesses | Yes. |
| 5 | KEEP alternatives | Yes (Plausible, Fathom, existing GA4 property): KEEP as manual status; IMPROVE = add key events and retention to an existing property. |
| 6 | Account ownership | Customer's `ops@` Workspace identity is the GA account Administrator; founder's personal Google as second Administrator. **Business Builder must not create GA accounts**: the Provisioning API Program Agreement §1(g) limits `provisionAccountTicket` to internal use and forbids "publicly available applications", and §1(d) forbids charging customers in connection with promoting Google Analytics ([agreement](https://developers.google.com/analytics/terms/provisioning/en)). Counsel should confirm; until then account creation is a Founder Action (accept GA ToS in the UI). |
| 7 | Who creates account | Founder creates the GA account (UI, ToS acceptance); Business Builder creates the property, stream, key events. |
| 8 | Who pays | Nobody; GA4 standard is free. |
| 9 | Authentication | **Preferred:** customer adds Business Builder's own service-account email to the property as `predefinedRoles/editor` (setup) then `predefinedRoles/analyst` or `viewer` (steady state) via `properties.accessBindings` (v1alpha only; do it once in setup, then use stable v1beta Data/Admin APIs). Service-account-only access is an explicit exemption from OAuth verification ([sensitive-scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification)) and avoids all refresh-token failure modes. **Fallback:** customer OAuth with `analytics.edit` / `analytics.readonly`. |
| 10 | OAuth availability | Yes. Sensitive scopes, not restricted (no CASA). Verification typically 3–5 business days; compliant results lapse after 7 days if not published; Testing-status apps issue 7-day refresh tokens; 6-month dormancy and 100-tokens-per-client-per-user limits apply. |
| 11 | Scopes | `https://www.googleapis.com/auth/analytics.edit` (create property/stream/key events; also the documented scope for `provisionAccountTicket`, not `analytics.provision`), `analytics.readonly` (reports), `analytics.manage.users` only if Business Builder manages bindings on the customer's behalf. Do not request `analytics.provision`. |
| 12 | Token fallback | Service account via access binding (preferred), then OAuth. No API key. |
| 13 | Public app approval | OAuth verification if OAuth is used; none for the service-account pattern. |
| 14 | Partner/reseller approval | None. |
| 15 | API gating | `accessBindings` is v1alpha with a breaking-change warning; 100 Analytics accounts per Google Account (so a pool of service accounts is needed at scale; property-level binding cap UNVERIFIED). |
| 16 | API families | Admin: `accounts.list`, `properties.create`, `properties.dataStreams.create` (`WEB_DATA_STREAM`, `measurementId` output-only), `properties.keyEvents.create` (`conversionEvents` deprecated), `dataRetentionSettings` (`FOURTEEN_MONTHS`), `measurementProtocolSecrets.create` (requires `acknowledgeUserDataCollection`, a legal attestation the founder should click), `accounts.searchChangeHistoryEvents`, `properties.accessBindings.*` (v1alpha). Data: `runReport`, `runRealtimeReport` (last 30 min), `returnPropertyQuota`. Measurement Protocol `/mp/collect` for server-side lead events only (25 events/request, 130 KB, 72-hour backdate; cannot replace tagging; debug endpoint does not validate `api_secret`). |
| 17 | Webhooks | None for config or reports (only v1alpha audience-export operations). Poll `searchChangeHistoryEvents` (max 200/page, 2-year retention, subset of changes). |
| 18 | Rate limits | Data API per property: 200k tokens/day, 40k/hour, 14k/project/hour, 10 concurrent, **10 server errors per project per hour then blocked for the hour** (never hammer on 5xx). Admin: 1,200 req/min, 600/min/user, 600 writes/min. |
| 19 | Quota visibility | Best of the Google set: `returnPropertyQuota: true` returns `consumed`/`remaining` for every bucket ([PropertyQuota](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/PropertyQuota)); resets midnight PST. |
| 20 | Health signals | Property and stream exist; `webStreamData.defaultUri` host equals the customer domain; **`runRealtimeReport` shows `activeUsers > 0` after tag install (the only programmatic tag-liveness signal that exists)**; key events non-empty; retention = 14 months; quota remaining trend; change history shows Business Builder's binding still present. |
| 21 | Errors to normalize | 401 `UNAUTHENTICATED` → AUTH_EXPIRED; 403 `PERMISSION_DENIED` → PERMISSION_ERROR "Business Builder isn't listed as a user on this Analytics property. Add the Business Builder analytics identity as an Analyst."; 429 `RESOURCE_EXHAUSTED` → RATE_LIMITED "Analytics hit its daily reporting limit; numbers refresh after midnight Pacific."; 400 `INVALID_ARGUMENT` → internal WARNING; 5xx → PROVIDER_OUTAGE with backoff. Branch on status and `propertyQuota`, never on message text. |
| 22 | Safe automation | Create property, web stream, key events; set retention; rotate MP secrets; inject gtag on Business Builder-hosted pages; run reports; poll realtime after install. |
| 23 | Founder Action | Create GA account and accept ToS; add the Business Builder identity (or grant OAuth); click `acknowledgeUserDataCollection`; install the snippet on a customer-hosted CMS. |
| 24 | Provider/authority action | None. |
| 25 | Evidence of setup | `G-` measurement ID; stream URI host match; ≥1 realtime hit observed after install; ≥1 key event; retention 14 months. Config without traffic is the top silent failure. |
| 26 | Verification should check | Realtime hit and key-event configuration from provider receipts; `website.deployed` may reference the tag but must not mark analytics done without traffic. |
| 27 | Disconnect/revocation | Delete Business Builder's access binding or revoke OAuth; nothing else to do. |
| 28 | Handoff | Cleanest of all: the customer already owns account, property and data. |
| 29 | Customer ownership | Full. |
| 30 | Compliance/security | No PII in event params; user-data-collection acknowledgement is the founder's attestation; UA APIs are dead (ignore any `ga:`/views documentation). |
| 31 | Cost | Free. |
| 32 | Who bears cost | Nobody. |
| 33 | Production prerequisites | GCP project, service-account pool with rotation, published OAuth consent screen only if OAuth is used, domain verified in Search Console for brand verification. |
| 34 | Repo readiness | Adapter MISSING; OAuth PARTIAL; secret-ref PARTIAL (service-account key belongs in Secrets Manager under the existing exact-ARN store; no `ANALYTICS` secret type); health MISSING; webhooks n/a; card PARTIAL (capability string exists); Founder Action MISSING; Verification MISSING (no `analytics.*` definition). The Stromation `.env` holds a GA4 service-account JSON and property/measurement IDs for Stromation's own site (parses as a service account; not verified against the API in this pass). |
| 35 | Missing backend | `GoogleAnalyticsProvider` (service-account mode + optional OAuth mode), property/stream/key-event setup job, realtime liveness check, `ANALYTICS` capability grants and secret type, `SEARCH` capability split. |
| 36 | Missing frontend | "Add this identity to your Analytics property" instruction card; realtime "tag detected" status; report card. |
| 37 | Difficulty | 2/5. |
| 38 | Order | Phase 2 (valuable, cheap, but not launch-critical; depends on the website being live). |

## 8. Google Search Console

| # | Item | Finding |
|---|---|---|
| 1 | Provider | Google Search Console (`searchconsole` API: `webmasters/v3` + `v1` URL inspection) and Site Verification API v1 |
| 2 | Business capability | `SEARCH` (recommended new capability; today it would collide with GA4 under `ANALYTICS`): property registration, ownership verification, sitemap submission, indexing status, organic performance reporting. |
| 3 | Recommended role | Default. Domain property (`sc-domain:`) verified by DNS TXT placed through the customer's Cloudflare zone token when Business Builder holds one; otherwise URL-prefix property verified by META/FILE on a Business Builder-hosted site; last resort a founder-placed DNS record. |
| 4 | Default for new businesses | Yes. |
| 5 | KEEP alternatives | Bing Webmaster Tools as an add-on; existing GSC property = KEEP (add Business Builder as delegated owner). |
| 6 | Account ownership | Customer's `ops@` identity is the verified owner; Business Builder is a delegated owner (added through `webResource.update`, no acceptance handshake, all owners emailed) or reads through the ops@ OAuth token. |
| 7 | Who creates | Business Builder can `sites.add` and verify when it controls DNS or hosting; otherwise founder places the token. |
| 8 | Who pays | Nobody; free. |
| 9 | Authentication | OAuth (customer) with `webmasters` / `webmasters.readonly` and `siteverification` (full scope needed to list/update owners; `siteverification.verify_only` cannot read existing sites). Service accounts can be added as owners/users too (email identity), which avoids refresh-token rot the same way as GA4 (UNVERIFIED for delegated-owner via API with a service-account email; verify in staging). |
| 10 | OAuth availability | Yes; sensitive, not restricted. |
| 11 | Scopes | `https://www.googleapis.com/auth/webmasters`, `.../webmasters.readonly`, `.../siteverification`. |
| 12 | Token fallback | Service-account email as delegated owner; no API key. |
| 13–15 | Approvals/gating | OAuth verification only. `sites.add` does **not** verify; the property lists as `siteUnverifiedUser` and every data call 403s until Site Verification succeeds ([sites.add](https://developers.google.com/webmaster-tools/v1/sites/add), [answer/34592](https://support.google.com/webmasters/answer/34592)). Domain properties are DNS-only. |
| 16 | API families | `sites.add/get/list/delete`, `sitemaps.submit/list/get/delete`, `searchanalytics.query` (dimensions incl. `hour`; `rowLimit` 1–25,000; 50k rows/day/type ceiling; 2–3 day lag; 16 months), `urlInspection.index.inspect`; Site Verification `getToken` (`DNS_TXT`/`DNS_CNAME` for `INET_DOMAIN`; `FILE`/`META`/`ANALYTICS`/`TAG_MANAGER` for `SITE`), `insert`, `get/list/update/delete`. |
| 17 | Webhooks | None; polling only. Email alerts are UI-only. |
| 18 | Rate limits | Search Analytics 1,200 QPM per site and per user; URL Inspection **2,000 QPD and 600 QPM per site (not raisable)**; other endpoints 20 QPS / 200 QPM per user; unpublished short-term (10-minute) and long-term (daily) load limits can 429 below those numbers. Site Verification quotas unpublished. |
| 19 | Quota visibility | None. Client-side accounting; treat 429 as authoritative. |
| 20 | Health signals | `sites.get.permissionLevel ∈ {siteOwner, siteFullUser}` (alert on `siteUnverifiedUser`); sitemap `errors: 0` and recent `lastDownloaded`; homepage inspection verdict `PASS`; 28-day query non-empty (new sites legitimately return nothing for weeks: say so, do not flag). |
| 21 | Errors to normalize | 401 `expired`/`authError` → AUTH_EXPIRED; 403 `insufficientPermissions`/`forbidden` → PERMISSION_ERROR "This site isn't verified yet or Business Builder was removed as an owner."; 403/429 `rateLimitExceeded`/`quotaExceeded`/`dailyLimitExceeded` → RATE_LIMITED "We're checking pages faster than Google allows; results finish shortly."; 404 → ACTION_REQUIRED "Add and verify this property." Do not match the unpublished "User does not have sufficient permission for site" text. |
| 22 | Safe automation | `sites.add`, `getToken`, DNS TXT via Cloudflare token, `insert`, sitemap submit, reads, delegated-owner add/remove. |
| 23 | Founder Action | Placing DNS TXT at a registrar Business Builder cannot reach; META tag on a customer-hosted CMS. |
| 24 | Authority action | Indexing decisions and manual actions are Google's; never promise indexing. |
| 25 | Evidence of setup | `permissionLevel` owner/full; sitemap `errors: 0`; homepage verdict `PASS`; first non-zero impressions row. |
| 26 | Verification should check | `domain.ownership` (exists; `dns-control` test + `DNS_OBSERVATION` evidence) can consume the verified-owner state and the TXT observation as provider receipts. |
| 27 | Disconnect/revocation | Remove Business Builder's verification token **first**, then remove it from `owners[]` (otherwise Google silently re-verifies). Revoke OAuth. |
| 28 | Handoff | Customer's ops@ remains verified owner throughout; nothing to transfer. |
| 29 | Customer ownership | Full. |
| 30 | Compliance/security | Anonymized queries mean query tables never sum to totals (footnote in the dashboard); `page`+`query` together drops data; `searchAppearance` enum values were removed Jan 2026 (never hardcode). |
| 31 | Cost | Free. |
| 32 | Who bears cost | Nobody. |
| 33 | Production prerequisites | OAuth verification (shared with Google Workspace/Calendar app or a separate low-scope client), Cloudflare DNS token flow, sitemap generation on Business Builder-hosted sites. |
| 34 | Repo readiness | Adapter MISSING; OAuth PARTIAL; secret-ref PARTIAL; health MISSING; webhooks n/a; card PARTIAL (collides with GA4 under `ANALYTICS`); Founder Action PARTIAL (`domain` checklist); Verification PARTIAL (`domain.ownership`, `website.*` definitions exist without runners). The Stromation `.env` has a GSC site URL and a GSC service-account variable whose value did not parse as JSON (not verified). |
| 35 | Missing backend | `SearchConsoleProvider` + Site Verification adapter; DNS TXT placement via the Cloudflare adapter; sitemap submission job; `SEARCH` capability + secret type; 2,000/day inspection budgeter for a curated key-page list. |
| 36 | Missing frontend | Verification-state card, 28-day clicks/impressions/position with anonymized-query footnote, sitemap status, key-page index list. |
| 37 | Difficulty | 3/5. |
| 38 | Order | Phase 2, together with GA4 and after Cloudflare DNS. |

## 9. QuickBooks Online

| # | Item | Finding |
|---|---|---|
| 1 | Provider | QuickBooks Online (Accounting API v3, OAuth 2.0 + OIDC, CloudEvents webhooks) |
| 2 | Business capability | `ACCOUNTING`: customers, service items, invoices/estimates and sending them, payment status read-back, read-only reports (P&L, A/R aging, customer balance). |
| 3 | Recommended role | Default bookkeeping starter. Business Builder operates the mechanics (customer → invoice from a founder-approved template → send) and never decides where money is classified. Intuit's terms do not forbid advice; the no-advice line is Business Builder's own liability decision (Developer Terms §20.5 disclaims Intuit's advice; §11.8 makes the app an agent of the user). |
| 4 | Default for new businesses | Yes, but the adapter is Phase 3; Phase 1 records "bookkeeping: QuickBooks (manual)" as an honest Founder Action. |
| 5 | KEEP alternatives | Yes: Xero, Wave, FreshBooks, or a bookkeeper's system (manual status). |
| 6 | Account ownership | Customer owns the QBO company (`realmId`) and subscription. Connecting user must be a company admin. |
| 7 | Who creates account | Founder. |
| 8 | Who pays | Founder pays the plan. Business Builder pays Intuit **platform program fees**: Builder tier $0 with a hard cap of 500,000 metered "CorePlus" read calls per month after which calls are **blocked**; Silver $300/mo for uncapped reads and Marketplace listing ([partner FAQ](https://developer.intuit.com/app/developer/qbo/docs/get-started/partner-faq)). Writes are unmetered. |
| 9 | Authentication | OAuth 2.0 authorization code (confidential client). Access token 3,600 s; refresh token 100 days rolling and **rotates roughly daily with a 24-hour overlap** (always persist the newest); **5-year hard cap** (send `x-include-refresh-token-hard-expires-in: true`; first mass expiries Oct 2028). Discovery `https://developer.api.intuit.com/.well-known/openid_configuration`; revoke `https://developer.api.intuit.com/v2/oauth2/tokens/revoke`. |
| 10 | OAuth availability | Yes. **Mandatory app settings:** Host domain, Launch URL, Disconnect URL, Reconnect URL (required since 2026-02-24). The Disconnect URL is a browser landing page, not a server callback. |
| 11 | Scopes | `com.intuit.quickbooks.accounting` only. Do not request `com.intuit.quickbooks.payment` (drags in PCI/PA-DSS obligations). Scopes can be added but never removed; changes force re-consent. |
| 12 | Token fallback | None. |
| 13 | Public app approval | **App Assessment Questionnaire is mandatory for production keys even for unlisted apps**; the "Show Credentials" toggle appears only after approval. Prerequisites: public EULA and privacy policy, support contact, hosting geolocation, accepted-connection countries, sanctions attestation, business disclosures. No published SLA (plan 1–3 weeks, UNVERIFIED). Marketplace listing (technical 10 bd, security up to 30 bd, marketing 5 bd) is optional and needs Silver. |
| 14 | Partner/reseller approval | Not required. Annual security re-review applies to any app with more than 500 connections; findings must be fixed within 2 weeks. |
| 15 | API gating | Sales tax cannot be enabled via API; AST overrides app-set tax codes; the books-close date is invisible to the API; sandboxes are Plus/Advanced only (customers often run Simple Start, so test degradation deliberately). |
| 16 | API families | `https://quickbooks.api.intuit.com/v3/company/{realmId}/...?minorversion=75` (75 is both floor and current); `query` (max 1,000 per page); `batch` (30 payloads recommended max; error 1040 states the real limit); `cdc` (30-day lookback, 1,000 objects; reconciliation backstop); reports (`ProfitAndLoss`, `BalanceSheet`, `CustomerBalance`, `AgedReceivables`, `ItemSales`); `POST /invoice/{id}/send?sendTo=`; entities Customer, Item (service items require `IncomeAccountRef`), Invoice (one `CustomerRef`; references cannot be created inline), Estimate, Payment, Preferences (`TaxPrefs.UsingSalesTax`, `TaxPrefs.PartnerTaxEnabled`, `SalesFormsPrefs.CustomTxnNumbers`), CompanyInfo (`SubscriptionStatus`). |
| 17 | Webhooks | Per-app subscription in the portal, separate dev/prod endpoints and verifier tokens, apply to all realms. **CloudEvents v1.0 payloads** (`type: qbo.invoice.created.v1`, `intuitentityid`, `intuitaccountid` = realmId, `time` is the source of truth; IDs only; one array can span multiple realms). Header `intuit-signature` = base64 HMAC-SHA256 of the raw body with the verifier token. Respond 200 within 3 s; retries at 10 s, 20 s, 30 s, 5 m, 20 m, 2 h, 4 h, 6 h then every 6 h; a wedged endpoint stalls the stream. No webhook for TaxCode/TaxRate. |
| 18 | Rate limits | 500 req/min per realm; 10 req/s per realm+app (a rate, not a concurrency limit); batch 40/min per realm+app; 120 s request timeout; 429 with **no `Retry-After`** (wait 60 s); sandbox 40 emails/day. Not purchasable. |
| 19 | Quota visibility | **None** via API; CorePlus consumption is visible only in the developer portal. Business Builder must count its own calls and alert at 70% and 85% of 500,000. Status page `status.developer.intuit.com` separates Accounting API, Webhooks and Sandbox. |
| 20 | Health signals | Refresh success; refresh-token runway (<14 days → prompt reconnect); `GET .../companyinfo/{realmId}`; **`CompanyInfo.SubscriptionStatus`** (`TRIAL`, `TRIALOPTIN`, `SUBSCRIBED` = read/write; `RESTRICTED`, `SUSPENDED`, `EXPIRED`, `CANCELLED` = read-only, writes fail 6190; data deleted 90/390 days after expiry/cancel, then 7001); Preferences snapshot; an active service item exists; last verified webhook timestamp per realm. |
| 21 | Errors to normalize | `invalid_grant` on refresh → AUTH_EXPIRED "Your QuickBooks connection has ended. Reconnect to keep invoicing."; 403 `ApplicationAuthorizationFailed` → DISCONNECTED "QuickBooks was disconnected from this business" (primary disconnect detector; there is no callback); 401 on API → silent refresh once; 3200 → refresh then re-consent; 3100 → PERMISSION_ERROR; 120 → ACTION_REQUIRED "The person who connected QuickBooks is no longer an admin there."; 140/150 → PROVIDER_OUTAGE; 429 → RATE_LIMITED (60 s); 5xx/10000 → PROVIDER_OUTAGE; 5010 stale `SyncToken` → re-read and retry once; 6000 → surface `Detail` rewritten; 6140 duplicate DocNumber → check `CustomTxnNumbers` first; **6190 → BILLING_OR_CREDITS "QuickBooks is read-only because of the QuickBooks subscription. Fix billing in QuickBooks."**; 6200/6210 closed period → ACTION_REQUIRED "Your books are closed for that period; changes must be made in QuickBooks."; 6240 duplicate DisplayName (unique across Customer, Vendor and Employee); 610/2500/6250 inactive reference → WARNING; 7001 → DISCONNECTED (company deleted). A `200 OK` can still carry a `<Fault>`; always parse. Log `intuit_tid`. |
| 22 | Safe automation | Read CompanyInfo/Preferences; cache Customer/Item/Account/TaxCode lists via webhooks plus daily CDC; create/update Customers from Company Brain; create Invoices/Estimates from a founder-approved template using a **founder-chosen** existing Item; send by email; read Payments; pull daily precomputed report tiles; void invoices Business Builder created on explicit request; revoke on offboarding. |
| 23 | Founder Action | Choose the income account for any new service item; anything touching the chart of accounts; enabling sales tax (API cannot); taxability of lines; expense categorization; closing the books and supplying the close date; deletions, merges, inactivations; invoice numbering mode; enabling QuickBooks Payments; fixing a `RESTRICTED`/`SUSPENDED` subscription; re-consent before expiry. |
| 24 | Provider/authority action | Intuit questionnaire approval and reviews; Payments underwriting; AST rate computation; CPA judgment on classification and close. |
| 25 | Evidence of setup | `realmId` and environment persisted; `CompanyName`/`LegalName`/`Country` confirmed by the founder (wrong-company check); `SubscriptionStatus` read/write; one refresh round-trip with the new token persisted and expiry runway recorded; at least one active service item with its income account explicitly confirmed; sandbox invoice created and `EmailStatus == EmailSent`; a CloudEvents webhook received, HMAC-verified, acked within 3 s, `intuitaccountid` matched; disconnect/reconnect rehearsed; Preferences snapshot; books-close date captured; CorePlus counter initialized. |
| 26 | Verification should check | No `accounting.*` definition exists; add one that requires the evidence above from provider receipts and a founder attestation for the income-account choice. |
| 27 | Disconnect/revocation | Customer: QBO Apps → Disconnect (browser lands on the Disconnect URL with `?realmId=`; the server learns via 403 or `invalid_grant`). Business Builder: the in-product disconnect must call the revoke endpoint (technical requirement 2.3); signing out is not disconnecting. |
| 28 | Handoff | Nothing to transfer; data stays in the customer's books. Warn on `CANCELLED`/`EXPIRED` because Intuit deletes the company after 90/390 days. Retain nothing beyond functional need. |
| 29 | Customer ownership | Full. |
| 30 | Compliance/security | TLS 1.2+, `Cache-Control: no-store`, TRACE disabled, never log QBO data or credentials, refresh token and realmId encrypted with a separately stored key (Secrets Manager satisfies), `state` anti-CSRF, 302 not HTML on token-bearing URLs, 99.95% availability requirement, GDPR/CCPA as independent controller (§12.4). **§11.7 binds LLM use to NIST AI standards**, and the security review forbids giving third parties access to QuickBooks data, which constrains sending QBO data to an external model provider without a functional justification and a no-training agreement. Cross-customer insights only aggregated and anonymized; no benchmarking of the platform. |
| 31 | Cost structure | Customer: Simple Start $38, Essentials $85, Plus $140, Advanced $340 list per month (2026-09-15); QuickBooks Payments 2.99% card, 1% ACH ($1 min; cap UNVERIFIED). Business Builder: $0 rising to $300/mo at roughly 550 active founders with a modest dashboard (30 reads per founder per day) unless reads are cached. |
| 32 | Who bears cost | Founder for QBO; Business Builder for program fees. |
| 33 | Production prerequisites | Public EULA and privacy policy; four app URLs; production redirect URIs; TLS/cache/TRACE posture; encrypted token store; connect/disconnect/reconnect rehearsed; CloudEvents webhook handler; questionnaire approved. Realistic 3–6 weeks from a working sandbox app, unlisted. |
| 34 | Repo readiness | Adapter MISSING; OAuth PARTIAL (no PKCE, client-secret exchange; the daily-rotating refresh token fits the existing lease but needs rotation persistence); secret-ref PARTIAL (no `ACCOUNTING` secret type); health MISSING; webhook verifier MISSING (no route); card PARTIAL; Founder Action MISSING (no bookkeeping checklist in the live journey); Verification MISSING. |
| 35 | Missing backend | `QuickBooksProvider`; realm-keyed token rotation; CloudEvents webhook route with HMAC; daily CDC reconcile; SubscriptionStatus → health mapping; CorePlus call counter; cache-first read layer; income-account Founder Action with attestation evidence; in-product disconnect calling revoke. |
| 36 | Missing frontend | Approved "Connect to QuickBooks" button asset and hide-after-connect rule; company-confirmation step; income-account chooser (read-only list, founder selects); read-only mode banner for `RESTRICTED`/`SUSPENDED`; reconnect page at the registered Reconnect URL. |
| 37 | Difficulty | 4/5. |
| 38 | Order | Phase 3 (customer demand), after Stripe Connect covers getting paid; start the questionnaire prerequisites (legal pages, URLs) in Phase 2 because they are shared with other providers. |

## 10. Google Business Profile

| # | Item | Finding |
|---|---|---|
| 1 | Provider | Google Business Profile (seven APIs; reviews/media/posts still on legacy v4.9) |
| 2 | Business capability | `LOCAL_PRESENCE`: location data, hours, service areas, photos, posts, review monitoring and founder-approved replies, performance metrics. |
| 3 | Recommended role | Default local-presence provider and the highest-leverage channel for residential cleaning, **with a human-in-the-loop floor**: Google policy requires the owner to initiate verification, and video verification (Google's usual method for service-area businesses) has no API representation at all. |
| 4 | Default for new businesses | Yes, as a Founder Action-driven setup with API-backed management after verification. |
| 5 | KEEP alternatives | Existing verified profile = KEEP (invite Business Builder's Organization as Manager). Yelp/Nextdoor are additive, no adapter. |
| 6 | Account ownership | Customer is primary owner. Business Builder holds **Manager** through an invitation to a Business Builder **Organization** account (Manager can edit info, photos, posts, reply to reviews; cannot add/remove users, delete the profile or transfer ownership) ([answer/3403100](https://support.google.com/business/answer/3403100)). Organization identities must be clean Google accounts that own or manage no profiles themselves. |
| 7 | Who creates | Founder creates the location in the dashboard (identity and policy reasons), even though `accounts.locations.create` exists. |
| 8 | Who pays | Nobody; free. Business Builder must disclose in writing that Business Profile is free and disclose its own fee. |
| 9 | Authentication | OAuth `https://www.googleapis.com/auth/business.manage` on the Business Builder Organization identity (preferred; survives founder password changes; revocable by the customer in their UI) or on the owner's account (legacy fallback). |
| 10 | OAuth availability | Yes; sensitivity label UNVERIFIED (not restricted; no CASA). Two separate reviews: OAuth verification and GBP API access. |
| 11 | Scopes | `business.manage` only. |
| 12 | Token fallback | None. |
| 13 | Public app approval | **GBP API access application** ([form](https://support.google.com/business/contact/api_default)): quota is 0 QPM and the APIs are invisible in Cloud Console until approved; requires a verified profile active 60+ days with a website, submitted from a domain-matched owner/manager email; reviewed within ~14 days; agencies must register an Organization. |
| 14 | Partner/reseller approval | Organization account (agency) registration. |
| 15 | API gating | **No sandbox at all** (only `validateOnly` on some methods). Q&A API discontinued 2025-11-03 with no replacement. Demo account must be provided to Google within 7 days on request. |
| 16 | API families | Account Management v1.1 (`accounts.admins.create` = invitation; `invitations.list/accept` as the invitee; `locations.transfer`); Business Information v1 (`locations.patch` with mandatory `updateMask`, `serviceArea.businessType = CUSTOMER_LOCATION_ONLY`, ≤20 `placeInfos`, `regionCode` immutable, omit `storefrontAddress`, `serviceItems`, categories, attributes, hours); Verifications v1 (`fetchVerificationOptions`, `verify`, `getVoiceOfMerchantState`; methods `ADDRESS`, `EMAIL`, `PHONE_CALL`, `SMS`, `AUTO`, `VETTED_PARTNER`; **no VIDEO**); Notifications v1.2 (Pub/Sub); Performance v1 (`fetchMultiDailyMetricsTimeSeries`, 18-month retention); legacy v4 reviews (`list`, `updateReply` ≤4,096 bytes, `ReviewReplyState`, `PolicyViolation` fields added 2026), media (`startUpload`), local posts. |
| 17 | Webhooks | Pub/Sub notifications: grant `mybusiness-api-pubsub@system.gserviceaccount.com` `pubsub.topics.publish`, then `updateNotificationSetting` with `NEW_REVIEW`, `UPDATED_REVIEW`, `GOOGLE_UPDATE`, `NEW_CUSTOMER_MEDIA`, `DUPLICATE_LOCATION`, `VOICE_OF_MERCHANT_UPDATED` (Q&A types and `LOSS_OF_VOICE_OF_MERCHANT` deprecated). |
| 18 | Rate limits | 300 QPM per API after approval; **Create Location 300 QPD; SearchGoogleLocation 300 QPD; edits 10/min per profile, non-increasable**. Quota increases are denied for spiky usage, so nightly batch syncs must be smoothed. |
| 19 | Quota visibility | None; 0 QPM in Cloud Console means not approved. Client-side accounting. |
| 20 | Health signals | `getVoiceOfMerchantState`: `hasVoiceOfMerchant`, `hasBusinessAuthority`, and exactly one of `waitForVoiceOfMerchant`, `verify{hasPendingVerification}`, `resolveOwnershipConflict`, `complyWithGuidelines{recommendationReason ∈ BUSINESS_LOCATION_SUSPENDED|BUSINESS_LOCATION_DISABLED}`; `hasPendingEdits` / `getGoogleUpdated`; category, hours, website, ≥1 photo; `VOICE_OF_MERCHANT_UPDATED` push. |
| 21 | Errors to normalize | 403 `PERMISSION_DENIED` pre-approval → UNKNOWN/operator "Business Builder's Business Profile access is pending Google approval."; 403 Workspace-disabled → ACTION_REQUIRED "Your Google Workspace admin has Business Profile turned off."; 429 `RESOURCE_EXHAUSTED` → RATE_LIMITED; `BUSINESS_LOCATION_SUSPENDED` → ACTION_REQUIRED "Google has suspended this profile. Only you can appeal, using Google's appeal form." (no API to view or appeal); `verify.hasPendingVerification` → WARNING "Google is still reviewing your verification."; `ErrorDetail` codes (`LAT_LNG_TOO_FAR_FROM_ADDRESS`, `CATEGORY_NOT_VERIFIED`, `MISSING_DEPENDENT_FIELD`, `PHOTO_UPLOAD_FAILED`) → field-level WARNING with plain copy; send `X-GOOG-API-FORMAT-VERSION: 2`. |
| 22 | Safe automation | After the founder's per-feature express consent: patch hours/phone/website/categories/attributes/service items/service areas; upload photos; create posts; read reviews and performance; post **founder-approved** replies; accept invitations to the Organization; poll VoM state. |
| 23 | Founder Action | Create the profile; **initiate and complete verification** (policy: "verification options can only be initiated by a direct request from the owner"; video is live, in-app, from the address); invite the Business Builder Organization as Manager; approve each AI-drafted review reply (verbal consent insufficient; keep proof); appeal suspensions; supply a findable address even for a hidden-address service-area business. |
| 24 | Provider/authority action | Voice of Merchant grant, verification outcome, suspension/reinstatement, duplicate merges, API access approval. |
| 25 | Evidence of setup | Location name; `hasVoiceOfMerchant == true`; primary category; hours; `websiteUri`; ≥1 photo; `CUSTOMER_LOCATION_ONLY` with 1–20 places; no storefront address; Manager admin present with no pending invitation. |
| 26 | Verification should check | `hasVoiceOfMerchant` on every health sweep (it can flip to false at any time); consent record for review-reply automation. |
| 27 | Disconnect/revocation | Remove Business Builder's Manager admin, revoke OAuth, purge cached GBP content; policy requires full disassociation within 7 business days and 48-hour notice of account changes. |
| 28 | Handoff | Customer is already primary owner; nothing to transfer. Never take ownership of an existing profile. |
| 29 | Customer ownership | Full. |
| 30 | Compliance/security | **30-day content storage limit and no aggregation/manipulation** of GBP content (forbids a persistent review archive or historical review analytics; decide the feature deliberately); no automated replies without prior specific express consent; no indirect API access for end users; one profile per service-area business (profile-per-city is a suspension trigger); no radius service areas; ~2-hour driving-time guideline; P.O. boxes ineligible; no "Google" in the org name; fee disclosure on invoices. |
| 31 | Cost | Free. |
| 32 | Who bears cost | Nobody. |
| 33 | Production prerequisites | A Business Builder-owned verified profile active 60+ days with a domain-matched email to apply; Organization account with clean identities; API access approval (~14 days); OAuth verification; Pub/Sub topic; consent-capture UI. |
| 34 | Repo readiness | Adapter MISSING; OAuth PARTIAL; secret-ref PARTIAL; health MISSING; webhooks MISSING (no Pub/Sub consumer); card PARTIAL (`LOCAL_PRESENCE` exists); Founder Action MISSING in the live journey (GBP verification appears only in marketing content); Verification MISSING. |
| 35 | Missing backend | `BusinessProfileProvider` (Org identity), invitation acceptance flow, VoM health reconcile, Pub/Sub consumer, review-reply approval action with consent record, 30-day cache purge, smoothed rate budget. |
| 36 | Missing frontend | Verification Founder Action card with video-verification guidance; consent checkbox for review-reply automation; review inbox with approve/edit/decline; suspension appeal card. |
| 37 | Difficulty | 5/5. |
| 38 | Order | Phase 3 for API-backed management (start the access application in Phase 1 because it is the long pole); the verification Founder Action ships in Phase 1 as a manual step with evidence. |

## 11. Private S3 evidence storage

Not a founder-facing Connection. It is Business Builder infrastructure; the founder sees evidence status, never a connection card.

| # | Item | Finding |
|---|---|---|
| 1 | Provider | AWS S3 (+ KMS, CloudTrail, EventBridge) |
| 2 | Business capability | Private, tenant-isolated, integrity-checked, immutable-after-acceptance storage for Founder Action evidence and brokered artifacts. |
| 3 | Recommended role | The only evidence store. Single bucket, prefix per tenant/company: `tenants/{tenant}/companies/{company}/evidence/{evidence_id}/{sha256}.{ext}`. The existing broker key layout `tenant/{t}/company/{c}/artifacts/{id}/{hash}` is compatible and can stay. |
| 4 | Default | Yes, always. |
| 5 | KEEP alternatives | No. |
| 6 | Account ownership | Business Builder's AWS account. Customers own the content; export on handoff. |
| 7 | Who creates | Business Builder infra. |
| 8 | Who pays | Business Builder. |
| 9 | Authentication | ECS task role. Per-tenant scoping via STS session policies (≤2,048 chars) or ABAC with `aws:PrincipalTag/tenant_id`; the current adapter enforces an exact allowed prefix in code and the staging task policy lists literal prefixes. |
| 10–15 | OAuth/scopes/approvals | Not applicable. |
| 16 | APIs/resources | Bucket in the account-regional namespace; Block Public Access all on; `BucketOwnerEnforced`; versioning on; Object Lock enabled (no default retention; apply per object at ACCEPT); SSE-KMS with one CMK per environment and Bucket Key on; EventBridge notifications on; bucket policy: TLS-only, deny PUT unless SSE-KMS with the environment key, **deny `s3:DeleteObject`/`DeleteObjectVersion` under `.../evidence/*` except a break-glass role** (Object Lock does not stop delete markers), GuardDuty tag-based read gate (§12); lifecycle: abort incomplete multipart 7 d, Intelligent-Tiering with an explicit size filter (objects <128 KB never transition by default), noncurrent-version expiry; CloudTrail data events (integrity-validated) rather than server access logs; gateway VPC endpoint (free) without a blanket `aws:SourceVpce` deny (it would break presigned browser uploads). Presigned **POST** with `content-length-range`, exact key, enforced Content-Type, SSE-KMS headers and `x-amz-checksum-sha256`; 5–15 minute expiry. |
| 17 | Events | S3 → EventBridge for scan triggers; `s3:ObjectRetention:Put` notifications; CloudTrail alarms on `PutBucketPolicy`, `PutObjectLockConfiguration`, `BypassGovernanceRetention`. |
| 18 | Limits | Session policy 2,048 chars; POST Object ≤5 GB single part; SHA-256 checksums are composite (part-size dependent) for multipart, so cap evidence at single-part size (≤50 MB recommended). |
| 19 | Quota/cost visibility | CloudWatch `AWS/S3` request and error metrics; Cost Explorer. |
| 20 | Health signals | 4xx spike on presigned puts; objects without a scan tag >15 min; CloudTrail HTTP attempts (`tlsDetails` missing); retention put events outside the accept path. |
| 21 | States to normalize | `UPLOAD_PENDING → SCAN_PENDING → CLEAN → ACCEPTED (locked) → ARCHIVED`, branches `INFECTED`, `UNSUPPORTED`, `SCAN_FAILED`, `SCAN_TIMEOUT`, `DELETED_ON_REQUEST`. The current `ScanState` (`pending_scan, clean, rejected, not_applicable`) needs `unsupported`, `failed`, `timeout`. |
| 22 | Safe automation | Presigned POST issuance; SHA-256 verification (`x-amz-checksum-sha256` + `GetObjectAttributes`, never ETag); GOVERNANCE retention at ACCEPT; lifecycle; noncurrent expiry; tenant purge after a signed, dated offboarding request with per-entry `DeleteObjects` error parsing and a post-purge empty-listing assertion. |
| 23 | Founder Action | Upload; re-upload after `UNSUPPORTED`. |
| 24 | Operator action | Any `BypassGovernanceRetention`; retention shortening; bucket policy edits; approving offboarding purge. |
| 25 | Evidence of setup | Bucket config snapshot; EICAR/clean/password-protected round trips; delete-marker test proving a tenant role cannot delete accepted evidence; tag-on-locked-object test (AWS does not state affirmatively that tags can be modified under retention; the "annotations" restriction is a separate feature; verify empirically before committing). |
| 26 | Verification should check | Evidence records carry `s3_version_id`, `sha256_client == sha256_s3`, `scan_status == NO_THREATS_FOUND`, `lock_mode == GOVERNANCE`, `locked_until` set, before any Founder Action can be `VERIFIED`. |
| 27 | Deletion/retention | GOVERNANCE not COMPLIANCE (COMPLIANCE cannot be undone short of closing the AWS account, which is incompatible with erasure requests); consider variable retention/event holds (Sept 2026) so retention runs from "dispute closed" or "account closed". Crypto-shredding via per-tenant keys only if a customer contract demands it ($1/key/month). |
| 28 | Handoff | Export objects + digests + audit log to the customer on Take-the-Keys; never transfer the bucket. |
| 29 | Customer ownership | Content is customer-owned; storage is Business Builder-controlled. |
| 30 | Compliance | S3/KMS in SOC 1/2/3 scope; Object Lock assessed by Cohasset for SEC 17a-4; single-Region residency; AWS publishes nothing reconciling Object Lock with GDPR erasure (counsel question). |
| 31 | Cost | Worked example 1,000 companies × 200 MB: ≈$5.83/mo S3 Standard + KMS; egress to browsers ≈$5.65–14.65/mo is the largest line; Intelligent-Tiering ≈$0.90/mo storage; CloudTrail data events ≈$0.44/yr. |
| 32 | Who bears cost | Business Builder. |
| 33 | Production prerequisites | Everything in row 16, plus the break-glass role behind MFA + SCP + alarm, IAM Access Analyzer, staging drill of the seven verification checks. |
| 34 | Repo readiness | EXISTS: `S3ArtifactStore` (exact prefix, no list, presigned GET ≤900 s), sha256 verified on put/read, quarantine status on upload, `AwsSecretsManagerStore` exact-ARN. PARTIAL: encryption is `AES256` (SSE-S3) not SSE-KMS; uploads pass through the backend rather than presigned POST; no Object Lock, versioning, lifecycle, TBAC or CloudTrail config in `infra/`; `staging-execution-secret-policy.json` uses a `businessbuilder-staging-*` wildcard contrary to the documented rule. MISSING: Object Lock/retention calls, `s3_version_id` on evidence records, offboarding purge. |
| 35 | Missing backend | SSE-KMS + checksum on put; `PutObjectRetention` at ACCEPT; version ID persistence; presigned POST issuance (optional in v1, backend pass-through is acceptable at ≤25 MB); purge runbook code; infra-as-code for the bucket baseline. |
| 36 | Missing frontend | Plain-English scan/acceptance states in the evidence history (currently title-cased enum names). |
| 37 | Difficulty | 4/5 for the full immutability/erasure design; 2/5 for the bucket baseline. |
| 38 | Order | Phase 1 (evidence is launch-critical and the malware-scanner approval is one of the nine release gates). |

## 12. GuardDuty Malware Protection for S3

| # | Item | Finding |
|---|---|---|
| 1 | Provider | Amazon GuardDuty Malware Protection for S3 |
| 2 | Business capability | Scans every new evidence object; tags it; gates reads until clean. |
| 3 | Recommended role | The production `MalwareScannerPort` replacement, but **asynchronous**: the current port is synchronous (`scan(content) -> result`); GuardDuty results arrive minutes later via tag + EventBridge, so the evidence state machine must hold `SCAN_PENDING` and be advanced by an event consumer. |
| 4 | Default | Yes. Enable the GuardDuty detector first (findings `Object:S3/MaliciousFile` are not retroactive), then the plan with `Tagging: ENABLED` before any evidence is uploaded. |
| 5 | KEEP alternatives | No. ClamAV-on-Lambda (self-maintained signatures, 15-min/10 GB ceilings) and third-party scanners (data may leave account/Region) are worse for evidence. |
| 6–8 | Ownership/creation/payment | Business Builder AWS account; Business Builder pays. |
| 9 | Authentication | Service role trusted by `malware-protection-plan.guardduty.amazonaws.com` with the eight documented statements (EventBridge managed rule, tagging, bucket notification, validation object, ListBucket, GetObject/GetObjectVersion, KMS `GenerateDataKey`/`Decrypt` via `s3.<region>.amazonaws.com`) ([IAM prerequisite](https://docs.aws.amazon.com/guardduty/latest/ug/malware-protection-s3-iam-policy-prerequisite.html)). |
| 10–15 | OAuth/approvals | Not applicable. Independent enablement without a detector is console-only for the first click. |
| 16 | APIs/resources | `AWS::GuardDuty::MalwareProtectionPlan` / `aws_guardduty_malware_protection_plan`; `CreateMalwareProtectionPlan`; `SendObjectMalwareScan` for on-demand and retries (no free tier). Whole-bucket or ≤5 prefixes; **25 protected buckets per account per Region** (rules out bucket-per-tenant). Max object 100 GB; 100,000 extracted files; nesting 100. SSE-C and client-side-encrypted objects are not scannable (the latter scans ciphertext silently); Intelligent-Tiering Archive/Deep Archive not supported; Object Lock objects are scannable. |
| 17 | Events | EventBridge `detail-type: "GuardDuty Malware Protection Object Scan Result"`, `source: aws.guardduty`, `detail.scanStatus ∈ COMPLETED|SKIPPED|FAILED`, `scanResultDetails.scanResultStatus ∈ NO_THREATS_FOUND|THREATS_FOUND|UNSUPPORTED|ACCESS_DENIED|FAILED`, `statusReasons`, `s3ObjectDetails.versionId`, `s3Throttled`. Also `... Resource Status Active|Warning|Error` and `... Post Scan Action Failed` (`ACCESS_DENIED`, `MAX_TAG_LIMIT_EXCEEDED`: scan succeeded but the tag never landed, so the read gate blocks a clean file forever; alarm on it). At-least-once delivery; consumer must be idempotent on `(bucket, key, versionId, status)`. Object tag `GuardDutyMalwareScanStatus`. |
| 18 | Limits/latency | No published latency SLA; calibrate a `SCAN_PENDING` deadline (start at 15 min) from staging `CompletedScanCount` timing; never time out into CLEAN. 10 tags per object; reserve a slot. |
| 19 | Cost visibility | CloudWatch `AWS/GuardDuty/MalwareProtection`: `CompletedScanCount`, `FailedScanCount`, `SkippedScanCount` (dimension `Skipped Reason` ∈ `Unsupported|MissingPermissions`), `InfectedScanCount`, `CompletedScanBytes`; use SUM statistics. |
| 20 | Health signals | `CompletedScanCount > 0` correlated with upload volume (plan status stays Active even if the role is broken); `SkippedScanCount{MissingPermissions} > 0` pages; `Post Scan Action Failed`; `s3Throttled`. |
| 21 | States to normalize | `NO_THREATS_FOUND` → CLEAN; `THREATS_FOUND` → INFECTED (terminal, operator alert) "We couldn't accept this file. Please upload a different copy." (no "virus" wording); `UNSUPPORTED` (`PASSWORD_PROTECTED`, size/extraction limits, `UNSUPPORTED_STORAGE_CLASS`) → founder re-upload "Try a PDF or screenshot instead."; `ACCESS_DENIED` (`UNAUTHORIZED_TO_GET_OBJECT`, `SSE_C_ENCRYPTED_OBJECT`, `OBJECT_E_TAG_CHANGED` benign) → SCAN_FAILED operator misconfiguration; `FAILED` → auto re-scan ≤3× then operator; timeout → "We're still checking this file." |
| 22 | Safe automation | State transitions from events; ≤3 re-scans; founder notification on UNSUPPORTED; lifecycle cleanup of aged infected objects. |
| 23 | Founder Action | Re-upload. Founders never override a verdict. |
| 24 | Operator action | Releasing an infected object (must re-run `SendObjectMalwareScan`, not flip a flag); any tag modification; disabling the TBAC policy; deleting the plan or role. |
| 25 | Evidence of setup | EICAR → `THREATS_FOUND`, 403 on GetObject, finding present; clean PDF → `NO_THREATS_FOUND` with measured latency; password ZIP → `UNSUPPORTED`; role tagging permission revoked → `Post Scan Action Failed` + alarm; tag-under-retention test; delete-marker test; `s3Throttled` false under burst. |
| 26 | Verification should check | Evidence `scan_status == NO_THREATS_FOUND` from the event/tag, then ACCEPT, then lock, strictly in that order. |
| 27 | Quarantine | Keep in place, tagged and read-denied by the AWS-published TBAC bucket policy (`Deny GetObject unless s3:ExistingObjectTag/GuardDutyMalwareScanStatus == NO_THREATS_FOUND`, `NotPrincipal` for the scanner role and session, plus a `Deny PutObjectTagging` on that key for everyone else). Do not copy to a quarantine bucket (breaks checksums, needs bypass, consumes a protected-bucket slot). Add the ECS task role/Lambda to `NotPrincipal` only where required and test the brittle `NotPrincipal` match in staging. |
| 28–29 | Handoff/ownership | Not applicable. |
| 30 | Compliance | Scanning occurs in an isolated same-Region VPC with no internet; the copy is deleted after scanning; GuardDuty in SOC scope. |
| 31 | Cost | $0.09/GB scanned + $0.215 per 1,000 objects; free tier 1,000 objects + 1 GB per month. Worked example: ≈$1.96/mo (≈$23.5/yr) for 200 GB / 40,000 objects spread over a year; ≈$26 one-time to backfill. |
| 32 | Who bears cost | Business Builder. |
| 33 | Production prerequisites | Detector → role → EventBridge on bucket → plan with tagging → TBAC policy + SCP → rule → SQS + DLQ → idempotent consumer → SUM alarms → timeout sweeper → the seven checks. |
| 34 | Repo readiness | MISSING entirely. PARTIAL: `MalwareScannerPort`, `PendingMalwareScanner` (fail-closed), quarantine-by-default on upload, `require_clean` gate in evidence review. |
| 35 | Missing backend | Async scan-result consumer (SQS worker or Lambda → API) that advances `ScanState`; new states; deadline sweeper; on-demand rescan; operator release action with audit; infra-as-code. |
| 36 | Missing frontend | Plain-English scan states and re-upload prompt. |
| 37 | Difficulty | 2/5. |
| 38 | Order | Phase 1 (release gate). |
