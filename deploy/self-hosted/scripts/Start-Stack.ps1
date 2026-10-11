[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $root '.env'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw 'run_Initialize-Env_first' }
foreach ($directory in @('runtime\inbox','runtime\private','runtime\state','runtime\snapshots','runtime\public')) {
    [void](New-Item -ItemType Directory -Force -Path (Join-Path $root $directory))
}
& (Join-Path $PSScriptRoot 'Prepare-SeedData.ps1')
if ($LASTEXITCODE -ne 0) { throw 'self_hosted_seed_staging_failed' }
& (Join-Path $PSScriptRoot 'Prepare-Assets.ps1')
if ($LASTEXITCODE -ne 0) { throw 'self_hosted_asset_staging_failed' }
& docker compose --project-directory $root --env-file $envFile -f (Join-Path $root 'compose.yaml') up -d --build
if ($LASTEXITCODE -ne 0) { throw 'docker_compose_up_failed' }
$sitePort = '8080'
Get-Content -LiteralPath $envFile | ForEach-Object {
    if ($_ -match '^SITE_PORT=([0-9]{1,5})$') { $sitePort = $Matches[1] }
}
Write-Output (([ordered]@{status='self_hosted_stack_started';site="http://127.0.0.1:$sitePort"}) | ConvertTo-Json -Compress)
