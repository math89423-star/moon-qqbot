param(
    [string]$OutDir
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "Continue"

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

# Try direct, then mirrors. Verify size after each attempt.
$methods = @(
    @{Label="direct"; Uri=$url},
    @{Label="ghproxy mirror"; Uri="https://ghproxy.com/$url"},
    @{Label="gh-proxy mirror"; Uri="https://gh-proxy.com/$url"}
)

$ok = $false
foreach ($m in $methods) {
    Write-Host "Trying $($m.Label)..."
    try {
        # curl.exe has built-in progress bar (Win10+), fallback to Invoke-WebRequest
        $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
        if ($curl) {
            & curl.exe -L -o $outZip $m.Uri --progress-bar
            if ($LASTEXITCODE -ne 0) { throw "curl exited with $LASTEXITCODE" }
        } else {
            Write-Host "  (no progress bar, downloading 109MB, please wait...)"
            Invoke-WebRequest -Uri $m.Uri -OutFile $outZip -ErrorAction Stop
        }
        $actual = (Get-Item $outZip).Length
        if ($actual -ge $expectedSize * 0.9) {
            Write-Host "OK ($([math]::Round($actual/1MB,1)) MB)"
            $ok = $true
            break
        }
        Write-Host "WARNING: Size mismatch ($actual vs $expectedSize bytes)"
    } catch {
        Write-Host "FAILED: $_"
    }
}

if (-not $ok) {
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
