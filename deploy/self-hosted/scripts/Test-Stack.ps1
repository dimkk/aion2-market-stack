[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $root '.env'
$sitePort = '8080'
Get-Content -LiteralPath $envFile | ForEach-Object {
    if ($_ -match '^SITE_PORT=([0-9]{1,5})$') { $sitePort = $Matches[1] }
}
& docker compose --project-directory $root --env-file $envFile -f (Join-Path $root 'compose.yaml') ps
if ($LASTEXITCODE -ne 0) { throw 'docker_compose_ps_failed' }
$site = "http://127.0.0.1:$sitePort"
$health = Invoke-RestMethod -Method Get -Uri "$site/api/v1/health" -TimeoutSec 10
if ($health.status -ne 'ok') { throw 'economy_api_health_failed' }
Write-Output (([ordered]@{status='self_hosted_stack_healthy';url=$site}) | ConvertTo-Json -Compress)
