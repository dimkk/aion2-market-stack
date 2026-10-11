[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
if (Get-Process -Name steam -ErrorAction SilentlyContinue) {
    throw 'steam_already_running_close_it_once_so_the_keylog_environment_is_inherited'
}
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $root '.env'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw 'run_Initialize-Env_first' }
$settings = @{}
Get-Content -LiteralPath $envFile | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') { $settings[$Matches[1].Trim()] = $Matches[2].Trim() }
}
$keylog = Join-Path $root $settings['AION2_HOST_KEYLOG']
[void](New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keylog))
$env:SSLKEYLOGFILE = $keylog
$steamPath = (Get-ItemProperty -LiteralPath 'HKCU:\Software\Valve\Steam' -ErrorAction Stop).SteamExe
if (-not (Test-Path -LiteralPath $steamPath -PathType Leaf)) { throw 'steam_executable_not_found' }
Start-Process -FilePath $steamPath -ArgumentList '-applaunch','3393110'
Write-Output '{"status":"aion2_launch_requested","tls_keylog_configured":true}'
