import type {
  CrawlerAction,
  CrawlerActionRequest,
  CrawlerApi,
  CrawlerError,
  CrawlerMetrics,
  CrawlerStatus,
  SafeCrawlerErrorPayload,
} from './types'
import { llmSafeText } from '../../components/CopyForLlm'
import type { AppLocale } from '../../lib/i18n'

const clamp = (value: number, minimum: number, maximum: number) => Math.min(maximum, Math.max(minimum, value))

export const CRAWLER_HEARTBEAT_STALE_SECONDS = 120

export type CrawlerHeartbeatFreshness = {
  state: 'fresh' | 'stale' | 'future' | 'unknown'
  ageSeconds: number | null
}

export function crawlerHeartbeatFreshness(
  updatedAt: string | null,
  nowMs = Date.now(),
  staleAfterSeconds = CRAWLER_HEARTBEAT_STALE_SECONDS,
): CrawlerHeartbeatFreshness {
  if (!updatedAt) return { state: 'unknown', ageSeconds: null }
  const updatedAtMs = Date.parse(updatedAt)
  if (!Number.isFinite(updatedAtMs) || !Number.isFinite(nowMs)) {
    return { state: 'unknown', ageSeconds: null }
  }
  const ageSeconds = Math.floor((nowMs - updatedAtMs) / 1000)
  return {
    state: ageSeconds < 0 ? 'future' : ageSeconds > staleAfterSeconds ? 'stale' : 'fresh',
    ageSeconds,
  }
}

const safeCrawlerItemId = (value: unknown): number | null => (
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
)

export function calculateCrawlerMetrics(status: CrawlerStatus): CrawlerMetrics {
  const elapsed = Number.isFinite(status.elapsedSeconds) ? Math.max(0, status.elapsedSeconds) : 0
  const totalItems = Number.isFinite(status.totalItems) ? Math.max(0, status.totalItems) : 0
  const processedItems = clamp(Number.isFinite(status.processedItems) ? status.processedItems : 0, 0, totalItems)
  const successful = Math.max(0, status.successfulRequests)
  const failed = Math.max(0, status.failedRequests)
  const totalRequests = successful + failed
  const itemsPerSecond = elapsed > 0 ? processedItems / elapsed : 0
  const requestsPerSecond = elapsed > 0 ? totalRequests / elapsed : 0
  const remainingItems = Math.max(0, totalItems - processedItems)

  return {
    coveragePercent: totalItems > 0 ? clamp((processedItems / totalItems) * 100, 0, 100) : 0,
    itemsPerSecond,
    requestsPerSecond,
    etaSeconds: remainingItems === 0 ? 0 : itemsPerSecond > 0 ? Math.ceil(remainingItems / itemsPerSecond) : null,
    remainingItems,
    totalRequests,
  }
}

export function allowedCrawlerActions(status: CrawlerStatus): CrawlerAction[] {
  if (status.controlAvailable === false) return []
  if (status.status === 'running') return ['pause']
  if (status.status === 'paused') return ['resume', 'retry']
  if (status.status === 'failed') return ['retry']
  return []
}

export function safeCrawlerErrorPayload(status: CrawlerStatus, error: CrawlerError): SafeCrawlerErrorPayload {
  return {
    schema: 'aion2.crawler.error.llm/v1',
    severity: error.retryable ? 'warning' : 'error',
    state: status.status,
    runId: status.runId,
    revision: status.revision,
    phase: status.phase,
    errorId: error.errorId,
    errorClass: error.errorClass,
    code: error.code,
    message: llmSafeText(error.message),
    retryable: error.retryable,
    itemId: safeCrawlerItemId(error.itemId),
    attempt: error.attempt ?? null,
    occurredAt: error.at,
    checkpointRevision: status.checkpoint?.revision ?? null,
    checkpointProcessedItems: status.checkpoint?.processedItems ?? null,
  }
}

export function crawlerErrorClipboardContext(
  safe: SafeCrawlerErrorPayload,
): Record<string, string | number | boolean | null> {
  return {
    schema: safe.schema,
    severity: safe.severity,
    state: safe.state,
    runId: safe.runId,
    revision: safe.revision,
    phase: safe.phase,
    errorId: safe.errorId,
    errorClass: safe.errorClass,
    code: safe.code,
    retryable: safe.retryable,
    itemId: safe.itemId,
    attempt: safe.attempt,
    occurredAt: safe.occurredAt,
    checkpointRevision: safe.checkpointRevision,
    checkpointProcessedItems: safe.checkpointProcessedItems,
  }
}

export function createIdempotencyKey(action: CrawlerAction, runId: string | null, revision: number): string {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `crawler:${action}:${runId ?? 'none'}:${revision}:${random}`
}

type FetchLike = typeof fetch

export function createCrawlerApi(baseUrl: string, fetcher: FetchLike = fetch, csrfToken?: string | null): CrawlerApi {
  const root = baseUrl.replace(/\/$/, '')
  const parse = async (response: Response): Promise<CrawlerStatus> => {
    if (!response.ok) throw new Error(`crawler_api_http_${response.status}`)
    return response.json() as Promise<CrawlerStatus>
  }
  return {
    async getStatus(signal) {
      return parse(await fetcher(`${root}/admin/crawler/status`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
        signal,
      }))
    },
    async execute(action: CrawlerAction, request: CrawlerActionRequest, signal) {
      const response = await fetcher(`${root}/admin/crawler/actions`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'Idempotency-Key': request.idempotencyKey,
          ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
        },
        body: JSON.stringify({ ...request, action }),
        signal,
      })
      if (!response.ok) throw new Error(`crawler_api_http_${response.status}`)
      const payload = await response.json() as { status?: CrawlerStatus }
      if (!payload.status) throw new Error('crawler_api_invalid_response')
      return payload.status
    },
  }
}

export function formatDuration(seconds: number | null, locale: AppLocale = 'ru'): string {
  if (seconds === null || !Number.isFinite(seconds)) return '—'
  const whole = Math.max(0, Math.ceil(seconds))
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const remainder = whole % 60
  if (locale === 'en') {
    return hours > 0 ? `${hours}h ${minutes}m` : minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`
  }
  return hours > 0 ? `${hours}ч ${minutes}м` : minutes > 0 ? `${minutes}м ${remainder}с` : `${remainder}с`
}
