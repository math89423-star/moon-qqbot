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
$url = $asset.browser_download_url
$mirror = "https://ghproxy.com/$url"
$outZip = Join-Path $OutDir 'NapCatQQ.zip'

Write-Host "Download: $name ($sizeMB MB)"
Write-Host "Mirror: $mirror"

try {
    Invoke-WebRequest -Uri $mirror -OutFile $outZip
    Write-Host "Mirror OK"
} catch {
    Write-Host "Mirror failed, trying direct: $url"
    Invoke-WebRequest -Uri $url -OutFile $outZip
}

Write-Host "Extracting..."
Expand-Archive -Path $outZip -DestinationPath $OutDir -Force
Remove-Item $outZip -Force

Write-Host "NapCatQQ ready: $OutDir"
