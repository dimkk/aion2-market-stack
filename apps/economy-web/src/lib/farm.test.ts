import { describe, expect, it } from 'vitest'

import {
  computeFarmSessionPayloadId,
  farmApiRowToView,
  farmActivityRollups,
  farmExportToView,
  parseFarmSessionExport,
  verifyFarmSessionExportPayloadId,
} from './farm'

const exportPayload = {
  schema: 'a2.farm.session-export.v1',
  payload_id: 'c7516183fbb979a318f1ab216f8b3b29928384df06d4197b9e08c5c65aa0c8ff',
  session: {
    session_id: 'farm-1',
    activity: { kind: 'dungeon', label: 'Secret Library' },
    started_at_ms: 1_000,
    stopped_at_ms: 3_000,
    observed_direct_kina: 400_000,
    estimated_loot_value: 120,
    estimated_total_value: 400_120,
    event_count: 3,
    loot: [{ item_id: 42, quantity: 2, unit_price: 60, market_server_id: 101, price_observed_at_ms: 2_000, price_source: 'market-summary', estimated_value: 120 }],
  },
}

describe('farm session exchange contract', () => {
  it('accepts the desktop export and preserves the three value dimensions', () => {
    const parsed = parseFarmSessionExport(exportPayload)
    const view = farmExportToView(parsed, 'local')
    expect(view).toMatchObject({ sessionId: 'farm-1', activity: { kind: 'dungeon', label: 'Secret Library' }, directKina: 400_000, lootValue: 120, totalValue: 400_120, durationSeconds: 2 })
    expect(view.kinaPerSecond).toBe(200_060)
  })

  it('aggregates comparable farm value by activity using weighted time', () => {
    const one = { ...farmExportToView(parseFarmSessionExport(exportPayload), 'local'), totalValue: 12 }
    const two = { ...one, sessionId: 'farm-2', payloadId: 'c'.repeat(64), durationSeconds: 4, totalValue: 12 }
    const rows = farmActivityRollups([one, two])
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ kind: 'dungeon', label: 'Secret Library', sessions: 2, durationSeconds: 6, totalValue: 24, kinaPerSecond: 4 })
  })

  it('uses the caller-provided locale label for unlabelled activities', () => {
    const row = { ...farmExportToView(parseFarmSessionExport(exportPayload), 'local'), activity: null }
    expect(farmActivityRollups([row], 'Без метки')[0].label).toBe('Без метки')
    expect(farmActivityRollups([row], 'Unlabelled')[0].label).toBe('Unlabelled')
  })

  it('rejects an invalid payload id before upload', () => {
    expect(() => parseFarmSessionExport({ ...exportPayload, payload_id: 'not-a-sha' })).toThrow('farm_export_payload_id_invalid')
  })

  it('rejects unbounded or unknown activity metadata', () => {
    for (const activity of [
      { kind: 'raid', label: null },
      { kind: 'spot', label: 'x'.repeat(81) },
      { kind: 'spot', label: 'unsafe\u0085label' },
      { kind: 'spot', label: null, location: 'invented' },
    ]) {
      expect(() => parseFarmSessionExport({
        ...exportPayload,
        session: { ...exportPayload.session, activity },
      })).toThrow('farm_export_activity_invalid')
    }
  })

  it('recomputes SHA-256 in the browser contract and rejects tampering before storage', async () => {
    const parsed = parseFarmSessionExport(exportPayload)
    expect(await computeFarmSessionPayloadId(parsed)).toBe(parsed.payload_id)
    await expect(verifyFarmSessionExportPayloadId(parsed)).resolves.toBe(parsed)
    await expect(verifyFarmSessionExportPayloadId({
      ...parsed,
      session: { ...parsed.session, observed_direct_kina: parsed.session.observed_direct_kina + 1 },
    })).rejects.toThrow('farm_export_payload_hash_mismatch')
  })

  it('renders a server envelope containing the original export', () => {
    expect(farmApiRowToView({ export: exportPayload }).source).toBe('server')
  })

  it('accepts a signed net wallet delta from the decoder contract', () => {
    const parsed = parseFarmSessionExport({
      ...exportPayload,
      session: {
        ...exportPayload.session,
        observed_direct_kina: -500,
        estimated_loot_value: 120,
        estimated_total_value: -380,
      },
    })
    expect(farmExportToView(parsed, 'local')).toMatchObject({
      directKina: -500,
      lootValue: 120,
      totalValue: -380,
      loot: [{ marketServerId: 101, priceObservedAtMs: 2_000, priceSource: 'market-summary' }],
    })
  })

  it('renders the backend flat aggregate without reparsing it as an export envelope', () => {
    const view = farmApiRowToView({
      id: 'server-row',
      schema: 'a2.farm.session-export.v1',
      client_session_id: 'a'.repeat(64),
      source_session_id: 'farm-1',
      activity: { kind: 'spot', label: null },
      started_at_ms: 1_000,
      stopped_at_ms: 3_000,
      duration_seconds: 2,
      observed_direct_kina: 400_000,
      estimated_loot_value: 120,
      estimated_total_value: 400_120,
      loot: [{ item_id: '42', quantity: 2, unit_price: 60, market_server_id: 101, price_observed_at_ms: 1_500, price_source: 'market-summary', estimated_value: 120 }],
    })
    expect(view).toMatchObject({
      sessionId: 'farm-1',
      payloadId: 'a'.repeat(64),
      startedAt: '1970-01-01T00:00:01.000Z',
      endedAt: '1970-01-01T00:00:03.000Z',
      directKina: 400_000,
      lootValue: 120,
      totalValue: 400_120,
      source: 'server',
      activity: { kind: 'spot', label: null },
      loot: [{ marketServerId: 101, priceObservedAtMs: 1_500, priceSource: 'market-summary' }],
    })
  })
})
