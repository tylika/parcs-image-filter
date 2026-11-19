<#
.SYNOPSIS
  Діагностика: стан ВМ, воркери в PARCS, вивід запуску (serial port) без SSH.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\04-diagnose.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\04-diagnose.ps1 -Vm yulparcs-worker2
#>
param([string]$Vm = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$null = Assert-Gcloud
if (-not $Vm) { $Vm = $MasterName }

$mine = Get-MyInstances
Write-Step ("Мої ВМ: {0}" -f $mine.Count)
$mine | ForEach-Object { Write-Host ("    {0,-18} {1,-10} {2}" -f $_.name, $_.status, ($_.zone -split '/')[-1]) }

$ip = Get-MasterPublicIp
if ($ip) {
    $w = Get-ParcsWorkers $ip
    Write-Step ("Майстер http://{0}:8080 - воркерів: {1}" -f $ip, $(if ($null -eq $w) { 'не відповідає' } else { $w.Count }))
    if ($w) { $w | ForEach-Object { Write-Host ("    #{0} {1}:{2}" -f $_.id, $_.ip, $_.port) } }
}

$vmObj = $mine | Where-Object { $_.name -eq $Vm } | Select-Object -First 1
if ($vmObj) {
    $zone = ($vmObj.zone -split '/')[-1]
    Write-Step "Останній вивід ВМ $Vm (serial port)"
    $r = Invoke-Gcloud -A @('compute', 'instances', 'get-serial-port-output', $Vm, "--zone=$zone", "--project=$ProjectId")
    ($r.Out -split "`n" | Select-Object -Last 40) -join "`n" | Write-Host
}
