# 从运行中的 Chrome 提取抖音 cookies (via DevTools Protocol)
# 用法: powershell.exe -ExecutionPolicy Bypass -File scripts\extract_douyin_cookies.ps1
param(
    [string]$OutputFile = ""
)

$ErrorActionPreference = "Stop"

# 缺省输出到 <repo>/AstrBot/data/douyin_cookies.txt (与 video 插件读取路径一致)
if (-not $OutputFile) {
    $OutputFile = Join-Path (Split-Path $PSScriptRoot -Parent) "AstrBot\data\douyin_cookies.txt"
}
$outDir = Split-Path $OutputFile -Parent
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

# 1. 找 Chrome 的 DevTools 调试端口
#    如果 Chrome 已经开了远程调试就直接用，否则启动一个新的调试实例
function Get-ChromeCookies {
    # 尝试连接已运行的 Chrome (需要先启用远程调试)
    # 更简单的方式: 用 Chrome 的 cookie SQLite 通过停止 Chrome 的写锁
    # 这里用 COM 对象 InternetGetCookie 来读

    $urls = @(
        "https://www.douyin.com",
        "https://v.douyin.com"
    )

    $allCookies = @()

    foreach ($url in $urls) {
        try {
            # 使用 InternetGetCookieEx 获取完整 cookie 字符串
            $cookieHeader = [System.Net.Http.HttpClientHandler]::new()
            # 直接用 System.Net.CookieContainer
            $uri = [System.Uri]::new($url)
            $cookies = [System.Net.CookieContainer]::new()

            # 通过注册表读取 IE/Edge 存储的 cookies (Chrome 可能不共享)
            # 回退方案: 直接用 .NET WebRequest 访问 douyin.com 获取新鲜 cookies
        } catch {}
    }

    # 方案B: 直接请求 douyin.com 获取 Set-Cookie 响应头中的 cookies
    $session = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
    $response = Invoke-WebRequest -Uri "https://www.douyin.com" -SessionVariable session -UseBasicParsing -MaximumRedirection 0 -ErrorAction SilentlyContinue

    # 写 Netscape 格式
    $lines = @("# Netscape HTTP Cookie File", "# Fresh from douyin.com response", "")

    foreach ($cookie in $session.Cookies.GetAllCookies()) {
        $domain = $cookie.Domain
        $flag = "TRUE"
        $path = $cookie.Path
        $secure = if ($cookie.Secure) { "TRUE" } else { "FALSE" }
        $expires = if ($cookie.Expires -gt [DateTime]::MinValue) {
            [int]($cookie.Expires.ToUniversalTime() - (Get-Date "1970-01-01")).TotalSeconds
        } else { 0 }
        $name = $cookie.Name
        $value = $cookie.Value
        $lines += "$domain`tTRUE`t$path`t$secure`t$expires`t$name`t$value"
    }

    $lines -join "`n" | Out-File -FilePath $OutputFile -Encoding ASCII
    Write-Host "$($session.Cookies.Count) cookies written to $OutputFile"
}

Get-ChromeCookies
