import type { FarmLoot, FarmSession } from '../types'

export type FarmSessionExportLoot = {
  item_id: number
  quantity: number
  unit_price: number | null
  market_server_id: number | null
  price_observed_at_ms: number | null
  price_source: string | null
  estimated_value: number | null
}

export type FarmLootProvenance = {
  marketServerId: number | null
  priceObservedAtMs: number | null
  priceSource: string | null
}

export type FarmSessionActivity = {
  kind: 'spot' | 'dungeon' | 'mob' | 'other'
  label: string | null
}

export type FarmSessionWithProvenance = FarmSession & {
  loot: Array<FarmLoot & FarmLootProvenance>
  activity: FarmSessionActivity | null
}

export type FarmActivityRollup = {
  key: string
  kind: FarmSessionActivity['kind'] | 'unlabelled'
  label: string
  sessions: number
  durationSeconds: number
  totalValue: number
  kinaPerSecond: number
}

export type FarmSessionExportV1 = {
  schema: 'a2.farm.session-export.v1'
  payload_id: string
  session: {
    session_id: string
    activity?: FarmSessionActivity | null
    started_at_ms: number
    stopped_at_ms: number
    observed_direct_kina: number
    estimated_loot_value: number
    estimated_total_value: number
    event_count: number
    loot: FarmSessionExportLoot[]
  }
}

const isSafeInteger = (value: unknown, minimum = 0) =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum

const isSafeSignedInteger = (value: unknown) =>
  typeof value === 'number' && Number.isSafeInteger(value)

export function parseFarmSessionExport(value: unknown): FarmSessionExportV1 {
  if (!value || typeof value !== 'object') throw new Error('farm_export_not_object')
  const root = value as Record<string, unknown>
  if (root.schema !== 'a2.farm.session-export.v1') throw new Error('farm_export_schema_mismatch')
  if (typeof root.payload_id !== 'string' || !/^[a-f0-9]{64}$/.test(root.payload_id)) {
    throw new Error('farm_export_payload_id_invalid')
  }
  if (!root.session || typeof root.session !== 'object') throw new Error('farm_export_session_missing')
  const session = root.session as Record<string, unknown>
  if (typeof session.session_id !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(session.session_id)) {
    throw new Error('farm_export_session_id_invalid')
  }
  const activity = session.activity
  if (activity !== null) {
    if (!activity || typeof activity !== 'object') throw new Error('farm_export_activity_invalid')
    const metadata = activity as Record<string, unknown>
    if (Object.keys(metadata).length !== 2 || !Object.hasOwn(metadata, 'kind') || !Object.hasOwn(metadata, 'label')) {
      throw new Error('farm_export_activity_invalid')
    }
    if (!['spot', 'dungeon', 'mob', 'other'].includes(String(metadata.kind))) throw new Error('farm_export_activity_invalid')
    if (metadata.label !== null && (
      typeof metadata.label !== 'string'
      || metadata.label.length < 1
      || new TextEncoder().encode(metadata.label).length > 80
      || metadata.label.trim() !== metadata.label
      || /[\u0000-\u001f\u007f-\u009f]/.test(metadata.label)
    )) throw new Error('farm_export_activity_invalid')
  }
  if (!isSafeInteger(session.started_at_ms) || !isSafeInteger(session.stopped_at_ms) || Number(session.stopped_at_ms) < Number(session.started_at_ms)) {
    throw new Error('farm_export_time_invalid')
  }
  for (const field of ['estimated_loot_value', 'event_count'] as const) {
    if (!isSafeInteger(session[field])) throw new Error(`farm_export_${field}_invalid`)
  }
  for (const field of ['observed_direct_kina', 'estimated_total_value'] as const) {
    if (!isSafeSignedInteger(session[field])) throw new Error(`farm_export_${field}_invalid`)
  }
  if (!Array.isArray(session.loot) || session.loot.length > 500) throw new Error('farm_export_loot_invalid')
  const loot = session.loot.map((entry, index) => {
    if (!entry || typeof entry !== 'object') throw new Error(`farm_export_loot_${index}_invalid`)
    const item = entry as Record<string, unknown>
    if (!isSafeInteger(item.item_id, 1) || !isSafeInteger(item.quantity, 1)) throw new Error(`farm_export_loot_${index}_identity_invalid`)
    for (const field of ['unit_price', 'price_observed_at_ms', 'estimated_value'] as const) {
      if (item[field] !== null && !isSafeInteger(item[field])) throw new Error(`farm_export_loot_${index}_${field}_invalid`)
    }
    if (item.market_server_id !== null && !isSafeInteger(item.market_server_id, 1)) {
      throw new Error(`farm_export_loot_${index}_market_server_id_invalid`)
    }
    if (item.price_source !== null && (typeof item.price_source !== 'string' || item.price_source.length > 96)) {
      throw new Error(`farm_export_loot_${index}_source_invalid`)
    }
    return item as FarmSessionExportLoot
  })
  return {
    schema: 'a2.farm.session-export.v1',
    payload_id: root.payload_id,
    session: {
      session_id: session.session_id as string,
      activity: activity as FarmSessionActivity | null,
      started_at_ms: session.started_at_ms as number,
      stopped_at_ms: session.stopped_at_ms as number,
      observed_direct_kina: session.observed_direct_kina as number,
      estimated_loot_value: session.estimated_loot_value as number,
      estimated_total_value: session.estimated_total_value as number,
      event_count: session.event_count as number,
      loot,
    },
  }
}

function unsignedFarmSessionExport(value: FarmSessionExportV1) {
  return {
    schema: value.schema,
    session: {
      session_id: value.session.session_id,
      activity: value.session.activity ?? null,
      started_at_ms: value.session.started_at_ms,
      stopped_at_ms: value.session.stopped_at_ms,
      observed_direct_kina: value.session.observed_direct_kina,
      estimated_loot_value: value.session.estimated_loot_value,
      estimated_total_value: value.session.estimated_total_value,
      event_count: value.session.event_count,
      loot: value.session.loot.map((item) => ({
        item_id: item.item_id,
        quantity: item.quantity,
        unit_price: item.unit_price,
        market_server_id: item.market_server_id,
        price_observed_at_ms: item.price_observed_at_ms,
        price_source: item.price_source,
        estimated_value: item.estimated_value,
      })),
    },
  }
}

export async function computeFarmSessionPayloadId(value: FarmSessionExportV1): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error('farm_export_sha256_unavailable')
  const canonical = new TextEncoder().encode(JSON.stringify(unsignedFarmSessionExport(value)))
  const digest = await globalThis.crypto.subtle.digest('SHA-256', canonical)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export async function verifyFarmSessionExportPayloadId(value: FarmSessionExportV1): Promise<FarmSessionExportV1> {
  const actual = await computeFarmSessionPayloadId(value)
  if (actual !== value.payload_id) throw new Error('farm_export_payload_hash_mismatch')
  return value
}

export function farmExportToView(value: FarmSessionExportV1, source: FarmSession['source']): FarmSessionWithProvenance {
  const session = value.session
  const durationSeconds = Math.max(0, (session.stopped_at_ms - session.started_at_ms) / 1000)
  const loot: Array<FarmLoot & FarmLootProvenance> = session.loot.map((item) => ({
    itemId: item.item_id,
    name: `Item ${item.item_id}`,
    quantity: item.quantity,
    unitPrice: item.unit_price,
    estimatedValue: item.estimated_value,
    marketServerId: item.market_server_id,
    priceObservedAtMs: item.price_observed_at_ms,
    priceSource: item.price_source,
  }))
  return {
    sessionId: session.session_id,
    activity: session.activity ?? null,
    payloadId: value.payload_id,
    startedAt: new Date(session.started_at_ms).toISOString(),
    endedAt: new Date(session.stopped_at_ms).toISOString(),
    durationSeconds,
    directKina: session.observed_direct_kina,
    lootValue: session.estimated_loot_value,
    totalValue: session.estimated_total_value,
    kinaPerSecond: durationSeconds > 0 ? session.estimated_total_value / durationSeconds : 0,
    source,
    loot,
  }
}

export function farmActivityRollups(
  sessions: FarmSessionWithProvenance[],
  unlabelled = 'Unlabelled',
): FarmActivityRollup[] {
  const groups = new Map<string, FarmActivityRollup>()
  for (const session of sessions) {
    const kind = session.activity?.kind ?? 'unlabelled'
    const label = session.activity?.label ?? unlabelled
    const key = `${kind}:${label}`
    const row = groups.get(key) ?? {
      key, kind, label, sessions: 0, durationSeconds: 0, totalValue: 0, kinaPerSecond: 0,
    }
    row.sessions += 1
    row.durationSeconds += session.durationSeconds
    row.totalValue += session.totalValue
    row.kinaPerSecond = row.durationSeconds > 0 ? row.totalValue / row.durationSeconds : 0
    groups.set(key, row)
  }
  return [...groups.values()].sort((a, b) => b.kinaPerSecond - a.kinaPerSecond || b.sessions - a.sessions)
}

export function farmApiRowToView(value: unknown): FarmSessionWithProvenance {
  if (!value || typeof value !== 'object') throw new Error('farm_api_row_invalid')
  const row = value as Record<string, unknown>
  if (row.schema === 'a2.farm.session-export.v1' && row.session && typeof row.session === 'object') {
    return farmExportToView(parseFarmSessionExport(row), 'server')
  }
  if (row.export && typeof row.export === 'object') return farmExportToView(parseFarmSessionExport(row.export), 'server')

  const sessionId = String(row.source_session_id ?? row.session_id ?? row.id ?? row.client_session_id ?? '')
  const payloadId = typeof row.client_session_id === 'string' && /^[a-f0-9]{64}$/.test(row.client_session_id)
    ? row.client_session_id
    : undefined
  const durationSeconds = Number(row.duration_seconds ?? 0)
  const directKina = Number(row.observed_direct_kina ?? row.direct_kina ?? 0)
  const lootValue = Number(row.estimated_loot_value ?? row.loot_value ?? 0)
  const totalValue = Number(row.estimated_total_value ?? row.total_kina ?? directKina + lootValue)
  if (!sessionId || !Number.isFinite(durationSeconds) || durationSeconds < 0 || ![directKina, lootValue, totalValue].every(Number.isFinite)) {
    throw new Error('farm_api_row_invalid')
  }
  const startedAt = row.started_at_ms == null
    ? String(row.started_at ?? row.created_at ?? new Date(0).toISOString())
    : new Date(Number(row.started_at_ms)).toISOString()
  const endedAt = row.stopped_at_ms == null
    ? String(row.ended_at ?? row.updated_at ?? startedAt)
    : new Date(Number(row.stopped_at_ms)).toISOString()
  const rawLoot = Array.isArray(row.loot) ? row.loot : []
  const rawActivity = row.activity
  const activity = rawActivity && typeof rawActivity === 'object'
    ? rawActivity as FarmSessionActivity
    : null
  return {
    sessionId,
    activity,
    payloadId,
    startedAt,
    endedAt,
    durationSeconds,
    directKina,
    lootValue,
    totalValue,
    kinaPerSecond: durationSeconds > 0 ? totalValue / durationSeconds : 0,
    location: typeof row.location === 'string' ? row.location : undefined,
    source: 'server',
    loot: rawLoot.map((item) => {
      const entry = item as Record<string, unknown>
      return {
        itemId: Number(entry.item_id),
        name: typeof entry.name === 'string' ? entry.name : `Item ${entry.item_id}`,
        quantity: Number(entry.quantity),
        unitPrice: entry.unit_price == null && entry.estimated_unit_price == null ? null : Number(entry.unit_price ?? entry.estimated_unit_price),
        estimatedValue: entry.estimated_value == null ? null : Number(entry.estimated_value),
        marketServerId: entry.market_server_id == null ? null : Number(entry.market_server_id),
        priceObservedAtMs: entry.price_observed_at_ms == null ? null : Number(entry.price_observed_at_ms),
        priceSource: typeof entry.price_source === 'string' ? entry.price_source : null,
      }
    }),
  }
}
