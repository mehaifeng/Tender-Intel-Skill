# 发送一条固定13字段平铺JSON到飞书Webhook。
# DryRun只校验；生产必须显式-Live并由manifest授权。
param(
    [Parameter(Mandatory=$true)]
    [string]$PayloadPath,
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

$json = Get-Content -Raw -Encoding UTF8 $PayloadPath
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
try {
    $obj = $json | ConvertFrom-Json
} catch {
    Write-Error ("JSON解析失败 -> " + $_.Exception.Message)
    exit 1
}

if ($obj -is [System.Object[]]) {
    Write-Warning "载荷必须是单条对象，不能是数组"
    exit 2
}

$expectedFields = @(
    "标题", "单位", "地区", "所属省/市", "所属大区", "发布时间", "截止时间",
    "预算", "采购方式", "内容（检索的摘要）", "链接", "医院全名", "医院等级"
)
$actualFields = @($obj.PSObject.Properties.Name)
$missingFields = @($expectedFields | Where-Object { $actualFields -notcontains $_ })
$extraFields = @($actualFields | Where-Object { $expectedFields -notcontains $_ })
$invalidFields = @()
$nestedFields = @()
$provinceValues = @(
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "四川", "贵州", "云南", "西藏", "陕西", "甘肃",
    "青海", "宁夏", "新疆", "内蒙古"
)
foreach ($property in $obj.PSObject.Properties) {
    if ($null -eq $property.Value -or -not ($property.Value -is [string]) -or $property.Value.Length -eq 0) {
        $invalidFields += $property.Name
    } elseif ($property.Value -is [System.Management.Automation.PSCustomObject] -or $property.Value -is [System.Object[]]) {
        $nestedFields += $property.Name
    }
}

$bad = $false
if ($missingFields.Count -gt 0) {
    Write-Warning ("缺少字段: " + ($missingFields -join ", "))
    $bad = $true
}
if ($extraFields.Count -gt 0) {
    Write-Warning ("包含不允许的字段: " + ($extraFields -join ", "))
    $bad = $true
}
if ($invalidFields.Count -gt 0) {
    Write-Warning ('所有字段必须是非空字符串；缺失值填"null": ' + ($invalidFields -join ", "))
    $bad = $true
}
if ($nestedFields.Count -gt 0) {
    Write-Warning ("字段不得嵌套: " + ($nestedFields -join ", "))
    $bad = $true
}
if ($obj.'所属省/市' -ne "null" -and $provinceValues -notcontains $obj.'所属省/市') {
    Write-Warning "所属省/市必须是省级行政区或直辖市简称，例如北京、河北、上海、新疆"
    $bad = $true
}
if ($bad) { exit 2 }

if (-not $WebhookUrl) {
    $WebhookUrl = $env:FEISHU_WEBHOOK_URL
    if (-not $WebhookUrl) {
        $WebhookUrl = $env:FEISHU_CREATE_WEBHOOK_URL
    }
    if (-not $WebhookUrl) {
        $skillRoot = Split-Path -Parent $PSScriptRoot
        $webhookConfigPath = Join-Path $skillRoot "config/webhook.json"
        if (Test-Path $webhookConfigPath) {
            try {
                $webhookConfig = Get-Content -Raw -Encoding UTF8 $webhookConfigPath | ConvertFrom-Json
                $WebhookUrl = [string]$webhookConfig.webhook_url
            } catch {
                Write-Error ("Webhook配置无效 -> " + $_.Exception.Message)
                exit 1
            }
        }
    }
}

if ($DryRun) {
    Write-Output "DRY-RUN（未发送）"
    Write-Output ("  URL配置: " + $(if ($WebhookUrl) { "是" } else { "否" }))
    Write-Output ("  字节数 : " + $bytes.Length)
    Write-Output ("  字段数 : " + @($obj.PSObject.Properties).Count)
    Write-Output $json
    Write-Output "校验通过：固定13字段、单条、平铺、全字符串、无JSON null"
    exit 0
}

if (-not $Live) {
    Write-Error "拒绝发送：生产推送必须显式传入-Live；调试请使用-DryRun"
    exit 2
}
if ($explicitWebhookUrl) {
    Write-Error "拒绝发送：生产模式不接受命令行-WebhookUrl，必须使用环境变量或config/webhook.json"
    exit 2
}
if (-not $ManifestPath) {
    Write-Error "拒绝发送：生产推送必须提供-ManifestPath"
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
if ($manifest.live_push_allowed -ne $true -or $manifest.mode -ne "daily-push" -or $manifest.state -ne "VALIDATED") {
    Write-Error ("拒绝发送：manifest必须为daily-push、live_push_allowed=true且state=VALIDATED")
    exit 2
}

$payloadRoot = [System.IO.Path]::GetFullPath([string]$manifest.payload_dir)
$requiredRoot = [System.IO.Path]::Combine($payloadRoot, "push")
$payloadFullPath = [System.IO.Path]::GetFullPath($PayloadPath)
if (-not $payloadFullPath.StartsWith($requiredRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "拒绝发送：PayloadPath必须位于manifest对应的pipeline/payloads/push目录"
    exit 2
}

$candidateId = [System.IO.Path]::GetFileNameWithoutExtension($payloadFullPath)
$sha256 = [System.BitConverter]::ToString(
    [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
).Replace("-", "").ToLowerInvariant()
$payloadMeta = @($manifest.payloads | Where-Object {
    $_.flow -eq "push" -and $_.candidate_id -eq $candidateId
})
if ($payloadMeta.Count -ne 1) {
    Write-Error "拒绝发送：manifest中没有唯一匹配的已验证载荷"
    exit 2
}
if (-not $payloadFullPath.Equals([System.IO.Path]::GetFullPath([string]$payloadMeta[0].path), [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "拒绝发送：载荷路径与manifest记录不一致"
    exit 2
}
if ($sha256 -ne [string]$payloadMeta[0].sha256) {
    Write-Error "拒绝发送：载荷在批次校验后被修改"
    exit 2
}
if (-not $WebhookUrl) {
    Write-Error "拒绝发送：未配置Webhook；请设置环境变量或config/webhook.json"
    exit 1
}

try {
    $response = Invoke-WebRequest -Uri $WebhookUrl -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body $bytes -TimeoutSec 30 -UseBasicParsing
    if ([int]$response.StatusCode -ne 200) {
        Write-Error "飞书HTTP状态不是200"
        exit 1
    }
    try {
        $responseObj = $response.Content | ConvertFrom-Json
        if ($null -eq $responseObj.code -or [int]$responseObj.code -ne 0) {
            Write-Error "飞书返回未确认成功（要求code: 0）"
            exit 1
        }
    } catch {
        Write-Error ("飞书返回不是可验证JSON -> " + $_.Exception.Message)
        exit 1
    }

    $receiptDir = [System.IO.Path]::Combine([string]$manifest.pipeline_dir, "receipts")
    [System.IO.Directory]::CreateDirectory($receiptDir) | Out-Null
    $receiptPath = [System.IO.Path]::Combine($receiptDir, ("push-" + $candidateId + ".json"))
    $receipt = [ordered]@{
        schema_version = 2
        flow = "push"
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
    Write-Output "飞书确认成功：HTTP 200 / code 0"
    Write-Output ("成功回执: " + $receiptPath)
} catch {
    Write-Error ("Request failed: " + $_.Exception.Message)
    exit 1
}
