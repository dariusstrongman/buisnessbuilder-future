# Staging deploy: operator preview + differentiated starting points

Non-production only. This updates the existing isolated Cognito pilot service.
It does not touch production, does not merge to `main`, and enables no payment,
filing, banking or customer communication.

## What is being deployed

| Surface | Branch | Commit |
| --- | --- | --- |
| Backend | `staging/operator-preview-starting-points-v1` | `5059a63511e34636f91a0b5b4257525db7010080` |
| Flagship | `staging/operator-preview-starting-points-v1` | `eecef6e1916645f7e03552d3a7d95ee7570bad9a` |

The flagship branch is a clean merge of `codex/operator-dashboard-preview-v1` and
`codex/starting-point-journeys-v1` onto `codex/founder-dashboard-connections-v1`.
No behaviour outside those two features changes.

## Target

From `infra/cognito-pilot/resource-manifest.json`:

- Region `us-east-1`, account `199949321335`
- Cluster `businessbuilder-staging`, service `businessbuilder-pilot-auth`
- Task family `businessbuilder-pilot-auth-v1`
- ECR repository `businessbuilder-staging-app`
- CloudFront distribution `E1W26K53JZPMLJ` → `d3qncwxo58gn5b.cloudfront.net`
- Database schema `bb_cognito_pilot_v1` on the existing private staging RDS

## One required environment change

`NEXT_PUBLIC_SITE_URL` is read at build time by the statically prerendered
`robots.txt`. The current deployment was built without it, so its robots body
carries a `localhost` sitemap line. Pass it as a build argument, or accept that
one cosmetic line. The `Disallow` rules are literals and are correct either way —
`/operator` is disallowed in this build.

No other environment variable changes. Callback and logout URIs stay exactly as
they are; see "Domain" below before changing them.

## Steps

```bash
REGISTRY=199949321335.dkr.ecr.us-east-1.amazonaws.com
REPO=$REGISTRY/businessbuilder-staging-app
TAG=operator-preview-v1

aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $REGISTRY

# Backend
cd buisnessbuilder-future
git checkout staging/operator-preview-starting-points-v1
docker build -t $REPO:backend-$TAG .
docker push $REPO:backend-$TAG

# Flagship
cd ../businessbuilder-site
git checkout staging/operator-preview-starting-points-v1
docker build -f Dockerfile.pilot \
  --build-arg NEXT_PUBLIC_SITE_URL=https://d3qncwxo58gn5b.cloudfront.net \
  -t $REPO:site-$TAG .
docker push $REPO:site-$TAG
```

Then register a new task-definition revision from
`infra/cognito-pilot/task-definition.json` with the two new image digests,
update the service, and invalidate the edge:

```bash
aws ecs register-task-definition --cli-input-json file://infra/cognito-pilot/task-definition.json --region us-east-1
aws ecs update-service --cluster businessbuilder-staging --service businessbuilder-pilot-auth \
  --task-definition businessbuilder-pilot-auth-v1 --force-new-deployment --region us-east-1
aws ecs wait services-stable --cluster businessbuilder-staging --services businessbuilder-pilot-auth --region us-east-1
aws cloudfront create-invalidation --distribution-id E1W26K53JZPMLJ --paths '/*'
```

## Operator access

Operator access is provisioned with the existing authorization model. There is
no bypass and no global authorization change.

The operator identity must be **separate from the founder identity**. An owner
grants support access to someone else; a founder cannot grant it to themselves,
and the tooling refuses that.

1. Both accounts sign in through Cognito once, so an internal user exists.
2. Report readiness (writes nothing):

```bash
DATABASE_URL=... DB_SCHEMA=bb_cognito_pilot_v1 \
  python scripts/staging_operator_access.py inspect --email <operator-email>
```

3. If `SUPPORT authority : no`, the owner grants the membership:

```bash
python scripts/staging_operator_access.py provision-membership \
  --owner-email <founder-email> --email <operator-email>
```

4. Issue the scoped, expiring, read-only grant — either with the tool, or by the
   founder in-product through
   `POST /api/product/companies/{company_id}/residential-cleaning-pilot/support-grants`,
   which fixes the same two permissions:

```bash
python scripts/staging_operator_access.py provision-grant \
  --owner-email <founder-email> --email <operator-email> \
  --company-id <company_id> --minutes 480 --reason "staging dashboard preview"
```

The grant carries `company.view` and `artifacts.access` only. `billing.view`
sits inside `SUPPORT_NEVER_ALLOWED` and cannot be granted to a support user by
any path, so the preview reports billing as unavailable rather than showing it.

## Verify after deploy

```
GET  /                                    new starting-point copy, four branches
GET  /operator/companies                  200, grant chooser
GET  /robots.txt                          Disallow: /operator
GET  /api/product/operator/companies      401 without a session
POST /api/product/operator/companies      405 with a session and matching origin
```

Sign in, open `/operator/companies`, choose the grant, open a company, confirm
the sticky `ADMIN PREVIEW · READ ONLY` band and that the page offers no controls.

## Domain

`builder.stromation.com` currently resolves to Cloudflare and returns 404; it is
not wired to this distribution. Cognito's callback and logout URLs list the
CloudFront hostname only. Pointing the domain at staging requires, in order: DNS
authorization, a certificate, the CloudFront alternate domain name, then adding
the new callback and logout URLs to the Cognito app client and updating
`COGNITO_REDIRECT_URI`, `COGNITO_LOGOUT_URI` and `NEXT_PUBLIC_SITE_URL`. Until
all of that is done together, use the CloudFront hostname — changing only some
of it breaks sign-in.

## Rollback

Update the service back to task definition `businessbuilder-pilot-auth-v1:7`,
whose image digests are recorded in the resource manifest, and invalidate again.
No schema migration is introduced by this change, so rollback needs no data step.
