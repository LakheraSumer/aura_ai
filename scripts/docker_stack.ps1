# Runs the app as containers next to the infra from infrastructure/docker/docker-compose.yml.
#   powershell -ExecutionPolicy Bypass -File scripts\docker_stack.ps1 up        # api :8800 + worker + webhook-only instance
#   powershell -ExecutionPolicy Bypass -File scripts\docker_stack.ps1 tunnel    # public tunnel -> webhook-only instance (new URL)
#   powershell -ExecutionPolicy Bypass -File scripts\docker_stack.ps1 down
# Add -Dev to mount services/api/src over the installed package (code changes without an image rebuild).
# Settings come from services/api/.env and are read when containers are CREATED: after editing .env, run `up`
# again (the tunnel and its URL are left alone).
param([ValidateSet('up', 'tunnel', 'down')][string]$Action = 'up', [switch]$Dev)

$root = Split-Path $PSScriptRoot -Parent
$envFile = Join-Path $root 'services\api\.env'
$image = 'apna-backend:dev'
$common = @('--network', 'apna-ai_default', '--env-file', $envFile,
    '-e', 'DATABASE_URL=postgresql+asyncpg://apna:apna_dev_only@postgres:5432/apna',
    '-e', 'TEMPORAL_ADDRESS=temporal:7233', '-e', 'REDIS_URL=redis://redis:6379/0',
    # The base image has no Chromium and no browser worker runs here: browser steps answer "not supported".
    '-e', 'BROWSER_ENABLED=false')
if ($Dev) {
    $common += @('-v', "$(Join-Path $root 'services\api\src\apna'):/usr/local/lib/python3.12/site-packages/apna:ro",
        '-v', "$(Join-Path $root 'services\api\migrations'):/app/migrations:ro")
}

function Remove-Apps([string[]]$names) { foreach ($n in $names) { docker rm -f $n 2>$null | Out-Null } }

switch ($Action) {
    'down' { Remove-Apps @('apna-tunnel', 'apna-webhook', 'apna-api', 'apna-worker') }
    'up' {
        Remove-Apps @('apna-api', 'apna-worker', 'apna-webhook')
        docker run --rm @common $image alembic upgrade head
        docker run -d --name apna-worker --restart unless-stopped @common $image python -m apna.workflows.worker | Out-Null
        docker run -d --name apna-api --restart unless-stopped -p 127.0.0.1:8800:8800 @common $image | Out-Null
        # The only instance the tunnel reaches: serves /v1/webhooks/* and /healthz, nothing else.
        docker run -d --name apna-webhook --restart unless-stopped @common -e WEBHOOK_ONLY=true $image | Out-Null
        Write-Host 'api: http://127.0.0.1:8800 (docs /docs)   worker: apna-worker   webhook-only: apna-webhook'
    }
    'tunnel' {
        Remove-Apps @('apna-tunnel')
        docker run -d --name apna-tunnel --network apna-ai_default cloudflare/cloudflared:latest `
            tunnel --no-autoupdate --url http://apna-webhook:8800 | Out-Null
        $url = $null
        foreach ($i in 1..30) {
            $m = docker logs apna-tunnel 2>&1 | ForEach-Object { $_.ToString() } |
                Select-String -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
            if ($m) { $url = $m.Matches[0].Value; break }
            Start-Sleep -Seconds 1
        }
        if (-not $url) { Write-Host 'Tunnel URL not found yet; check: docker logs apna-tunnel'; exit 1 }
        Write-Host "WhatsApp callback URL: $url/v1/webhooks/whatsapp   (update it in Meta: the URL changes on restart)"
        Write-Host "Telegram webhook URL:  $url/v1/webhooks/telegram"
    }
}
