export type SiteRole = 'guest' | 'registered' | 'approved' | 'admin'

export type CrawlerRunStatus =
  | 'not_connected'
  | 'idle'
  | 'starting'
  | 'running'
  | 'pausing'
  | 'paused'
  | 'retrying'
  | 'completed'
  | 'failed'

export type CrawlerPhase =
  | 'idle'
  | 'session'
  | 'catalog'
  | 'queue'
  | 'scan'
  | 'items'
  | 'persist'
  | 'publish'
  | 'complete'

export type CrawlerErrorClass =
  | 'authorization'
  | 'network'
  | 'rate_limit'
  | 'upstream'
  | 'schema'
  | 'storage'
  | 'concurrency'
  | 'internal'

export type CrawlerCheckpoint = {
  cursor: string | null
  processedItems: number
  savedAt: string
  revision: number
}

export type CrawlerError = {
  errorId: string
  at: string
  code: string
  errorClass: CrawlerErrorClass
  message: string
  retryable: boolean
  itemId?: number
  attempt?: number
}

export type CrawlerStatus = {
  configured: boolean
  source?: 'control_plane' | 'standalone_object'
  controlAvailable?: boolean
  economyRevision?: string | null
  nextRunAt?: string | null
  minimumNextAttemptAt?: string | null
  workUnit?: 'items' | 'server_scopes'
  lastSuccess?: {
    runId: string | null
    observedAt: string
    economyRevision: string
    release: string
    marketScopeCount: number
    requestCount: number
    transportAttemptCount?: number
    summaryRows: number
    snapshotChanged: boolean
  } | null
  runId: string | null
  status: CrawlerRunStatus
  phase: CrawlerPhase
  revision: number
  updatedAt: string | null
  startedAt: string | null
  elapsedSeconds: number
  totalItems: number
  processedItems: number
  successfulRequests: number
  transportAttempts?: number
  failedRequests: number
  retriedRequests: number
  queueDepth: number
  checkpoint: CrawlerCheckpoint | null
  errors: CrawlerError[]
}

export type CrawlerMetrics = {
  coveragePercent: number
  itemsPerSecond: number
  requestsPerSecond: number
  etaSeconds: number | null
  remainingItems: number
  totalRequests: number
}

export type CrawlerAction = 'pause' | 'resume' | 'retry'

export type CrawlerActionRequest = {
  expectedRevision: number
  idempotencyKey: string
}

export type CrawlerApi = {
  getStatus(signal?: AbortSignal): Promise<CrawlerStatus>
  execute(action: CrawlerAction, request: CrawlerActionRequest, signal?: AbortSignal): Promise<CrawlerStatus>
}

export type SafeCrawlerErrorPayload = {
  schema: 'aion2.crawler.error.llm/v1'
  severity: 'error' | 'warning'
  state: CrawlerRunStatus
  runId: string | null
  revision: number
  phase: CrawlerPhase
  errorId: string
  errorClass: CrawlerErrorClass
  code: string
  message: string
  retryable: boolean
  itemId: number | null
  attempt: number | null
  occurredAt: string
  checkpointRevision: number | null
  checkpointProcessedItems: number | null
}
