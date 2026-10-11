# Experimental farming tracker

This is a standalone, host-side proof of concept. It consumes the packet bus
written by the Windows collector; it does not inject into the game, launch the
client, or require the market worker to be restarted.

The tracker currently implements only confirmed, strict packet shapes:

- server loot increment (`0x8D33`);
- absolute inventory/Kinah balance (`0x561B`), converted to a delta only after
  a per-flow baseline has been observed;
- local teleport plus map context (`0x3623`, `0x4001`, `0x4002`) for a
  build-scoped zone label.

Unknown lengths, directions, malformed frames, stale market prices, ambiguous
zones, and unrecognised item IDs are rejected. A missing quote remains unknown;
the tracker never substitutes a guessed price.

## Runtime flow

```text
Windows collector -> append-only local packet bus -> decoder -> session state
                                                     |          |
                                                     |          +-> SQLite history
                                                     +-> local market SQLite quote
                                                                |
                                                                +-> optional aggregate API sync
```

The first positive Kinah delta or confirmed loot event starts a session. The
session stops after 15 seconds without another positive Kinah delta. Completed
sessions remain in the local SQLite database and can be uploaded with a
write-only Farm sync token. Raw packets, TLS material, account data, and item
names are not part of the upload contract.

## Start

1. Initialize the self-hosted stack, then set
   `AION2_FARM_PACKET_BUS_ENABLED=true` and the exact current character's
   `AION2_FARM_MARKET_SERVER_ID` in the generated `.env`. There is no default or
   cross-server price fallback.
2. Keep `Start-Collector.ps1` running so it writes the bounded packet bus.
3. In another terminal, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\self-hosted\scripts\Start-FarmTracker.ps1
```

The local control surface listens on `http://127.0.0.1:8765` by default:

- `GET /api/farm/health`
- `GET /api/farm/session`
- `GET /api/farm/history`
- `GET /api/farm/history/{session_id}`
- `POST /api/farm/history/{session_id}/sync`

All mutations require an exact loopback same-origin `Origin` header. The sync
token is read from the process environment and is never accepted on the command
line or printed.

The bundled zone catalog is client-derived evidence for build `25650019`. It is
not relicensed as source code and must be refreshed and reverified for a new
client build.

The packet bus is opt-in. Each segment is capped at 32 MiB and at most eight
segments are retained. A bus initialization/write failure degrades farm tracking
only; it does not abort the primary market capture.
