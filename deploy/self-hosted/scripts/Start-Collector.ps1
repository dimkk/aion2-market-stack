[CmdletBinding()]
param([switch]$Once)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$project = (Resolve-Path (Join-Path $root '..\..')).Path
$envFile = Join-Path $root '.env'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw 'run_Initialize-Env_first' }
$settings = @{}
Get-Content -LiteralPath $envFile | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') { $settings[$Matches[1].Trim()] = $Matches[2].Trim() }
}
$arguments = @(
    '-B', (Join-Path $project 'tools\public_collector\collector.py'),
    '--spool', (Join-Path $root 'runtime\private\capture'),
    '--inbox', (Join-Path $root 'runtime\inbox'),
    '--keylog', (Join-Path $root $settings['AION2_HOST_KEYLOG']),
    '--seconds', $settings['AION2_CAPTURE_SECONDS'],
    '--interval', $settings['AION2_CAPTURE_INTERVAL_SECONDS']
)
if ($Once) { $arguments += '--once' }
& python @arguments
exit $LASTEXITCODE
