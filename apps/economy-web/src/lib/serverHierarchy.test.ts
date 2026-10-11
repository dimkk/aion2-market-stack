import { describe, expect, it } from 'vitest'

import type { EconomyServer } from '../types'
import { groupEconomyServers } from './serverHierarchy'

const server = (overrides: Partial<EconomyServer>): EconomyServer => ({
  region: 'EU',
  serverId: 1,
  name: 'EU Test Server',
  faction: 'Test Faction',
  market: 'server',
  dimensionsConfirmed: true,
  dimensionSource: 'client-derived:unit-test:/synthetic',
  lastObservedAt: '2026-10-05T00:00:00Z',
  coverageItems: 1,
  activeItems: 1,
  listings: 1,
  ...overrides,
})

describe('groupEconomyServers', () => {
  it('builds EU and NA regions with a separate cross-server market lane', () => {
    const groups = groupEconomyServers([
      server({ serverId: 11, name: 'EU Test B' }),
      server({ serverId: 10, name: 'EU Test A' }),
      server({ serverId: 19, name: 'EU Test Pool', market: 'cross-server', faction: 'All test factions' }),
      server({ serverId: 20, region: 'NA', name: 'NA Test Server' }),
    ])

    expect(groups.map((group) => group.region)).toEqual(['EU', 'NA'])
    expect(groups[0].serverMarkets.map((row) => row.serverId)).toEqual([10, 11])
    expect(groups[0].crossServerMarkets.map((row) => row.serverId)).toEqual([19])
    expect(groups[1].serverMarkets.map((row) => row.serverId)).toEqual([20])
  })

  it('keeps unknown dimensions in an explicit unconfirmed group', () => {
    const groups = groupEconomyServers([
      server({ serverId: 99, region: 'unconfirmed', name: 'Server 99', faction: 'unknown', dimensionsConfirmed: false, dimensionSource: null }),
    ])

    expect(groups).toHaveLength(1)
    expect(groups[0]).toMatchObject({ region: 'unconfirmed', dimensionsConfirmed: false })
  })

  it('retains confirmed servers with zero current listings', () => {
    const groups = groupEconomyServers([
      server({ serverId: 10, name: 'EU Active' }),
      server({ serverId: 11, name: 'EU Empty', activeItems: 0, listings: 0 }),
    ])

    expect(groups[0].serverMarkets.map((row) => row.serverId)).toEqual([10, 11])
  })
})
