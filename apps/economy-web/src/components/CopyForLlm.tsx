import { useState, type MouseEvent } from 'react'
import { MessageSquareText } from 'lucide-react'

import { canonicalUrl } from '../lib/utils'
import { Button } from './ui'

const SAFE_CONTEXT_KEYS = new Set([
  'schema', 'severity', 'state', 'build', 'activeItems', 'itemId', 'serverId',
  'horizon', 'observedServerIds', 'recipeId', 'missingItemIds', 'sessionId',
  'payloadId', 'revision', 'endpoint', 'runId', 'phase', 'errorId', 'errorClass',
  'code', 'retryable', 'attempt', 'occurredAt', 'checkpointRevision',
  'checkpointProcessedItems', 'mode', 'updatedAt', 'ageSeconds',
  'thresholdSeconds',
])

export function llmSafeText(value: string, maximum = 512): string {
  return value
    .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+/gi, 'Bearer [redacted]')
    .replace(/\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b/g, '[redacted-jwt]')
    .replace(/\ba2(?:fs|farm|cl)_[A-Za-z0-9_-]{20,}\b/gi, '[redacted-token]')
    .replace(/\b(authorization|cookie|password|secret|token|jwt)\s*[:=]\s*[^\s,;]+/gi, '$1=[redacted]')
    .slice(0, maximum)
}

export function buildLlmClipboardPayload(
  message: string,
  context: Record<string, string | number | boolean | null>,
  url = canonicalUrl(),
): string {
  const safeContext = Object.entries(context)
    .filter(([key]) => SAFE_CONTEXT_KEYS.has(key))
    .map(([key, value]) => `${key}=${typeof value === 'string' ? llmSafeText(value, 256) : value ?? 'null'}`)
  return [llmSafeText(message), ...safeContext, `url=${url}`].join('\n')
}

type CopyLocale = 'ru' | 'en'
type CopyState = 'idle' | 'ok' | 'error'

export function copyForLlmLabel(locale: CopyLocale, state: CopyState): string {
  const labels = {
    ru: { idle: 'Скопировать для LLM', ok: 'Скопировано', error: 'Не удалось' },
    en: { idle: 'Copy for LLM', ok: 'Copied', error: 'Copy failed' },
  }
  return labels[locale][state]
}

export function CopyForLlm({
  message,
  context,
  locale: explicitLocale,
}: {
  message: string
  context: Record<string, string | number | boolean | null>
  locale?: CopyLocale
}) {
  const [state, setState] = useState<'idle' | 'ok' | 'error'>('idle')
  const locale: CopyLocale = explicitLocale
    ?? (typeof document !== 'undefined' && document.documentElement.lang.startsWith('en') ? 'en' : 'ru')
  const copy = async (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    const payload = buildLlmClipboardPayload(message, context)
    try {
      await navigator.clipboard.writeText(payload)
      setState('ok')
    } catch {
      setState('error')
    }
  }
  return <Button variant="outline" onClick={copy}><MessageSquareText className="mr-2 size-4" />{copyForLlmLabel(locale, state)}</Button>
}
