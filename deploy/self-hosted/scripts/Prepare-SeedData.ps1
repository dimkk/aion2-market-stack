[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$project = (Resolve-Path (Join-Path $root '..\..')).Path
$seed = Join-Path $project 'data\seed'
$databaseSource = Join-Path $seed 'state\market-history.sqlite'
$snapshotSource = Join-Path $seed 'snapshots'
$databaseTarget = Join-Path $root 'runtime\state\market-history.sqlite'
$snapshotTarget = Join-Path $root 'runtime\snapshots'

if (-not (Test-Path -LiteralPath $databaseSource -PathType Leaf)) {
    throw 'seed_database_missing'
}
if (-not (Test-Path -LiteralPath (Join-Path $snapshotSource 'current.json') -PathType Leaf)) {
    throw 'seed_snapshot_pointer_missing'
}

[void](New-Item -ItemType Directory -Force -Path (Split-Path -Parent $databaseTarget))
[void](New-Item -ItemType Directory -Force -Path $snapshotTarget)

$databaseSeeded = $false
$snapshotSeeded = $false
if (-not (Test-Path -LiteralPath $databaseTarget -PathType Leaf)) {
    Copy-Item -LiteralPath $databaseSource -Destination $databaseTarget
    $databaseSeeded = $true
}
if (-not (Test-Path -LiteralPath (Join-Path $snapshotTarget 'current.json') -PathType Leaf)) {
    Get-ChildItem -LiteralPath $snapshotSource -Force | Copy-Item -Destination $snapshotTarget -Recurse
    $snapshotSeeded = $true
}

Write-Output (([ordered]@{
    status = 'seed_data_ready'
    database_seeded = $databaseSeeded
    snapshot_seeded = $snapshotSeeded
}) | ConvertTo-Json -Compress)
