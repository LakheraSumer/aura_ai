# Writes one secret into services/api/.env without echoing it, so it never appears in chat or logs.
# Usage (run it yourself):
#   powershell -ExecutionPolicy Bypass -File scripts\set_secret.ps1 WHATSAPP_ACCESS_TOKEN
#   powershell -ExecutionPolicy Bypass -File scripts\set_secret.ps1 ANTHROPIC_API_KEY
param([Parameter(Mandatory = $true)][ValidatePattern('^[A-Z][A-Z0-9_]+$')][string]$Name)

$envFile = Join-Path $PSScriptRoot '..\services\api\.env'
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $PSScriptRoot '..\.env.example') $envFile }

$sec = Read-Host "Paste the value for $Name (input hidden), then press Enter" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
try { $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr).Trim() }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
if (-not $value) { Write-Host 'Nothing entered. Nothing changed.'; exit 1 }

$path = (Resolve-Path $envFile).Path
$lines = [System.Collections.Generic.List[string]](Get-Content $path)
$i = $lines.FindIndex({ param($l) $l.StartsWith("$Name=") })
if ($i -ge 0) { $lines[$i] = "$Name=$value" } else { $lines.Add("$Name=$value") }
[IO.File]::WriteAllLines($path, [string[]]$lines)   # UTF-8, no BOM
Write-Host "Saved $Name to services\api\.env (length $($value.Length)). You can close this tab."
