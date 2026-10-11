[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidatePattern('^[a-z0-9_.-]{3,48}$')][string]$Username)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
& docker compose --project-directory $root --env-file (Join-Path $root '.env') `
    -f (Join-Path $root 'compose.yaml') exec api `
    python -m economy_api.bootstrap_admin_postgres --username $Username
exit $LASTEXITCODE
