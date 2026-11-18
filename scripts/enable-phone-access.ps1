#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory = $true)][string]$LocalAddress,
    [ValidateRange(1, 65535)][int]$Port = 8528,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$resultPath = Join-Path $projectDir 'data\phone-access-result.json'
try {
    $address = Get-NetIPAddress -IPAddress $LocalAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object { $_.AddressState -eq 'Preferred' } | Select-Object -First 1
    if (!$address) { throw 'The local IPv4 address is not active.' }
    $adapter = Get-NetAdapter | Where-Object { $_.ifIndex -eq $address.InterfaceIndex } | Select-Object -First 1
    if ($adapter.Status -ne 'Up' -or !$adapter.HardwareInterface) { throw 'Choose an active physical network adapter.' }
    $ruleName = "InterviewCopilot-Phone-$Port-$($address.InterfaceIndex)"
    $existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($Remove) {
        if ($existing) { $existing | Remove-NetFirewallRule }
        $result = @{ ok = $true; removed = $true; rule = $ruleName }
    } else {
        $config = Get-Content -LiteralPath (Join-Path $projectDir 'config.json') -Raw | ConvertFrom-Json
        $desktopPort = if ($config.port) { [int]$config.port } else { 8527 }
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$desktopPort/api/health" -TimeoutSec 5
        $mobile = Invoke-RestMethod -Uri "http://127.0.0.1:$desktopPort/api/mobile" -TimeoutSec 5
        if ($health.service -ne 'interview-copilot' -or !$health.ready -or $health.preview -or
            !$mobile.enabled -or $mobile.local_only -or $mobile.port -ne $Port) {
            throw 'Start the real Copilot service with the LAN phone listener first.'
        }
        $listener = Get-NetTCPConnection -State Listen -LocalPort $Port |
            Where-Object { $_.LocalAddress -eq '0.0.0.0' -or $_.LocalAddress -eq $LocalAddress } | Select-Object -First 1
        $desktopListener = Get-NetTCPConnection -State Listen -LocalPort $desktopPort -LocalAddress '127.0.0.1'
        if (!$listener -or $listener.OwningProcess -notin @($desktopListener.OwningProcess)) {
            throw 'The phone port does not belong to the local Copilot service.'
        }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        if (!$process.ExecutablePath -or !(Test-Path -LiteralPath $process.ExecutablePath)) { throw 'Cannot resolve the listener program.' }
        $bytes = [System.Net.IPAddress]::Parse($LocalAddress).GetAddressBytes()
        $prefix = [int]$address.PrefixLength
        for ($index = 0; $index -lt 4; $index++) {
            $bits = [Math]::Max(0, [Math]::Min(8, $prefix - 8 * $index))
            $mask = if ($bits -eq 0) { 0 } else { 256 - [Math]::Pow(2, 8 - $bits) }
            $bytes[$index] = $bytes[$index] -band [int]$mask
        }
        $subnet = ($bytes -join '.') + "/$prefix"
        $profile = (Get-NetConnectionProfile -InterfaceIndex $address.InterfaceIndex).NetworkCategory.ToString()
        if ($profile -eq 'DomainAuthenticated') { $profile = 'Domain' }
        $ruleParameters = @{ Enabled = 'True'; Direction = 'Inbound'; Action = 'Allow'; Protocol = 'TCP';
            LocalPort = $Port; LocalAddress = $LocalAddress; RemoteAddress = $subnet; Profile = $profile;
            InterfaceAlias = $adapter.Name; Program = $process.ExecutablePath }
        if ($existing) {
            Set-NetFirewallRule -Name $ruleName @ruleParameters | Out-Null
        } else {
            New-NetFirewallRule -Name $ruleName -DisplayName "Interview Copilot Phone ($Port)" @ruleParameters | Out-Null
        }
        $result = @{ ok = $true; rule = $ruleName; local_address = $LocalAddress; port = $Port;
            remote_subnet = $subnet; profile = $profile; program = $process.ExecutablePath }
    }
    $result | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    $result | ConvertTo-Json
} catch {
    @{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    throw
}
