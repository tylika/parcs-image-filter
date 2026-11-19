# Спільні функції для скриптів Google Cloud. Сумісно з Windows PowerShell 5.1 і PowerShell 7.
. (Join-Path $PSScriptRoot 'config.ps1')

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ClusterFile = Join-Path $PSScriptRoot '.cluster.json'
$MasterName  = "$Prefix-master"
$FirewallWeb = "$Prefix-allow-web-ssh"
$FirewallInt = "$Prefix-allow-internal"

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "    $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text){ Write-Host "    $Text" -ForegroundColor Yellow }

function Invoke-Gcloud {
    # Викликає gcloud (масив аргументів). Повертає Code / Out / Err; не кидає винятків.
    param([string[]]$A)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $res = & gcloud @A 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    $out = ($res | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"
    $err = ($res | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } |
            ForEach-Object { $_.ToString() }) -join "`n"
    return [pscustomobject]@{ Code = $code; Out = $out; Err = $err }
}

function Invoke-GcloudJson {
    param([string[]]$A, [switch]$AllowFail)
    $r = Invoke-Gcloud -A ($A + @("--project=$ProjectId", '--format=json', '--quiet'))
    if ($r.Code -ne 0) {
        if ($AllowFail) { return $null }
        throw ("gcloud {0}`n{1}" -f ($A -join ' '), $r.Err)
    }
    if ([string]::IsNullOrWhiteSpace($r.Out)) { return $null }
    return ($r.Out | ConvertFrom-Json)
}

function Assert-Gcloud {
    if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
        throw 'gcloud не знайдено. Встановіть:  winget install --id Google.CloudSDK -e  і відкрийте НОВЕ вікно PowerShell.'
    }
    $r = Invoke-Gcloud -A @('auth', 'list', '--filter=status:ACTIVE', '--format=value(account)')
    if ($r.Code -ne 0 -or [string]::IsNullOrWhiteSpace($r.Out)) {
        throw 'Ви не увійшли в gcloud. Виконайте:  gcloud init  (або  gcloud auth login)  і увійдіть buriak.yuliana@knu.ua'
    }
    $acc = $r.Out.Trim()
    $p = Invoke-Gcloud -A @('projects', 'describe', $ProjectId, '--format=value(projectId)')
    if ($p.Code -ne 0) {
        throw "Немає доступу до проєкту $ProjectId під акаунтом $acc. Перевірте, що ввійшли правильним акаунтом (gcloud auth list)."
    }
    Write-Ok "Акаунт: $acc, проєкт: $ProjectId"
    return $acc
}

function Get-MyInstances {
    # лише МОЇ ВМ (ім'я починається з yulparcs-)
    $x = Invoke-GcloudJson -A @('compute', 'instances', 'list', "--filter=name~^$Prefix-") -AllowFail
    # друга перевірка імені на нашому боці: чужі ресурси не повертаються ніколи
    return @(@($x) | Where-Object { $_ -and $_.name -like "$Prefix-*" })
}

function Get-MasterPublicIp {
    $list = Get-MyInstances | Where-Object { $_.name -eq $MasterName }
    foreach ($i in @($list)) {
        $ac = $i.networkInterfaces[0].accessConfigs
        if ($ac -and $ac[0].natIP) { return $ac[0].natIP }
    }
    return $null
}

function Get-ParcsWorkers([string]$MasterIp) {
    try {
        $r = Invoke-RestMethod -Uri "http://$($MasterIp):8080/api/worker" -TimeoutSec 10
        return @($r.workers)
    } catch { return $null }
}

function Find-Python {
    $cands = @(
        [pscustomobject]@{ Exe = 'py';      Pre = @('-3') },
        [pscustomobject]@{ Exe = 'python';  Pre = @() },
        [pscustomobject]@{ Exe = 'python3'; Pre = @() })
    foreach ($c in $cands) {
        if (Get-Command $c.Exe -ErrorAction SilentlyContinue) {
            $ver = (& $c.Exe @($c.Pre + @('--version')) 2>&1) | Out-String
            if ($ver -match 'Python 3') { return $c }
        }
    }
    throw 'Python 3 не знайдено (потрібна команда py або python).'
}

function Write-LfFile([string]$Path, [string]$Text) {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Text -replace "`r`n", "`n"), $enc)
}
