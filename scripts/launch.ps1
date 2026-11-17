param([ValidateSet('Desktop', 'Phone')][string]$Mode = 'Desktop')
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir 'venv\Scripts\python.exe'
$configPath = Join-Path $projectDir 'config.json'
if (!(Test-Path -LiteralPath $pythonExe)) { throw 'Missing venv. Install the project dependencies first.' }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$port = if ($config.port) { [int]$config.port } else { 8527 }
$desktopUrl = "http://127.0.0.1:$port"

function Get-CopilotHealth {
    try {
        $health = Invoke-RestMethod -Uri "$desktopUrl/api/health" -TimeoutSec 2
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
if (!$health) {
    if ($config.stt.url -and $env:INTERVIEW_COPILOT_STT_DIR) {
        $sttUri = [Uri]$config.stt.url
        if ($sttUri.IsLoopback) {
            $sttReady = $false
            try { $null = Invoke-RestMethod -Uri ($config.stt.url.TrimEnd('/') + '/models') -TimeoutSec 2; $sttReady = $true } catch {}
            if (!$sttReady) {
                $sttPython = Join-Path $env:INTERVIEW_COPILOT_STT_DIR 'venv\Scripts\python.exe'
                Start-Process -FilePath $sttPython -ArgumentList 'stt_server.py' -WorkingDirectory $env:INTERVIEW_COPILOT_STT_DIR -WindowStyle Hidden
                $sttDeadline = (Get-Date).AddSeconds(30)
                do {
                    try { $null = Invoke-RestMethod -Uri ($config.stt.url.TrimEnd('/') + '/models') -TimeoutSec 2; $sttReady = $true } catch { Start-Sleep -Milliseconds 500 }
                } until ($sttReady -or (Get-Date) -gt $sttDeadline)
                if (!$sttReady) { Write-Warning 'STT did not become ready. Check the configured STT service.' }
            }
        }
    }
    $logDir = Join-Path $projectDir 'data\logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
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
    Start-Process $desktopUrl
    Write-Host "Desktop: $desktopUrl"
} else {
    $mobile = Invoke-RestMethod -Uri "$desktopUrl/api/mobile" -TimeoutSec 3
    if (!$mobile.enabled) { throw 'The running service has no phone listener. Stop that service and launch Phone mode again.' }
    $captureState = Invoke-RestMethod -Uri "$desktopUrl/api/state" -TimeoutSec 5
    foreach ($source in @('loopback', 'mic')) {
        if (($source -eq 'loopback' -and $captureState.listening) -or ($source -eq 'mic' -and $captureState.self_listening)) { continue }
        $deviceKey = if ($source -eq 'mic') { 'mic_device_index' } else { 'loopback_device_index' }
        $captureBody = @{ source = $source }
        if ($null -ne $config.audio.$deviceKey) { $captureBody.device_index = [int]$config.audio.$deviceKey }
        $result = Invoke-RestMethod -Uri "$desktopUrl/api/start" -Method Post -ContentType 'application/json' -Body ($captureBody | ConvertTo-Json) -TimeoutSec 15
        if (!$result.ok) { Write-Warning "$source capture failed. Check the device in the desktop workspace." }
    }
    $pairing = Invoke-RestMethod -Uri "$desktopUrl/api/mobile/pairing" -Method Post -TimeoutSec 3
    Write-Host 'Phone addresses:'
    if (!$pairing.urls.Count) { Write-Warning 'No LAN address found. Check the network or configure mobile.host.' }
    $pairing.urls | ForEach-Object { Write-Host "  $_" }
    Write-Host "Pairing code: $($pairing.code) (single use, expires in $($pairing.expires_in) seconds)"
    Write-Host "Renew code or revoke access: $desktopUrl"
    Write-Host 'PC capture continues when this launcher closes.'
}
