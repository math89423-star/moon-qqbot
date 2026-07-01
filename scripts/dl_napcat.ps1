param(
    [string]$OutDir
)

if (-not $OutDir) {
    Write-Host "Usage: dl_napcat.ps1 -OutDir <target_dir>"
    exit 1
}

if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

Write-Host "Querying latest NapCatQQ release..."
$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest' -Headers @{'User-Agent'='moon-qqbot'}
$asset = $release.assets | Where-Object { $_.name -like 'NapCat.Shell.Windows.Node*' -and $_.name -like '*.zip' } | Select-Object -First 1

if (-not $asset) {
    Write-Host "ERROR: NapCatQQ Windows package not found"
    exit 1
}

$name = $asset.name
$sizeMB = [math]::Round($asset.size / 1MB, 1)
$expectedSize = $asset.size
$url = $asset.browser_download_url
$outZip = Join-Path $OutDir 'NapCatQQ.zip'

Write-Host "Download: $name ($sizeMB MB)"

# 下载函数：带完整性验证
function Download-File($uri, $description) {
    Write-Host "Trying $description..."
    try {
        Invoke-WebRequest -Uri $uri -OutFile $outZip -ErrorAction Stop
        $actual = (Get-Item $outZip).Length
        if ($actual -ge $expectedSize * 0.9) {
            Write-Host "OK ($([math]::Round($actual/1MB,1)) MB)"
            return $true
        }
        Write-Host "WARNING: Size mismatch ($actual vs $expectedSize bytes), likely proxy error page"
        return $false
    } catch {
        Write-Host "FAILED: $_"
        return $false
    }
}

# 直连优先（GitHub CDN 国内通常可达），镜像兜底
$success = $false
if (-not $success) { $success = Download-File $url 'direct' }
if (-not $success) { $success = Download-File "https://ghproxy.com/$url" 'ghproxy mirror' }
if (-not $success) { $success = Download-File "https://gh-proxy.com/$url" 'gh-proxy mirror' }

if (-not $success) {
    Write-Host "ERROR: All download methods failed"
    Write-Host "Manual download: $url"
    Write-Host "Extract to: $OutDir"
    exit 1
}

Write-Host "Extracting..."
try {
    Expand-Archive -Path $outZip -DestinationPath $OutDir -Force -ErrorAction Stop
    Write-Host "Extract OK (Expand-Archive)"
} catch {
    Write-Host "Expand-Archive failed, trying tar..."
    & tar -xf $outZip -C $OutDir 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Both Expand-Archive and tar failed"
        Write-Host "Manual extract: $outZip -> $OutDir"
        exit 1
    }
    Write-Host "Extract OK (tar)"
}

Remove-Item $outZip -Force
Write-Host "NapCatQQ ready: $OutDir"
