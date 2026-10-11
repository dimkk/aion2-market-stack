import { afterEach, describe, expect, it, vi } from 'vitest'

import { canonicalUrl } from './utils'

describe('canonicalUrl', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('drops credentials, query parameters and fragments from LLM diagnostics', () => {
    vi.stubGlobal('window', { location: { href: 'https://operator:p%40ss@economy.example.test/items?token=secret#private' } })
    expect(canonicalUrl()).toBe('https://economy.example.test/items')
  })
})
