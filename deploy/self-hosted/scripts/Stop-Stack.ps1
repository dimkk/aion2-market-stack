[CmdletBinding()]
param([switch]$RemoveDatabase)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$arguments = @(
    'compose', '--project-directory', $root,
    '--env-file', (Join-Path $root '.env'),
    '-f', (Join-Path $root 'compose.yaml'),
    'down', '--remove-orphans'
)
if ($RemoveDatabase) { $arguments += '--volumes' }
& docker @arguments
if ($LASTEXITCODE -ne 0) { throw 'docker_compose_down_failed' }
Write-Output (([ordered]@{
    status='self_hosted_stack_stopped'
    database_removed=[bool]$RemoveDatabase
}) | ConvertTo-Json -Compress)
