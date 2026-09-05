param(
  [switch]$Remove
)

# Scope WSL2 LAN access to this TCP port, the Private profile, and LocalSubnet.
# Run from an elevated Windows PowerShell. Localhost access needs no rule.
$port = 8000
$ruleName = "WizardsBrush-8000"
$displayName = "The Wizard's Brush 8000 (Private LAN only)"
$hyperRuleName = "WizardsBrush-WSL-8000"
$wslCreatorId = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'

Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue |
  Remove-NetFirewallRule -ErrorAction SilentlyContinue

if (Get-Command Remove-NetFirewallHyperVRule -ErrorAction SilentlyContinue) {
  Remove-NetFirewallHyperVRule -Name $hyperRuleName -ErrorAction SilentlyContinue
}

if ($Remove) {
  Write-Host "Removed The Wizard's Brush WSL LAN firewall rules."
  exit 0
}

New-NetFirewallRule -Name $ruleName -DisplayName $displayName `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port `
  -Profile Private -RemoteAddress LocalSubnet | Out-Null

if (Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue) {
  New-NetFirewallHyperVRule -Name $hyperRuleName -DisplayName $displayName `
    -Direction Inbound -Action Allow -VMCreatorId $wslCreatorId `
    -Protocol TCP -LocalPorts $port -RemoteAddresses LocalSubnet | Out-Null
  Write-Host "Added scoped Windows and Hyper-V firewall rules."
} else {
  Write-Warning "Hyper-V firewall cmdlets are unavailable. No broad fallback was applied; consult the WSL networking guide if mirrored-mode clients cannot connect."
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" } |
       Select-Object -First 1).IPAddress
Write-Host "Use API_TOKEN and HTTPS before opening http://$ip`:$port to other devices."
Write-Host "Remove these rules later with: .\windows-lan-access.ps1 -Remove"
