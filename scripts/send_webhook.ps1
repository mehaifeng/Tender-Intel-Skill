# 发送 JSON 载荷到飞书 Webhook
# 用法: powershell -File send_webhook.ps1 -PayloadPath payload.json [-WebhookUrl <url>] [-DryRun]
#   -DryRun 只校验不发送：解析 JSON、检查平铺与 JSON null、打印将要发送的内容
# 退出码: 0 成功 / 1 文件或 JSON 错误 / 2 载荷校验不通过（仅 DryRun）
param(
    [Parameter(Mandatory=$true)]
    [string]$PayloadPath,
    [string]$WebhookUrl = "https://cp-pharm.feishu.cn/base/workflow/webhook/event/BiHYar0YcwkDhYhjVF4cVFMgnJf",
    [switch]$DryRun
)

if (-not (Test-Path $PayloadPath)) {
    Write-Error "Payload file not found: $PayloadPath"
    exit 1
}

# 按 UTF-8 读取并作为字节发送，避免中文乱码
$json = Get-Content -Raw -Encoding UTF8 $PayloadPath
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)

if ($DryRun) {
    try {
        $obj = $json | ConvertFrom-Json
    } catch {
        Write-Error ("DRY-RUN: JSON 解析失败 -> " + $_.Exception.Message)
        exit 1
    }

    Write-Output "DRY-RUN (未发送)"
    Write-Output ("  URL    : " + $WebhookUrl)
    Write-Output ("  字节数 : " + $bytes.Length)

    if ($obj -is [System.Object[]]) {
        Write-Output "  ---- 载荷 ----"
        Write-Output $json
        Write-Warning "载荷是数组：飞书 Webhook 每次只接受一条平铺记录，多条必须逐条推送"
        exit 2
    }

    $nullFields = @()
    $nestedFields = @()
    foreach ($p in $obj.PSObject.Properties) {
        if ($null -eq $p.Value) {
            $nullFields += $p.Name
        } elseif ($p.Value -is [System.Management.Automation.PSCustomObject] -or $p.Value -is [System.Object[]]) {
            $nestedFields += $p.Name
        }
    }

    Write-Output ("  字段数 : " + @($obj.PSObject.Properties).Count)
    Write-Output "  ---- 载荷 ----"
    Write-Output $json

    $bad = $false
    if ($nullFields.Count -gt 0) {
        Write-Warning ('含 JSON null 字段（飞书会报字段识别错误，字符串应填 "null"、数字应填 0）: ' + ($nullFields -join ", "))
        $bad = $true
    }
    if ($nestedFields.Count -gt 0) {
        Write-Warning ("含嵌套字段（飞书要求全部平铺）: " + ($nestedFields -join ", "))
        $bad = $true
    }
    if ($bad) { exit 2 }

    Write-Output "校验通过：单条、平铺、无 JSON null"
    exit 0
}

try {
    $r = Invoke-WebRequest -Uri $WebhookUrl -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body $bytes -TimeoutSec 30 -UseBasicParsing
    Write-Output ("HTTP " + $r.StatusCode)
    Write-Output $r.Content
} catch {
    Write-Error ("Request failed: " + $_.Exception.Message)
    if ($_.Exception.Response) {
        Write-Error ("Status: " + [int]$_.Exception.Response.StatusCode)
    }
    exit 1
}
