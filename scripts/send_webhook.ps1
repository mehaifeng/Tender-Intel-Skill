# Windows 参数兼容入口；发送与去重只保留一套 Python 实现。
param(
    [Parameter(Mandatory=$true)][string]$PayloadPath,
    [string]$WebhookUrl,
    [string]$ManifestPath,
    [switch]$DryRun,
    [switch]$Live
)
if ($DryRun -eq $Live) {
    Write-Error "必须且只能选择 -DryRun 或 -Live"
    exit 2
}
$python = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $python) { Write-Error "需要 Python 3.9+"; exit 2 }
$arguments = @((Join-Path $PSScriptRoot "send_webhook.py"), "--payload", $PayloadPath)
if ($ManifestPath) { $arguments += @("--manifest", $ManifestPath) }
if ($WebhookUrl) { $arguments += @("--webhook-url", $WebhookUrl) }
if ($DryRun) { $arguments += "--dry-run" } else { $arguments += "--live" }
& $python.Source @arguments
exit $LASTEXITCODE
