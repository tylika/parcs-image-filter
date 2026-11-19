<#
.SYNOPSIS
  Видаляє МІЙ кластер (ВМ, диски, правила брандмауера з префіксом yulparcs-) і перевіряє,
  що нічого не лишилось. Ресурси Максима не чіпає.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\03-delete-cluster.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\03-delete-cluster.ps1 -Force
#>
param([switch]$Force)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

Write-Step 'Перевірка gcloud і доступу до проєкту'
$null = Assert-Gcloud

function Get-Leftovers {
    $left = @()
    foreach ($i in (Get-MyInstances)) { $left += [pscustomobject]@{ Kind = 'ВМ'; Name = $i.name; Zone = ($i.zone -split '/')[-1]; Type = 'instance' } }
    $d = Invoke-GcloudJson -A @('compute', 'disks', 'list', "--filter=name~^$Prefix-") -AllowFail
    foreach ($x in @($d)) { if ($x) { $left += [pscustomobject]@{ Kind = 'Диск'; Name = $x.name; Zone = ($x.zone -split '/')[-1]; Type = 'disk' } } }
    $f = Invoke-GcloudJson -A @('compute', 'firewall-rules', 'list', "--filter=name~^$Prefix-") -AllowFail
    foreach ($x in @($f)) { if ($x) { $left += [pscustomobject]@{ Kind = 'Правило брандмауера'; Name = $x.name; Zone = ''; Type = 'firewall' } } }
    # друга перевірка імені: видаляємо лише yulparcs-*
    return @($left | Where-Object { $_.Name -like "$Prefix-*" })
}

$left = @(Get-Leftovers)
if ($left.Count -eq 0) {
    Write-Ok "Ресурсів $Prefix-* не знайдено - видаляти нічого."
} else {
    Write-Step "Буде видалено (лише $Prefix-*):"
    $left | ForEach-Object { Write-Host ("    {0,-22} {1}" -f $_.Kind, $_.Name) }
    if (-not $Force) {
        $ans = Read-Host 'Видалити? (y/n)'
        if ($ans -notmatch '^(y|yes|т|так)$') { Write-Host 'Скасовано.'; exit 1 }
    }
    Write-Step 'Видалення (1-3 хв)'
    foreach ($z in @($left | Where-Object { $_.Type -eq 'instance' } | Group-Object Zone)) {
        $names = @($z.Group | ForEach-Object { $_.Name })
        $r = Invoke-Gcloud -A (@('compute', 'instances', 'delete') + $names + @("--zone=$($z.Name)", "--project=$ProjectId", '--delete-disks=all', '--quiet'))
        if ($r.Code -ne 0) { Write-Warn2 "delete instances: $($r.Err)" }
    }
    foreach ($x in @($left | Where-Object { $_.Type -eq 'disk' })) {
        $null = Invoke-Gcloud -A @('compute', 'disks', 'delete', $x.Name, "--zone=$($x.Zone)", "--project=$ProjectId", '--quiet')
    }
    $fw = @($left | Where-Object { $_.Type -eq 'firewall' } | ForEach-Object { $_.Name })
    if ($fw.Count -gt 0) {
        $null = Invoke-Gcloud -A (@('compute', 'firewall-rules', 'delete') + $fw + @("--project=$ProjectId", '--quiet'))
    }
}

Write-Step 'Перевірка залишків'
$left = @()
for ($attempt = 1; $attempt -le 3; $attempt++) {
    $left = @(Get-Leftovers)
    if ($left.Count -eq 0) { break }
    Write-Warn2 "Ще лишилось: $($left.Count). Чекаю і повторюю (спроба $attempt)"
    Start-Sleep -Seconds 15
    foreach ($x in ($left | Where-Object { $_.Type -eq 'instance' })) {
        $null = Invoke-Gcloud -A @('compute', 'instances', 'delete', $x.Name, "--zone=$($x.Zone)", "--project=$ProjectId", '--delete-disks=all', '--quiet')
    }
    foreach ($x in ($left | Where-Object { $_.Type -eq 'disk' })) {
        $null = Invoke-Gcloud -A @('compute', 'disks', 'delete', $x.Name, "--zone=$($x.Zone)", "--project=$ProjectId", '--quiet')
    }
    foreach ($x in ($left | Where-Object { $_.Type -eq 'firewall' })) {
        $null = Invoke-Gcloud -A @('compute', 'firewall-rules', 'delete', $x.Name, "--project=$ProjectId", '--quiet')
    }
}
$left = @(Get-Leftovers)

Write-Host ''
if ($left.Count -eq 0) {
    if (Test-Path $ClusterFile) { Remove-Item $ClusterFile -Force }
    Write-Host 'OK: КЛАСТЕР ВИДАЛЕНО. ВМ, дисків і правил yulparcs-* не лишилось.' -ForegroundColor Green
    Write-Host '    Тепер напишіть Максиму, що проєкт вільний.'
    exit 0
} else {
    Write-Host 'УВАГА: щось лишилось:' -ForegroundColor Red
    $left | ForEach-Object { Write-Host ("    {0}: {1}" -f $_.Kind, $_.Name) }
    Write-Host '    Запустіть скрипт ще раз або видаліть вручну в Google Cloud Console (Compute Engine).'
    exit 1
}
