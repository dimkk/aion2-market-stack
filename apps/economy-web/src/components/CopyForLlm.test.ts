import { describe, expect, it } from 'vitest'

import { buildLlmClipboardPayload, copyForLlmLabel, llmSafeText } from './CopyForLlm'

describe('CopyForLlm safety', () => {
  it('allowlists diagnostic context and omits account identity', () => {
    const payload = buildLlmClipboardPayload(
      'Registration awaits approval',
      { severity: 'warning', state: 'pending', username: 'private-owner', token: 'must-not-copy' },
      'https://economy.example.test/account',
    )
    expect(payload).toContain('severity=warning')
    expect(payload).not.toContain('private-owner')
    expect(payload).not.toContain('must-not-copy')
  })

  it('redacts bearer, jwt, scoped tokens and named secrets from messages', () => {
    const jwt = `eyJ${'a'.repeat(12)}.${'b'.repeat(12)}.${'c'.repeat(10)}`
    const input = `Bearer abc.def token=a2farm_${'x'.repeat(43)} jwt=${jwt} password=hunter2`
    const safe = llmSafeText(input)
    expect(safe).not.toContain('abc.def')
    expect(safe).not.toContain('hunter2')
    expect(safe).not.toContain(jwt)
    expect(safe).not.toContain('a2farm_')
  })

  it('localizes idle, success and failure labels', () => {
    expect(copyForLlmLabel('ru', 'idle')).toBe('Скопировать для LLM')
    expect(copyForLlmLabel('en', 'idle')).toBe('Copy for LLM')
    expect(copyForLlmLabel('en', 'ok')).toBe('Copied')
    expect(copyForLlmLabel('en', 'error')).toBe('Copy failed')
  })
})
