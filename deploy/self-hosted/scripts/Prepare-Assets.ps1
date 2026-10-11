[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$project = (Resolve-Path (Join-Path $root '..\..')).Path
$manifest = Join-Path $project 'exports\economy\item-icons.json'
$source = Join-Path $project 'exports\economy\item-icons'
$destination = Join-Path $root 'runtime\public'
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw 'item_icon_manifest_missing_regenerate_client_derived_assets'
}
if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    Write-Warning 'Optional client-derived PNG assets are absent; the UI will use built-in fallbacks.'
    Write-Output (([ordered]@{status='item_icons_optional_missing';staged=0}) | ConvertTo-Json -Compress)
    exit 0
}
[void](New-Item -ItemType Directory -Force -Path $destination)
& python -B (Join-Path $project 'scripts\stage_economy_web_assets.py') `
    --manifest $manifest --source-dir $source --dist-dir $destination
exit $LASTEXITCODE
