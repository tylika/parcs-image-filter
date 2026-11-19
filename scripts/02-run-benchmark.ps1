<#
.SYNOPSIS
  Запускає заміри на ВЖЕ створеному кластері PARCS (кластер не створює і не видаляє).

.DESCRIPTION
  Для кожного розміру зображення: послідовна версія і 1, 2, 3, 4 воркери, по 3 запуски.
  Результати: results\gcp\ (summary.csv, raw.csv, workers_detail.csv, images\)
  і архів results\gcp-results.zip - його надішліть для генерації звіту.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\02-run-benchmark.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\02-run-benchmark.ps1 -Photo C:\photos\cat.jpg
  powershell -ExecutionPolicy Bypass -File .\scripts\02-run-benchmark.ps1 -Resume
  powershell -ExecutionPolicy Bypass -File .\scripts\02-run-benchmark.ps1 -MasterIp 1.2.3.4
#>
param(
    [string]$Sizes = '1024,2048,4096',
    [int]$Runs = 3,
    [string]$Photo = '',
    [string]$MasterIp = '',
    [string]$Out = '',
    [switch]$Resume
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$SizeList = @($Sizes -split '[,; ]+' | Where-Object { $_ } | ForEach-Object { [int]$_ })

if (-not $Out) { $Out = Join-Path (Join-Path $ProjectRoot 'results') 'gcp' }
$py = Find-Python

# --- адреса майстра ------------------------------------------------------------
if (-not $MasterIp -and (Test-Path $ClusterFile)) {
    $MasterIp = (Get-Content $ClusterFile -Raw | ConvertFrom-Json).masterIp
}
if (-not $MasterIp) {
    $null = Assert-Gcloud
    $MasterIp = Get-MasterPublicIp
}
if (-not $MasterIp) { throw 'Не знайдено майстра. Спершу запустіть 01-create-cluster.ps1 (або передайте -MasterIp).' }

$w = Get-ParcsWorkers $MasterIp
if ($null -eq $w) { throw "PARCS-майстер http://$($MasterIp):8080 не відповідає. Перевірте 04-diagnose.ps1." }
Write-Step ("Майстер http://{0}:8080, воркерів підключено: {1}" -f $MasterIp, $w.Count)

# --- фото -> PGM (потрібен Pillow) -----------------------------------------------
$photoArgs = @()
if ($Photo) {
    if (-not (Test-Path $Photo)) { throw "Файл $Photo не знайдено" }
    $pgm = $Photo
    if ([System.IO.Path]::GetExtension($Photo).ToLower() -ne '.pgm') {
        $pgm = Join-Path (Join-Path $ProjectRoot 'data') 'photo.pgm'
        New-Item -ItemType Directory -Force -Path (Split-Path $pgm) | Out-Null
        Write-Step 'Конвертація фото у PGM (відтінки сірого, довша сторона до 2048)'
        & $py.Exe @($py.Pre + @((Join-Path (Join-Path $ProjectRoot 'tools') 'photo_to_pgm.py'), $Photo, $pgm, '--max-side', '2048'))
        if ($LASTEXITCODE -ne 0) { throw 'Конвертація не вдалася (встановіть Pillow:  py -m pip install pillow)' }
    }
    $photoArgs = @('--photo', $pgm)
}

# --- заміри ---------------------------------------------------------------------
Write-Step ("Заміри: розміри {0}; конфігурації: послідовно, 1-4 воркери; запусків: {1}" -f ($SizeList -join ', '), $Runs)
$benchArgs = @((Join-Path (Join-Path $ProjectRoot 'bench') 'run_benchmark.py'), '--master', "http://$($MasterIp):8080",
               '--runs', "$Runs", '--out', $Out, '--sizes') + @($SizeList | ForEach-Object { "$_" }) + $photoArgs
if ($Resume) { $benchArgs += '--resume' }
$started = Get-Date
& $py.Exe @($py.Pre + $benchArgs)
$code = $LASTEXITCODE
Write-Host ("    Тривалість: {0:N1} хв" -f ((Get-Date) - $started).TotalMinutes)
if ($code -ne 0) {
    Write-Warn2 'Заміри перервались з помилкою. Можна продовжити з того самого місця:  02-run-benchmark.ps1 -Resume'
    exit $code
}

# --- архів для звіту ----------------------------------------------------------------
if (Test-Path $ClusterFile) { Copy-Item $ClusterFile (Join-Path $Out 'cluster.json') -Force }
$zip = Join-Path (Split-Path $Out) ((Split-Path $Out -Leaf) + '-results.zip')
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $Out '*') -DestinationPath $zip
Write-Host ''
Write-Host "Готово. Результати: $Out" -ForegroundColor Green
Write-Host "Архів для звіту:    $zip"
Write-Host 'НЕ ЗАБУДЬТЕ видалити кластер:  powershell -ExecutionPolicy Bypass -File .\scripts\03-delete-cluster.ps1' -ForegroundColor Yellow
