[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$source = Join-Path $root '.env.example'
$target = Join-Path $root '.env'
if (Test-Path -LiteralPath $target) { throw 'self_hosted_env_already_exists' }

$passwordBytes = [byte[]]::new(32)
$sessionBytes = [byte[]]::new(32)
[Security.Cryptography.RandomNumberGenerator]::Fill($passwordBytes)
[Security.Cryptography.RandomNumberGenerator]::Fill($sessionBytes)
$password = [Convert]::ToBase64String($passwordBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
$session = [Convert]::ToBase64String($sessionBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
$value = (Get-Content -LiteralPath $source -Raw).
    Replace('replace-with-a-long-random-value', $password).
    Replace('replace-with-base64url-32-random-bytes', $session)
[IO.File]::WriteAllText($target, $value, [Text.UTF8Encoding]::new($false))
Write-Output '{"status":"env_initialized","secrets_printed":false}'
