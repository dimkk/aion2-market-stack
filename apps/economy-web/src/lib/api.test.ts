import { afterEach, describe, expect, it, vi } from 'vitest'

import { economyApi } from './api'
import type { FarmSessionExportV1 } from './farm'

afterEach(() => vi.unstubAllGlobals())

const response = (body: unknown, status = 200) => new Response(status === 204 ? null : JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

describe('economy API integration contract', () => {
  it('maps an unauthenticated /me response to guest without loading market data', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({ error: 'authentication_required' }, 401))
    vi.stubGlobal('fetch', fetcher)
    await expect(economyApi.session()).resolves.toEqual({ authenticated: false, role: 'guest', username: null, csrfToken: null })
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/auth/me')
  })

  it('uses the role-gated API backed by an immutable market revision', async () => {
    const snapshot = {
      schemaVersion: 'aion2.economy.snapshot/v1', generatedAt: '2026-10-05T00:00:00Z', build: 25650019,
      stats: { catalogItems: 1, activeItems: 0, listings: 0, servers: 0 }, items: [], recipes: [], servers: [], limitations: [],
    }
    const fetcher = vi.fn().mockResolvedValue(response(snapshot))
    vi.stubGlobal('fetch', fetcher)
    await expect(economyApi.snapshot()).resolves.toEqual(snapshot)
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/market/snapshot')
  })

  it('assembles every advertised market shard from the same revision', async () => {
    const base = {
      schemaVersion: 'aion2.economy.snapshot/v1', revision: 'r-segmented', segments: ['s113', 's123'],
      generatedAt: '2026-10-07T19:44:00Z', build: 25650019,
      stats: { catalogItems: 9100, activeItems: 2, listings: 8, servers: 2 },
      items: [], recipes: [], servers: [], limitations: [],
    }
    const item = (serverId: number, itemId: number) => ({ serverId, itemId, market: 'server' })
    const fetcher = vi.fn(async (_url: string, options?: RequestInit) => {
      const segment = new Headers(options?.headers).get('X-Economy-Segment')
      if (segment === 's113') return response({ ...base, segments: undefined, items: [item(11301, 1)] })
      if (segment === 's123') return response({ ...base, segments: undefined, items: [item(12301, 2)] })
      return response(base)
    })
    vi.stubGlobal('fetch', fetcher)
    const assembled = await economyApi.snapshot()
    expect(assembled.items.map((row) => [row.serverId, row.itemId])).toEqual([[11301, 1], [12301, 2]])
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual([
      '/api/v1/market/snapshot',
      '/api/v1/market/snapshot',
      '/api/v1/market/snapshot',
    ])
    expect(fetcher.mock.calls.slice(1).map((call) =>
      new Headers(call[1]?.headers).get('X-Economy-Segment'))).toEqual(['s113', 's123'])
  })

  it('assembles every history item shard without mixing revisions', async () => {
    const base = {
      schemaVersion: 'aion2.economy.snapshot/v1', revision: 'r-items', segments: ['s113', 's123'],
      generatedAt: '2026-10-08T00:00:00Z', build: 25650019, items: [], itemCount: 3,
    }
    const item = (serverId: number, itemId: number, saleCount: number) => ({ serverId, itemId, market: 'server', saleCount })
    const fetcher = vi.fn(async (_url: string, options?: RequestInit) => {
      const segment = new Headers(options?.headers).get('X-Economy-Segment')
      if (segment === 's113') return response({ ...base, segments: undefined, itemCount: undefined, items: [item(11301, 1, 1)] })
      if (segment === 's123') return response({ ...base, segments: undefined, itemCount: undefined, items: [item(12301, 2, 1), item(12301, 3, 0)] })
      return response(base)
    })
    vi.stubGlobal('fetch', fetcher)
    const assembled = await economyApi.items('r-items')
    expect(assembled.map((row) => [row.serverId, row.itemId, row.saleCount])).toEqual([
      [11301, 1, 1], [12301, 2, 1], [12301, 3, 0],
    ])
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual([
      '/api/v1/economy/items', '/api/v1/economy/items', '/api/v1/economy/items',
    ])
  })

  it('posts role changes to the canonical username route', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({ user: { id: 'u1', username: 'alice', role: 'approved' } }))
    vi.stubGlobal('fetch', fetcher)
    await economyApi.setRole('alice', 'approved', 'csrf-value')
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/admin/users/alice/role')
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'POST' })
    expect(new Headers(fetcher.mock.calls[0][1].headers).get('X-CSRF-Token')).toBe('csrf-value')
  })

  it('uploads the exact safe desktop aggregate with an idempotency key', async () => {
    const payload: FarmSessionExportV1 = {
      schema: 'a2.farm.session-export.v1', payload_id: 'b'.repeat(64),
      session: { session_id: 'farm-1', started_at_ms: 1000, stopped_at_ms: 3000, observed_direct_kina: 5, estimated_loot_value: 7, estimated_total_value: 12, event_count: 1, loot: [] },
    }
    const fetcher = vi.fn().mockResolvedValue(response({ session: {
      id: 'stored-1', client_session_id: 'farm-1', started_at: '1970-01-01T00:00:01Z', ended_at: '1970-01-01T00:00:03Z', duration_seconds: 2,
      observed_direct_kina: 5, estimated_loot_value: 7, estimated_total_value: 12, loot: [],
    } }))
    vi.stubGlobal('fetch', fetcher)
    await economyApi.uploadFarmExport(payload, 'csrf-value')
    const options = fetcher.mock.calls[0][1]
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/farm/sessions')
    expect(new Headers(options.headers).get('Idempotency-Key')).toBe(payload.payload_id)
    expect(JSON.parse(options.body)).toEqual(payload)
  })

  it('issues and revokes a scoped one-time-visible desktop sync token', async () => {
    const tokenMeta = { id: 'a'.repeat(32), scope: 'farm:write', label: 'desktop', created_at: '2026-10-05T00:00:00Z', expires_at: '2026-10-12T00:00:00Z', revoked_at: null }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(response({ token: 'a2farm_secret', token_meta: tokenMeta }, 201))
      .mockResolvedValueOnce(response(null, 204))
    vi.stubGlobal('fetch', fetcher)

    const issued = await economyApi.issueDesktopSyncToken('desktop', 604800, 'csrf-value')
    expect(issued).toMatchObject({ token: 'a2farm_secret', tokenMeta: { id: tokenMeta.id, scope: 'farm:write', label: 'desktop' } })
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/farm/sync-tokens')
    expect(new Headers(fetcher.mock.calls[0][1].headers).get('X-CSRF-Token')).toBe('csrf-value')
    await economyApi.revokeDesktopSyncToken(tokenMeta.id, 'csrf-value')
    expect(fetcher.mock.calls[1][0]).toBe(`/api/v1/farm/sync-tokens/${tokenMeta.id}`)
    expect(fetcher.mock.calls[1][1]).toMatchObject({ method: 'DELETE' })
  })
})
