# Bundled historical market dataset

The self-hosted stack copies this directory into its writable runtime only when
that runtime is empty. A successful scan later publishes a new immutable
revision and keeps the same SQLite history.

The checked-in seed is the complete safe derived market database available to
the authors at publication time for client build `25650019`:

- 1,102 summary runs and 314,803 price rows across 101 observed market scopes;
- all 23,799 individual listing observations retained by the research database
  (deep listing scans covered two scopes);
- item IDs, prices, quantities, listing IDs, conditions, counts, public item
  metadata, and build/observation provenance;
- no packet bytes, raw HTTP bodies, request headers, JWTs, cookies, TLS secrets,
  account IDs, character IDs, or seller identities.

`manifest.json` records hashes, the observation span, and exact row counts.
Rebuild from an equivalent credential-free derived database:

```powershell
python -B scripts\build_seed_from_database.py `
  --database path\to\market-history.sqlite `
  --output data\seed
```

This dataset contains every safe derived row available at publication time. It
is historical, not a promise of current prices, and cannot contain listings that
were never observed between scans. It must not be used as a trading guarantee.
Game-derived names, IDs, prices, and metadata remain subject to the rights and
terms of their respective owners and are not relicensed under the repository's
Apache-2.0 source-code license.
