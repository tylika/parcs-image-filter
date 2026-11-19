<#
.SYNOPSIS
  (Необов'язково) Репетиція на вашому комп'ютері: PARCS у Docker Desktop
  (майстер + 4 воркери в контейнерах), короткі заміри на малих зображеннях.
  У хмарі нічого не створюється і нічого не коштує.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\00-local-docker-test.ps1
#>
param([string]$Sizes = '256,512,1024', [int]$Runs = 1, [switch]$KeepRunning)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$SizeList = @($Sizes -split '[,; ]+' | Where-Object { $_ } | ForEach-Object { [int]$_ })
$py = Find-Python
$compose = Join-Path (Join-Path $ProjectRoot 'docker') 'docker-compose.yml'

Write-Step 'Запуск PARCS у Docker (майстер + 4 воркери)'
& docker compose -f $compose up -d
if ($LASTEXITCODE -ne 0) { throw 'docker compose не запустився. Чи запущено Docker Desktop?' }

$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 5
    $w = Get-ParcsWorkers 'localhost'
    $n = if ($null -eq $w) { 0 } else { $w.Count }
    Write-Host "    воркерів підключено: $n з 4"
} while ($n -lt 4 -and (Get-Date) -lt $deadline)

Write-Step 'Короткі заміри'
$out = Join-Path (Join-Path $ProjectRoot 'results') 'local-docker'
& $py.Exe @($py.Pre + @((Join-Path (Join-Path $ProjectRoot 'bench') 'run_benchmark.py'), '--master', 'http://localhost:8080',
                        '--runs', "$Runs", '--visual-size', '512', '--out', $out, '--sizes') + @($SizeList | ForEach-Object { "$_" }))

if (-not $KeepRunning) {
    Write-Step 'Зупинка контейнерів'
    & docker compose -f $compose down
}
Write-Host "Результати: $out"
