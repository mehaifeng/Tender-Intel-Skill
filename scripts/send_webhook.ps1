# 发送 JSON 载荷到飞书 Webhook
# 用法: powershell -File send_webhook.ps1 -PayloadPath payload.json -Flow create|update [-WebhookUrl <url>] [-DryRun|-Live -ManifestPath manifest.json]
#   -DryRun 只校验不发送：解析 JSON、检查平铺与 JSON null、打印将要发送的内容
# 退出码: 0 成功 / 1 文件或 JSON 错误 / 2 载荷校验不通过（仅 DryRun）
param(
    [Parameter(Mandatory=$true)]
    [string]$PayloadPath,
    [ValidateSet("create", "update")]
    [string]$Flow = "create",
    [string]$WebhookUrl,
    [string]$ManifestPath,
    [switch]$DryRun,
    [switch]$Live
)

$explicitWebhookUrl = -not [string]::IsNullOrWhiteSpace($WebhookUrl)

if (-not (Test-Path $PayloadPath)) {
    Write-Error "Payload file not found: $PayloadPath"
    exit 1
}

# 按 UTF-8 读取并作为字节发送，避免中文乱码
$json = Get-Content -Raw -Encoding UTF8 $PayloadPath
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)

try {
    $obj = $json | ConvertFrom-Json
} catch {
    Write-Error ("JSON 解析失败 -> " + $_.Exception.Message)
    exit 1
}

if ($obj -is [System.Object[]]) {
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

$createFields = @("title", "record_id", "region", "project_code", "purchaser", "agency", "procurement_method", "notice_type", "category", "budget", "tech_key_points", "publish_date", "doc_fetch_end", "deadline", "days_left", "contact", "source_url", "attachment", "match_level", "matched_category", "status", "requires_manual", "designated_supplier", "winner", "award_amount", "notes")
$updateFields = @("record_id", "change_type", "status", "notice_type", "publish_date", "deadline", "winner", "award_amount", "designated_supplier", "budget", "contact", "attachment", "source_url", "notes")
$expectedFields = if ($Flow -eq "create") { $createFields } else { $updateFields }
$actualFields = @($obj.PSObject.Properties.Name)
$missingFields = @($expectedFields | Where-Object { $actualFields -notcontains $_ })
$extraFields = @($actualFields | Where-Object { $expectedFields -notcontains $_ })

$bad = $false
if ($nullFields.Count -gt 0) {
    Write-Warning ('含 JSON null 字段（字符串应填 "null"、数字应填 0）: ' + ($nullFields -join ", "))
    $bad = $true
}
if ($nestedFields.Count -gt 0) {
    Write-Warning ("含嵌套字段（飞书要求全部平铺）: " + ($nestedFields -join ", "))
    $bad = $true
}
if ($missingFields.Count -gt 0) {
    Write-Warning ("缺少 $Flow 流字段: " + ($missingFields -join ", "))
    $bad = $true
}
if ($extraFields.Count -gt 0) {
    Write-Warning ("包含 $Flow 流不允许的字段: " + ($extraFields -join ", "))
    $bad = $true
}
if ($bad) { exit 2 }

if (-not $WebhookUrl) {
    $WebhookUrl = if ($Flow -eq "create") { $env:FEISHU_CREATE_WEBHOOK_URL } else { $env:FEISHU_UPDATE_WEBHOOK_URL }
}

if ($DryRun) {

    Write-Output "DRY-RUN (未发送)"
    Write-Output ("  流程   : " + $Flow)
    Write-Output ("  URL配置: " + $(if ($WebhookUrl) { "是" } else { "否" }))
    Write-Output ("  字节数 : " + $bytes.Length)
    Write-Output ("  字段数 : " + @($obj.PSObject.Properties).Count)
    Write-Output "  ---- 载荷 ----"
    Write-Output $json
    Write-Output "校验通过：字段集正确、单条、平铺、无 JSON null"
    exit 0
}

if (-not $Live) {
    Write-Error "拒绝发送：生产推送必须显式传入 -Live；调试请使用 -DryRun"
    exit 2
}
if ($explicitWebhookUrl) {
    Write-Error "拒绝发送：生产模式不接受命令行-WebhookUrl，必须使用受保护的环境变量"
    exit 2
}
if (-not $ManifestPath) {
    Write-Error "拒绝发送：生产推送必须提供 -ManifestPath，由运行清单确认模式与状态"
    exit 2
}
try {
    $manifest = Get-Content -Raw -Encoding UTF8 $ManifestPath | ConvertFrom-Json
} catch {
    Write-Error ("无法读取运行清单 -> " + $_.Exception.Message)
    exit 1
}
$manifestFullPath = [System.IO.Path]::GetFullPath($ManifestPath)
$declaredManifestPath = [System.IO.Path]::Combine(
    [System.IO.Path]::GetFullPath([string]$manifest.pipeline_dir),
    "manifest.json"
)
if (-not $manifestFullPath.Equals($declaredManifestPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "拒绝发送：ManifestPath与manifest声明的pipeline_dir不一致"
    exit 2
}
if ($manifest.live_push_allowed -ne $true -or $manifest.state -ne "VALIDATED") {
    Write-Error ("拒绝发送：manifest必须为 live_push_allowed=true 且 state=VALIDATED，当前 mode=" + $manifest.mode + " state=" + $manifest.state)
    exit 2
}
if ($Flow -eq "create" -and $manifest.mode -ne "daily-push") {
    Write-Error ("拒绝创建流：当前mode=" + $manifest.mode + "，只有daily-push允许创建")
    exit 2
}
if ($Flow -eq "update" -and @("daily-push", "update-only") -notcontains $manifest.mode) {
    Write-Error ("拒绝更新流：当前mode=" + $manifest.mode)
    exit 2
}
$payloadRoot = [System.IO.Path]::GetFullPath([string]$manifest.payload_dir)
$requiredRoot = [System.IO.Path]::Combine($payloadRoot, $Flow)
$payloadFullPath = [System.IO.Path]::GetFullPath($PayloadPath)
if (-not $payloadFullPath.StartsWith($requiredRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "拒绝发送：PayloadPath必须位于manifest对应的pipeline/payloads/$Flow目录"
    exit 2
}
$candidateId = [System.IO.Path]::GetFileNameWithoutExtension($payloadFullPath)
$sha256 = [System.BitConverter]::ToString(
    [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
).Replace("-", "").ToLowerInvariant()
$payloadMeta = @($manifest.payloads | Where-Object {
    $_.flow -eq $Flow -and $_.candidate_id -eq $candidateId
})
if ($payloadMeta.Count -ne 1) {
    Write-Error "拒绝发送：manifest中没有唯一匹配的已验证载荷"
    exit 2
}
$manifestPayloadPath = [System.IO.Path]::GetFullPath([string]$payloadMeta[0].path)
if (-not $payloadFullPath.Equals($manifestPayloadPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "拒绝发送：载荷路径与manifest记录不一致"
    exit 2
}
if ($sha256 -ne [string]$payloadMeta[0].sha256) {
    Write-Error "拒绝发送：载荷在批次校验后被修改，SHA-256与manifest不一致"
    exit 2
}
if (-not $WebhookUrl) {
    Write-Error "拒绝发送：未配置 WebhookUrl。设置 FEISHU_CREATE_WEBHOOK_URL / FEISHU_UPDATE_WEBHOOK_URL，或显式传 -WebhookUrl"
    exit 1
}

try {
    $r = Invoke-WebRequest -Uri $WebhookUrl -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body $bytes -TimeoutSec 30 -UseBasicParsing
    Write-Output ("HTTP " + $r.StatusCode)
    Write-Output $r.Content
    if ([int]$r.StatusCode -ne 200) {
        Write-Error "飞书HTTP状态不是200，拒绝生成成功回执"
        exit 1
    }
    try {
        $responseObj = $r.Content | ConvertFrom-Json
        if ($null -eq $responseObj.code -or [int]$responseObj.code -ne 0) {
            Write-Error "飞书返回未确认成功（要求 code: 0），拒绝更新 seen.json"
            exit 1
        }
    } catch {
        Write-Error ("飞书返回不是可验证的 JSON，拒绝视为成功 -> " + $_.Exception.Message)
        exit 1
    }
    $receiptDir = [System.IO.Path]::Combine([string]$manifest.pipeline_dir, "receipts")
    [System.IO.Directory]::CreateDirectory($receiptDir) | Out-Null
    $receiptPath = [System.IO.Path]::Combine($receiptDir, ($Flow + "-" + $candidateId + ".json"))
    $receipt = [ordered]@{
        schema_version = 1
        flow = $Flow
        candidate_id = $candidateId
        payload_path = $payloadFullPath
        payload_sha256 = $sha256
        http_status = 200
        feishu_code = 0
        confirmed_at = [System.DateTimeOffset]::Now.ToString("o")
    }
    $receiptJson = $receipt | ConvertTo-Json -Depth 4
    $receiptTemp = $receiptPath + ".tmp"
    [System.IO.File]::WriteAllText(
        $receiptTemp,
        $receiptJson + [Environment]::NewLine,
        (New-Object System.Text.UTF8Encoding($false))
    )
    Move-Item -Force $receiptTemp $receiptPath
    Write-Output "飞书确认成功：code 0"
    Write-Output ("成功回执: " + $receiptPath)
    Write-Output "下一步必须用 tender_pipeline.py record-push 核验回执后更新 seen"
} catch {
    Write-Error ("Request failed: " + $_.Exception.Message)
    if ($_.Exception.Response) {
        Write-Error ("Status: " + [int]$_.Exception.Response.StatusCode)
    }
    exit 1
}
