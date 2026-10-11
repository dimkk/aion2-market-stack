# AION2 Economy API

Serverless backend for the private AION2 Economy deployment. One Yandex Cloud
Function entrypoint serves the SPA `index.html`, allowlisted content-hashed
`/assets/*`, and all `/api/*` routes. Unhashed asset names are rejected rather
than served with immutable caching. Assets remain in the private SPA bucket;
the bounded Function hop adds HSTS, immutable caching and `nosniff`. There is
no separate function authorizer and no direct protected market-object route.

The browser bundle contains no market snapshot. Approved market access is only
`GET /api/v1/market/snapshot`; other precomputed views use
`GET /api/v1/economy/{view}`. The API resolves `snapshots/current.json`, accepts
only canonical immutable `snapshots/versions/<revision>/...` keys, verifies
SHA-256 and the embedded revision, and reads the current role from YDB before
each protected response. JSON responses from these API routes are gzip-encoded
above 512 KiB, returned as a base64 Function payload with `Content-Encoding:
gzip`, and fail closed before the encoded body can exceed the 2 MB integration
budget below API Gateway's 2.5 MB response ceiling.

The crawler can publish a sanitized revision without `yc` or Object Storage
credentials on the worker. A revocable `crawler:control` Bearer authorizes
`POST /api/v1/crawler/economy/views` (one bounded gzip shard) and
`POST /api/v1/crawler/economy/commit` (one revision/hash manifest). The Function
uses its attached service-account identity to perform absence/ETag conditional
puts and read-after-write verification. Cookie fallback, raw payloads, request
headers, session material and token-like values in views are rejected. The
runtime identity needs `storage.uploader` on the exact private snapshot bucket;
it does not need bucket administration or object deletion.

YDB schema applies expiry TTL to sessions, scoped tokens and rate windows,
180-day TTL to audit/farm rows, and 30-day TTL to crawler actions. Existing
tables receive idempotent TTL `ALTER TABLE` statements during migration.

## Local checks

```powershell
$env:PYTHONPATH = (Resolve-Path 'services/economy-api').Path
python -B -m unittest discover -s services/economy-api/tests -v
```

## Handler

Yandex Cloud Functions entrypoint:

```text
economy_api.yandex_handler.handler
```

The deploy bundle uses `requirements-deploy.lock` with `pip --require-hashes`.
The unhashed input file is not a deploy dependency.

`scripts/publish_economy_views_http.py` is the worker path and reads its scoped
Bearer only from process environment. `scripts/publish_economy_views_yc.py`
keeps local `--yc-profile` support for owner-run recovery by project decision.
Credential provisioning and operator authorization belong to the development
infrastructure, not to this application repository.

Required runtime variables are documented in
`../../docs/23_YANDEX_ECONOMY_DEPLOY.md`.  No real credential belongs in this
directory or in Terraform state.
