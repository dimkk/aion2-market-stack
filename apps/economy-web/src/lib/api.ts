import type { AdminUser, DesktopSyncToken, EconomyItem, EconomySnapshot, FarmSession, SessionState, UserRole } from '../types'
import { validateSnapshot } from './economy'
import { farmApiRowToView, type FarmSessionExportV1 } from './farm'

export class ApiError extends Error {
  constructor(public readonly status: number, public readonly code: string, message?: string) {
    super(message ?? code)
  }
}

type ApiUser = {
  id: string
  username: string
  role: Exclude<UserRole, 'guest'>
}

type ApiDesktopSyncToken = {
  id: string
  scope: 'farm:write'
  label: string | null
  created_at: string
  expires_at: string
  revoked_at: string | null
}

const desktopSyncToken = (value: ApiDesktopSyncToken): DesktopSyncToken => ({
  id: value.id,
  scope: value.scope,
  label: value.label,
  createdAt: value.created_at,
  expiresAt: value.expires_at,
  revokedAt: value.revoked_at,
})

const origin = (import.meta.env.VITE_ECONOMY_API_BASE as string | undefined)?.replace(/\/$/, '') ?? ''
const apiRoot = `${origin}/api/v1`

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null
  const prefix = `${encodeURIComponent(name)}=`
  const part = document.cookie.split(';').map((item) => item.trim()).find((item) => item.startsWith(prefix))
  return part ? decodeURIComponent(part.slice(prefix.length)) : null
}

function guestSession(): SessionState {
  return { authenticated: false, role: 'guest', username: null, csrfToken: null }
}

function userSession(user: ApiUser): SessionState {
  return { authenticated: true, role: user.role, username: user.username, csrfToken: readCookie('__Host-a2e_csrf') }
}

async function parseBody(response: Response): Promise<unknown> {
  if (response.status === 204) return null
  return response.json().catch(() => ({}))
}

async function request<T>(path: string, options: RequestInit = {}, csrfToken?: string | null): Promise<T> {
  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/json')
  if (options.body) headers.set('Content-Type', 'application/json')
  if (csrfToken) headers.set('X-CSRF-Token', csrfToken)
  const response = await fetch(`${apiRoot}${path}`, {
    ...options,
    headers,
    credentials: 'include',
    redirect: 'error',
    cache: options.method && options.method !== 'GET' ? 'no-store' : options.cache,
  })
  const body = await parseBody(response) as Record<string, unknown> | null
  if (!response.ok) {
    throw new ApiError(response.status, String(body?.error ?? body?.code ?? `http_${response.status}`), String(body?.message ?? body?.error ?? 'Request failed'))
  }
  return body as T
}

type MarketSegment = Pick<EconomySnapshot, 'schemaVersion' | 'revision' | 'generatedAt' | 'build' | 'items'>

function validateMarketSegment(value: unknown, revision: string | undefined): MarketSegment {
  if (!value || typeof value !== 'object') throw new Error('market_segment_not_object')
  const segment = value as Partial<MarketSegment>
  if (segment.schemaVersion !== 'aion2.economy.snapshot/v1' || !Array.isArray(segment.items)) {
    throw new Error('market_segment_invalid')
  }
  if (revision && segment.revision !== revision) throw new Error('market_segment_revision_mismatch')
  return segment as MarketSegment
}

async function protectedSnapshot(): Promise<EconomySnapshot> {
  const base = validateSnapshot(await request<unknown>('/market/snapshot', { cache: 'no-store' }))
  const segments = base.segments ?? []
  if (!segments.length) return base
  if (new Set(segments).size !== segments.length || segments.some((segment) => !/^[a-z0-9_-]{1,48}$/.test(segment))) {
    throw new Error('market_segments_invalid')
  }
  const parts = await Promise.all(segments.map(async (segment) => validateMarketSegment(
    await request<unknown>('/market/snapshot', {
      cache: 'no-store',
      headers: { 'X-Economy-Segment': segment },
    }),
    base.revision,
  )))
  const items = parts.flatMap((part) => part.items)
  const identities = new Set(items.map((item) => `${item.serverId}:${item.itemId}:${item.market}`))
  if (identities.size !== items.length || items.length !== base.stats.activeItems) {
    throw new Error('market_segment_coverage_mismatch')
  }
  return validateSnapshot({ ...base, items })
}

type ItemsIndex = Pick<EconomySnapshot, 'schemaVersion' | 'revision' | 'generatedAt' | 'build' | 'segments'> & {
  items: EconomyItem[]
  itemCount: number
}

function validateItemsIndex(value: unknown, revision?: string): ItemsIndex {
  if (!value || typeof value !== 'object') throw new Error('items_index_not_object')
  const index = value as Partial<ItemsIndex>
  if (
    index.schemaVersion !== 'aion2.economy.snapshot/v1'
    || !Array.isArray(index.items)
    || !Number.isSafeInteger(index.itemCount)
    || (index.itemCount ?? -1) < 0
    || (revision && index.revision !== revision)
  ) throw new Error('items_index_invalid')
  return index as ItemsIndex
}

async function protectedItems(revision?: string): Promise<EconomyItem[]> {
  const base = validateItemsIndex(
    await request<unknown>('/economy/items', { cache: 'no-store' }),
    revision,
  )
  const segments = base.segments ?? []
  if (!segments.length) {
    if (base.items.length !== base.itemCount) throw new Error('items_coverage_mismatch')
    return base.items
  }
  if (new Set(segments).size !== segments.length || segments.some((segment) => !/^[a-z0-9_-]{1,48}$/.test(segment))) {
    throw new Error('items_segments_invalid')
  }
  const parts = await Promise.all(segments.map(async (segment) => validateMarketSegment(
    await request<unknown>('/economy/items', {
      cache: 'no-store',
      headers: { 'X-Economy-Segment': segment },
    }),
    base.revision,
  )))
  const items = parts.flatMap((part) => part.items)
  const identities = new Set(items.map((item) => `${item.serverId}:${item.itemId}:${item.market}`))
  if (identities.size !== items.length || items.length !== base.itemCount) {
    throw new Error('items_segment_coverage_mismatch')
  }
  return items
}

export const economyApi = {
  session: async (): Promise<SessionState> => {
    try {
      const value = await request<{ user: ApiUser }>('/auth/me', { cache: 'no-store' })
      return userSession(value.user)
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return guestSession()
      throw error
    }
  },
  login: async (username: string, password: string): Promise<SessionState> => {
    const value = await request<{ user: ApiUser }>('/auth/login', {
      method: 'POST', body: JSON.stringify({ username, password }),
    })
    return userSession(value.user)
  },
  register: (username: string, password: string) => request<{ status: 'registration_received' }>('/auth/register', {
    method: 'POST', body: JSON.stringify({ username, password }),
  }),
  logout: async (csrfToken: string | null): Promise<SessionState> => {
    await request<null>('/auth/logout', { method: 'POST', body: JSON.stringify({}) }, csrfToken)
    return guestSession()
  },
  snapshot: protectedSnapshot,
  items: protectedItems,
  users: () => request<{ users: AdminUser[] }>('/admin/users', { cache: 'no-store' }),
  setRole: (username: string, role: Extract<UserRole, 'registered' | 'approved'>, csrfToken: string | null) =>
    request<{ user: AdminUser }>(`/admin/users/${encodeURIComponent(username)}/role`, {
      method: 'POST', body: JSON.stringify({ role }),
    }, csrfToken),
  farmSessions: async (): Promise<FarmSession[]> => {
    const rows: FarmSession[] = []
    const seen = new Set<string>()
    let cursor: string | null = null
    for (let page = 0; page < 100; page += 1) {
      const headers: Record<string, string> = { 'X-Page-Limit': '100' }
      if (cursor) headers['X-Page-Cursor'] = cursor
      const result = await request<{ sessions: unknown[]; next_cursor: string | null }>(
        '/farm/sessions',
        { cache: 'no-store', headers },
      )
      rows.push(...result.sessions.map(farmApiRowToView))
      if (!result.next_cursor) return rows
      if (seen.has(result.next_cursor)) throw new Error('farm_history_cursor_loop')
      seen.add(result.next_cursor)
      cursor = result.next_cursor
    }
    throw new Error('farm_history_page_limit_exceeded')
  },
  uploadFarmExport: async (payload: FarmSessionExportV1, csrfToken: string | null): Promise<FarmSession> => {
    const result = await request<{ session: unknown }>('/farm/sessions', {
      method: 'POST',
      headers: { 'Idempotency-Key': payload.payload_id },
      body: JSON.stringify(payload),
    }, csrfToken)
    return farmApiRowToView(result.session)
  },
  desktopSyncTokens: async (): Promise<DesktopSyncToken[]> => {
    const result = await request<{ tokens: ApiDesktopSyncToken[] }>('/farm/sync-tokens', { cache: 'no-store' })
    return result.tokens.map(desktopSyncToken)
  },
  issueDesktopSyncToken: async (label: string, expiresInSeconds: number, csrfToken: string | null): Promise<{ token: string; tokenMeta: DesktopSyncToken }> => {
    const result = await request<{ token: string; token_meta: ApiDesktopSyncToken }>('/farm/sync-tokens', {
      method: 'POST', body: JSON.stringify({ label, expires_in_seconds: expiresInSeconds }),
    }, csrfToken)
    return { token: result.token, tokenMeta: desktopSyncToken(result.token_meta) }
  },
  revokeDesktopSyncToken: async (tokenId: string, csrfToken: string | null): Promise<void> => {
    await request<null>(`/farm/sync-tokens/${encodeURIComponent(tokenId)}`, { method: 'DELETE' }, csrfToken)
  },
}
