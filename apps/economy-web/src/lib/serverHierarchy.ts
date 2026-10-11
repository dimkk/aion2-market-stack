import type { EconomyServer } from '../types'

export type EconomyRegionGroup = {
  region: string
  dimensionsConfirmed: boolean
  serverMarkets: EconomyServer[]
  crossServerMarkets: EconomyServer[]
}

function byNameThenId(left: EconomyServer, right: EconomyServer): number {
  return left.name.localeCompare(right.name) || left.serverId - right.serverId
}

export function groupEconomyServers(servers: EconomyServer[]): EconomyRegionGroup[] {
  const grouped = new Map<string, EconomyRegionGroup>()
  for (const server of servers) {
    const key = server.dimensionsConfirmed ? server.region : 'unconfirmed'
    const group = grouped.get(key) ?? {
      region: key,
      dimensionsConfirmed: server.dimensionsConfirmed,
      serverMarkets: [],
      crossServerMarkets: [],
    }
    group.dimensionsConfirmed &&= server.dimensionsConfirmed
    if (server.market === 'cross-server') group.crossServerMarkets.push(server)
    else group.serverMarkets.push(server)
    grouped.set(key, group)
  }
  return [...grouped.values()]
    .map((group) => ({
      ...group,
      serverMarkets: [...group.serverMarkets].sort(byNameThenId),
      crossServerMarkets: [...group.crossServerMarkets].sort(byNameThenId),
    }))
    .sort((left, right) => {
      if (left.dimensionsConfirmed !== right.dimensionsConfirmed) return left.dimensionsConfirmed ? -1 : 1
      return left.region.localeCompare(right.region)
    })
}
