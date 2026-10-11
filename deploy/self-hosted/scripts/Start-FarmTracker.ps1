[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$project = (Resolve-Path (Join-Path $root '..\..')).Path
$envFile = Join-Path $root '.env'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw 'run_Initialize-Env_first' }
$settings = @{}
Get-Content -LiteralPath $envFile | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') { $settings[$Matches[1].Trim()] = $Matches[2].Trim() }
}

function Setting([string]$Name, [string]$Default) {
    if ($settings.ContainsKey($Name) -and $settings[$Name]) { return $settings[$Name] }
    return $Default
}

if ((Setting 'AION2_FARM_PACKET_BUS_ENABLED' 'false') -ne 'true') {
    throw 'set_AION2_FARM_PACKET_BUS_ENABLED_true'
}
$marketServerId = Setting 'AION2_FARM_MARKET_SERVER_ID' ''
if ($marketServerId -notmatch '^[1-9][0-9]{0,9}$') {
    throw 'set_exact_AION2_FARM_MARKET_SERVER_ID'
}

$env:AION2_FARM_SYNC_TOKEN = Setting 'AION2_FARM_SYNC_TOKEN' ''
$arguments = @(
    '-B', '-m', 'tools.farm_tracker',
    '--history', (Join-Path $root (Setting 'AION2_FARM_HISTORY' 'runtime\state\farm-sessions.sqlite')),
    '--market-database', (Join-Path $root 'runtime\state\market-history.sqlite'),
    '--market-server-id', $marketServerId,
    '--catalog', (Join-Path $project 'exports\economy\item-catalog.json'),
    '--zones', (Join-Path $project 'tools\farm_tracker\data\zone-catalog.json'),
    '--client-build', (Setting 'AION2_CLIENT_BUILD' '25650019'),
    '--packet-bus', (Join-Path $root (Setting 'AION2_FARM_PACKET_BUS' 'runtime\private\packet-live')),
    '--idle-seconds', '15',
    '--listen', '127.0.0.1',
    '--port', (Setting 'AION2_FARM_LISTEN_PORT' '8765'),
    '--sync-origin', (Setting 'AION2_FARM_SYNC_ORIGIN' 'http://127.0.0.1:8080')
)
Push-Location $project
try {
    & python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
    Remove-Item Env:AION2_FARM_SYNC_TOKEN -ErrorAction SilentlyContinue
}
