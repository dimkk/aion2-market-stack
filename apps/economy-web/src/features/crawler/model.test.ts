import { describe, expect, it } from 'vitest'

import {
  allowedCrawlerActions,
  calculateCrawlerMetrics,
  crawlerErrorClipboardContext,
  createCrawlerApi,
  crawlerHeartbeatFreshness,
  formatDuration,
  safeCrawlerErrorPayload,
} from './model'
import { buildLlmClipboardPayload } from '../../components/CopyForLlm'
import type { CrawlerStatus } from './types'

const status: CrawlerStatus = {
  configured: true,
  runId: 'run-42',
  status: 'running',
  phase: 'scan',
  revision: 7,
  updatedAt: '2026-10-05T12:00:00Z',
  startedAt: '2026-10-05T11:59:00Z',
  elapsedSeconds: 60,
  totalItems: 1000,
  processedItems: 300,
  successfulRequests: 270,
  failedRequests: 30,
  retriedRequests: 12,
  queueDepth: 700,
  checkpoint: { cursor: 'item:299', processedItems: 300, savedAt: '2026-10-05T12:00:00Z', revision: 7 },
  errors: [],
}

describe('crawler operations model', () => {
  it('calculates coverage, rates and ETA from confirmed counters', () => {
    expect(calculateCrawlerMetrics(status)).toEqual({
      coveragePercent: 30,
      itemsPerSecond: 5,
      requestsPerSecond: 5,
      etaSeconds: 140,
      remainingItems: 700,
      totalRequests: 300,
    })
  })

  it('does not invent rates or ETA before counters start', () => {
    const metrics = calculateCrawlerMetrics({ ...status, elapsedSeconds: 0, processedItems: 0 })
    expect(metrics.itemsPerSecond).toBe(0)
    expect(metrics.requestsPerSecond).toBe(0)
    expect(metrics.etaSeconds).toBeNull()
  })

  it('reports heartbeat freshness from server-confirmed update time', () => {
    const now = Date.parse('2026-10-05T12:02:00Z')
    expect(crawlerHeartbeatFreshness(status.updatedAt, now)).toEqual({
      state: 'fresh', ageSeconds: 120,
    })
    expect(crawlerHeartbeatFreshness(status.updatedAt, now + 1000)).toEqual({
      state: 'stale', ageSeconds: 121,
    })
    expect(crawlerHeartbeatFreshness(status.updatedAt, Date.parse(status.updatedAt!) - 1000)).toEqual({
      state: 'future', ageSeconds: -1,
    })
    expect(crawlerHeartbeatFreshness(null, now)).toEqual({
      state: 'unknown', ageSeconds: null,
    })
    expect(crawlerHeartbeatFreshness('invalid', now)).toEqual({
      state: 'unknown', ageSeconds: null,
    })
  })

  it('formats crawler durations in the selected UI language', () => {
    expect(formatDuration(121, 'ru')).toBe('2м 1с')
    expect(formatDuration(121, 'en')).toBe('2m 1s')
    expect(formatDuration(3720, 'en')).toBe('1h 2m')
  })

  it('preserves explicit disconnected state for the UI', () => {
    const disconnected: CrawlerStatus = {
      ...status, configured: false, status: 'not_connected', updatedAt: null,
      runId: null, totalItems: 0, processedItems: 0,
    }
    expect(disconnected.updatedAt).toBeNull()
    expect(allowedCrawlerActions(disconnected)).toEqual([])
  })

  it('exposes only state-valid operations', () => {
    expect(allowedCrawlerActions(status)).toEqual(['pause'])
    expect(allowedCrawlerActions({ ...status, status: 'paused' })).toEqual(['resume', 'retry'])
    expect(allowedCrawlerActions({ ...status, status: 'failed' })).toEqual(['retry'])
    expect(allowedCrawlerActions({ ...status, status: 'completed' })).toEqual([])
    expect(allowedCrawlerActions({ ...status, controlAvailable: false })).toEqual([])
  })

  it('builds a bounded diagnostic payload without opaque details', () => {
    const payload = safeCrawlerErrorPayload(status, {
      errorId: 'err-1',
      at: '2026-10-05T12:00:00Z',
      code: 'upstream_timeout',
      errorClass: 'upstream',
      message: 'Market response timed out',
      retryable: true,
      itemId: 42,
      attempt: 2,
    })
    expect(payload.schema).toBe('aion2.crawler.error.llm/v1')
    expect(payload.checkpointRevision).toBe(7)
    expect(payload.itemId).toBe(42)
    expect(Object.keys(payload)).not.toContain('raw')
    expect(Object.keys(payload)).not.toContain('token')
  })

  it('keeps safe crawler IDs in the actual LLM clipboard context', () => {
    const safe = safeCrawlerErrorPayload(status, {
      errorId: 'err-copy', at: status.updatedAt!, code: 'upstream_timeout',
      errorClass: 'upstream', message: 'safe', retryable: true, itemId: 516150001,
    })
    const clipboard = buildLlmClipboardPayload(
      safe.message,
      crawlerErrorClipboardContext(safe),
      'https://economy.example.test/crawler',
    )
    expect(clipboard).toContain('runId=run-42')
    expect(clipboard).toContain('errorId=err-copy')
    expect(clipboard).toContain('itemId=516150001')
    expect(clipboard).toContain('checkpointRevision=7')
    expect(clipboard).not.toContain('run_id=')
  })

  it('redacts credentials embedded in a crawler error message', () => {
    const payload = safeCrawlerErrorPayload(status, {
      errorId: 'err-2', at: status.updatedAt!, code: 'upstream_error',
      errorClass: 'upstream', message: 'authorization=private-value Bearer abc.def',
      retryable: false,
    })
    expect(payload.message).not.toContain('private-value')
    expect(payload.message).not.toContain('abc.def')
  })

  it('copies only bounded integer item IDs to LLM diagnostics', () => {
    const baseError = {
      errorId: 'err-3', at: status.updatedAt!, code: 'upstream_error',
      errorClass: 'upstream' as const, message: 'safe', retryable: false,
    }
    expect(safeCrawlerErrorPayload(status, { ...baseError, itemId: 516150001 }).itemId).toBe(516150001)
    expect(safeCrawlerErrorPayload(status, { ...baseError, itemId: Number.POSITIVE_INFINITY }).itemId).toBeNull()
    expect(safeCrawlerErrorPayload(status, { ...baseError, itemId: -1 }).itemId).toBeNull()
    expect(safeCrawlerErrorPayload(status, {
      ...baseError,
      itemId: 'authorization=private-value' as unknown as number,
    }).itemId).toBeNull()
  })

  it('queues actions through the canonical endpoint and unwraps returned status', async () => {
    const fetcher = async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('/api/v1/admin/crawler/actions')
      expect(init?.headers).toMatchObject({ 'X-CSRF-Token': 'csrf-value' })
      expect(JSON.parse(String(init?.body))).toMatchObject({
        action: 'pause',
        expectedRevision: 7,
        idempotencyKey: 'crawler-pause-7',
      })
      return new Response(JSON.stringify({ status: { ...status, status: 'pausing' } }), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    const api = createCrawlerApi('/api/v1', fetcher as typeof fetch, 'csrf-value')
    const result = await api.execute('pause', {
      expectedRevision: 7,
      idempotencyKey: 'crawler-pause-7',
    })
    expect(result.status).toBe('pausing')
  })
})
