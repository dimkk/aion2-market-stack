# Sanitized starter dataset

The self-hosted stack copies this directory into its writable runtime only when
that runtime is empty. A successful scan later publishes a new immutable
revision and keeps the same SQLite history.

The checked-in seed was built from a sanitized read-only market summary observed
on 2026-10-10 for client build `25650019`:

- 100 configured Global market scopes;
- 29,587 item/server summary rows;
- only item IDs, minimum unit prices, sale counts, public item metadata, and
  build/observation provenance;
- no packet bytes, raw HTTP bodies, request headers, JWTs, cookies, TLS secrets,
  account IDs, or character IDs.

`manifest.json` records hashes and exact row counts. Rebuild from an equivalent
sanitized summary:

```powershell
python -B scripts\build_seed.py `
  --summary path\to\sanitized-multi-summary.json `
  --output data\seed `
  --observed-at 2026-10-10T06:45:06Z
```

This dataset is historical sample data. It is not a promise of current prices,
does not contain every individual auction listing, and must not be used as a
trading guarantee. Game-derived names, IDs, prices, and metadata remain subject
to the rights and terms of their respective owners and are not relicensed under
the repository's Apache-2.0 source-code license.
