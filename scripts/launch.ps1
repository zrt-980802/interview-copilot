param([ValidateSet('Desktop', 'Phone')][string]$Mode = 'Desktop', [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir 'venv\Scripts\python.exe'
$configPath = Join-Path $projectDir 'config.json'
if (!(Test-Path -LiteralPath $pythonExe)) { throw 'Missing venv. Install the project dependencies first.' }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$port = if ($config.port) { [int]$config.port } else { 8527 }
$desktopUrl = "http://127.0.0.1:$port"
$logDir = Join-Path $projectDir 'data\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

function Invoke-LocalJson {
    param([Uri]$Uri, [string]$Method = 'GET', [string]$Body, [int]$TimeoutSec = 5)
    if (!$Uri.IsLoopback) { throw 'This launcher only manages loopback HTTP services.' }
    $request = [System.Net.HttpWebRequest]::Create($Uri)
    $request.Proxy = $null
    $request.Method = $Method.ToUpperInvariant()
    $request.Timeout = $TimeoutSec * 1000
    $request.ReadWriteTimeout = $TimeoutSec * 1000
    if ($Body) {
        $request.ContentType = 'application/json'
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
        $request.ContentLength = $bytes.Length
        $stream = $request.GetRequestStream()
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    }
    $response = $request.GetResponse()
    $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
    try { return $reader.ReadToEnd() | ConvertFrom-Json }
    finally { $reader.Dispose(); $response.Close() }
}

function Get-CopilotHealth {
    try {
        $health = Invoke-LocalJson -Uri "$desktopUrl/api/health" -TimeoutSec 2
    } catch [System.Net.WebException] {
        if (!$_.Exception.Response) { return $null }
        $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        try { $health = $reader.ReadToEnd() | ConvertFrom-Json }
        catch { throw 'Port is occupied by an unrecognized service.' }
        finally { $reader.Dispose() }
    }
    if ($health.service -ne 'interview-copilot') { throw 'Port is occupied by another service.' }
    return $health
}

$health = Get-CopilotHealth
$sttDir = if ($env:INTERVIEW_COPILOT_STT_DIR) { $env:INTERVIEW_COPILOT_STT_DIR } else { $config.stt.server_dir }
if ($config.stt.url) {
    $sttUri = [Uri]$config.stt.url
    if ($sttUri.IsLoopback) {
        $sttReady = $false
        try { $null = Invoke-LocalJson -Uri ($config.stt.url.TrimEnd('/') + '/models') -TimeoutSec 2; $sttReady = $true } catch {}
        if (!$sttReady -and $sttDir) {
            $sttPython = Join-Path $sttDir 'venv\Scripts\python.exe'
            if (!(Test-Path -LiteralPath $sttPython) -or !(Test-Path -LiteralPath (Join-Path $sttDir 'stt_server.py'))) {
                throw 'The configured STT directory must contain venv\Scripts\python.exe and stt_server.py.'
            }
            $sttService = Start-Process -FilePath $sttPython -ArgumentList @('stt_server.py', '--port', [string]$sttUri.Port) -WorkingDirectory $sttDir -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $logDir "$stamp-stt-service.log") -RedirectStandardError (Join-Path $logDir "$stamp-stt-error.log")
            $sttDeadline = (Get-Date).AddSeconds(30)
            do {
                $sttService.Refresh()
                if ($sttService.HasExited) { throw "STT exited. Check data\logs\$stamp-stt-error.log" }
                try { $null = Invoke-LocalJson -Uri ($config.stt.url.TrimEnd('/') + '/models') -TimeoutSec 2; $sttReady = $true } catch { Start-Sleep -Milliseconds 500 }
            } until ($sttReady -or (Get-Date) -gt $sttDeadline)
        }
        if (!$sttReady) {
            if ($Mode -eq 'Phone') { throw 'STT is unavailable. Start it or configure stt.server_dir before Phone mode.' }
            Write-Warning 'STT is unavailable. Manual questions still work; start STT or configure stt.server_dir before recording.'
        }
    }
}
if (!$health) {
    $serverArgs = @('server.py')
    if ($Mode -eq 'Phone') { $serverArgs += @('--mobile', '--auto-listen') }
    $service = Start-Process -FilePath $pythonExe -ArgumentList $serverArgs -WorkingDirectory $projectDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logDir "$stamp-service.log") -RedirectStandardError (Join-Path $logDir "$stamp-error.log")
    $deadline = (Get-Date).AddSeconds(45)
    do {
        if ($service.HasExited) { throw "Copilot exited. Check data\logs\$stamp-error.log" }
        $health = Get-CopilotHealth
        if (!$health -or !$health.ready) { Start-Sleep -Milliseconds 500 }
    } until (($health -and $health.ready) -or (Get-Date) -gt $deadline)
    if (!$health -or !$health.ready) { throw "Copilot is not ready. Check data\logs\$stamp-error.log" }
}
if (!$health.ready) { throw 'The existing service is not ready. Check its health diagnostics.' }
if ($Mode -eq 'Desktop') {
    if (!$NoBrowser) { Start-Process $desktopUrl }
    Write-Host "Desktop: $desktopUrl"
} else {
    $mobile = Invoke-LocalJson -Uri "$desktopUrl/api/mobile" -TimeoutSec 3
    if (!$mobile.enabled) { throw 'The running service has no phone listener. Stop that service and launch Phone mode again.' }
    $captureState = Invoke-LocalJson -Uri "$desktopUrl/api/state" -TimeoutSec 5
    foreach ($source in @('loopback', 'mic')) {
        if (($source -eq 'loopback' -and $captureState.listening) -or ($source -eq 'mic' -and $captureState.self_listening)) { continue }
        $deviceKey = if ($source -eq 'mic') { 'mic_device_index' } else { 'loopback_device_index' }
        $captureBody = @{ source = $source }
        if ($null -ne $config.audio.$deviceKey) { $captureBody.device_index = [int]$config.audio.$deviceKey }
        $result = Invoke-LocalJson -Uri "$desktopUrl/api/start" -Method Post -Body ($captureBody | ConvertTo-Json) -TimeoutSec 15
        if (!$result.ok) { Write-Warning "$source capture failed. Check the device in the desktop workspace." }
    }
    $pairing = Invoke-LocalJson -Uri "$desktopUrl/api/mobile/pairing" -Method Post -TimeoutSec 3
    Write-Host 'Phone addresses:'
    if (!$pairing.urls.Count) { Write-Warning 'No LAN address found. Check the network or configure mobile.host.' }
    $pairing.urls | ForEach-Object { Write-Host "  $_" }
    Write-Host "Pairing code: $($pairing.code) (single use, expires in $($pairing.expires_in) seconds)"
    Write-Host "Renew code or revoke access: $desktopUrl"
    Write-Host 'PC capture continues when this launcher closes.'
}
