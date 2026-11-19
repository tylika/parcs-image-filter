<#
.SYNOPSIS
  Створює кластер PARCS у Google Cloud: 1 майстер + 4 воркери (n1-standard-1, Container-Optimized OS).

.DESCRIPTION
  * працює лише з ресурсами з префіксом yulparcs- (кластер Максима не зачіпається);
  * перед створенням перевіряє, що в проєкті вистачає квоти vCPU (ліміт trial - 8 vCPU на регіон);
    якщо кластер Максима ще працює - скаже про це і нічого не створюватиме;
  * брандмауер: 8080 і 22 лише з вашої IP-адреси, між ВМ кластера - увесь трафік;
  * контейнер PARCS запускається стартовим скриптом ВМ (docker run --network host).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\01-create-cluster.ps1
#>
param([int]$Workers = 0)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if ($Workers -gt 0) { $WorkersWanted = $Workers }
$VmCount = $WorkersWanted + 1

Write-Step 'Перевірка gcloud і доступу до проєкту'
$null = Assert-Gcloud

# --- мій кластер уже існує? ---------------------------------------------------------
$mine = Get-MyInstances
if ($mine.Count -gt 0) {
    Write-Warn2 ("Ваші ВМ уже існують ({0}): {1}" -f $mine.Count, (($mine | ForEach-Object { $_.name }) -join ', '))
    $ip = Get-MasterPublicIp
    if ($ip) {
        $w = Get-ParcsWorkers $ip
        Write-Ok ("Майстер: http://{0}:8080   воркерів підключено: {1}" -f $ip, $(if ($w) { $w.Count } else { '?' }))
    }
    Write-Host '    Щоб створити заново, спершу виконайте 03-delete-cluster.ps1'
    exit 0
}

# --- квота vCPU (спільна з Максимом) ----------------------------------------------------
Write-Step "Перевірка квоти vCPU у регіоні $Region"
$reg = Invoke-GcloudJson -A @('compute', 'regions', 'describe', $Region)
$cpus = @($reg.quotas) | Where-Object { $_.metric -eq 'CPUS' } | Select-Object -First 1
$free = [int]($cpus.limit - $cpus.usage)
Write-Host ("    ліміт {0}, зайнято {1}, вільно {2}, потрібно {3}" -f $cpus.limit, $cpus.usage, $free, $VmCount)
if ($free -lt $VmCount) {
    Write-Host ''
    Write-Host 'У проєкті зараз недостатньо вільних vCPU: найімовірніше, працює кластер Максима.' -ForegroundColor Red
    Write-Host 'Одночасно може працювати лише один кластер. Напишіть Максиму, дочекайтесь, поки він видалить свій, і запустіть скрипт знову.'
    exit 2
}

# --- мережа ------------------------------------------------------------------------------
$net = Invoke-GcloudJson -A @('compute', 'networks', 'describe', $Network) -AllowFail
if (-not $net) { throw "У проєкті немає мережі '$Network'. Напишіть мені - змінимо налаштування." }

$MyIp = $null
try { $MyIp = (Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 15).ToString().Trim() } catch { }
if ($MyIp -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    $MyIp = $null
    Write-Warn2 'Не вдалося визначити вашу IP-адресу - порти 8080 і 22 буде відкрито для всіх.'
}
$Source = if ($MyIp) { "$MyIp/32" } else { '0.0.0.0/0' }

Write-Step 'Правила брандмауера (лише yulparcs-*)'
$r = Invoke-Gcloud -A @('compute', 'firewall-rules', 'create', $FirewallWeb, "--project=$ProjectId", "--network=$Network",
                        '--direction=INGRESS', '--action=ALLOW', '--rules=tcp:8080,tcp:22', "--source-ranges=$Source",
                        "--target-tags=$Prefix", '--quiet')
if ($r.Code -ne 0 -and $r.Err -notmatch 'already exists') { throw "Не вдалося створити правило $FirewallWeb`n$($r.Err)" }
$r = Invoke-Gcloud -A @('compute', 'firewall-rules', 'create', $FirewallInt, "--project=$ProjectId", "--network=$Network",
                        '--direction=INGRESS', '--action=ALLOW', '--rules=all', "--source-tags=$Prefix",
                        "--target-tags=$Prefix", '--quiet')
if ($r.Code -ne 0 -and $r.Err -notmatch 'already exists') { throw "Не вдалося створити правило $FirewallInt`n$($r.Err)" }
Write-Ok "8080 і 22 відкрито для $Source; між ВМ кластера трафік дозволено"

# --- стартові скрипти ----------------------------------------------------------------------
$template = @'
#!/bin/bash
# Container-Optimized OS: власний брандмауер ВМ пропускає лише SSH.
# Відкриваємо вхідні з'єднання (хто може підключитись, і так обмежують правила брандмауера GCP).
iptables -I INPUT 1 -j ACCEPT
iptables -P INPUT ACCEPT
docker rm -f parcs >/dev/null 2>&1
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do docker pull __IMAGE__ && break; sleep 10; done
docker run -d --name parcs --restart=always --network host -e PARCS_ARGS="__ARGS__" __IMAGE__
'@
# тимчасові файли в папці проєкту: шлях має бути латиницею (gcloud.cmd погано передає кирилицю)
$tmp = Join-Path $PSScriptRoot '.init-tmp'
if ($tmp -match '[^\x00-\x7F]') {
    throw "Шлях до проєкту містить кирилицю ($tmp). Перемістіть папку, наприклад, у C:\parcs\parcs-image-filter і запустіть знову."
}
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$masterScript = Join-Path $tmp 'master.sh'
Write-LfFile $masterScript ($template.Replace('__IMAGE__', $ParcsImage).Replace('__ARGS__', 'master'))

$common = @("--project=$ProjectId", "--machine-type=$MachineType", '--image-family=cos-stable', '--image-project=cos-cloud',
            '--boot-disk-size=10GB', "--tags=$Prefix", "--labels=project=$Prefix", "--network=$Network", '--quiet')

# --- майстер (пробуємо зони по черзі) --------------------------------------------------------
Write-Step "Створення майстра $MasterName"
$Zone = $null
foreach ($z in $Zones) {
    Write-Host "    зона $z ..." -NoNewline
    $r = Invoke-Gcloud -A (@('compute', 'instances', 'create', $MasterName, "--zone=$z", "--metadata-from-file=startup-script=$masterScript") + $common)
    if ($r.Code -eq 0) { Write-Host ' OK' -ForegroundColor Green; $Zone = $z; break }
    Write-Host ' не вийшло'
    # повний текст помилки (без попереджень про розмір диска)
    $msg = ($r.Err -split "`n" | Where-Object { $_ -and $_ -notmatch 'disk size of under|developers.google.com/compute/docs/disks' }) -join "`n"
    Write-Host $msg -ForegroundColor Yellow
    if ($msg -match 'already exists') {
        Write-Warn2 "ВМ $MasterName уже існує - запустіть 03-delete-cluster.ps1 і спробуйте знову."
        break
    }
}
if (-not $Zone) { throw 'Не вдалося створити майстра в жодній із зон (див. повідомлення вище).' }

$desc = Invoke-GcloudJson -A @('compute', 'instances', 'describe', $MasterName, "--zone=$Zone")
$MasterInternal = $desc.networkInterfaces[0].networkIP
$MasterIp = $desc.networkInterfaces[0].accessConfigs[0].natIP
Write-Ok "Внутрішня адреса: $MasterInternal, зовнішня: $MasterIp"

# --- воркери ------------------------------------------------------------------------------------
$workerNames = @(1..$WorkersWanted | ForEach-Object { "$Prefix-worker$_" })
Write-Step ("Створення воркерів: {0}" -f ($workerNames -join ', '))
$workerScript = Join-Path $tmp 'worker.sh'
Write-LfFile $workerScript ($template.Replace('__IMAGE__', $ParcsImage).Replace('__ARGS__', "worker $MasterInternal"))
$r = Invoke-Gcloud -A (@('compute', 'instances', 'create') + $workerNames + @("--zone=$Zone", "--metadata-from-file=startup-script=$workerScript") + $common)
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
if ($r.Code -ne 0) {
    Write-Host $r.Err -ForegroundColor Yellow
    throw 'Не вдалося створити воркерів. Видаліть створене: 03-delete-cluster.ps1, потім спробуйте знову.'
}
Write-Ok 'Воркерів створено'

$info = [pscustomobject]@{
    project = $ProjectId; zone = $Zone; masterIp = $MasterIp; masterInternalIp = $MasterInternal
    machineType = $MachineType; vmCount = $VmCount; workers = $WorkersWanted
    createdUtc = (Get-Date).ToUniversalTime().ToString('s')
}
$info | ConvertTo-Json | Set-Content -Path $ClusterFile -Encoding UTF8

# --- очікування PARCS ----------------------------------------------------------------------------
Write-Step 'Очікування запуску PARCS (завантаження образу Docker, 2-5 хв)'
$deadline = (Get-Date).AddMinutes(15)
$count = -1
while ((Get-Date) -lt $deadline) {
    $w = Get-ParcsWorkers $MasterIp
    $count = if ($null -eq $w) { -1 } else { $w.Count }
    if ($count -ge $WorkersWanted) { break }
    $state = if ($count -lt 0) { 'майстер ще не відповідає' } else { "воркерів підключено: $count з $WorkersWanted" }
    Write-Host ("    {0:HH:mm:ss}  {1}" -f (Get-Date), $state)
    Start-Sleep -Seconds 15
}

Write-Host ''
if ($count -ge $WorkersWanted) {
    Write-Host 'КЛАСТЕР ГОТОВИЙ' -ForegroundColor Green
} else {
    Write-Host "Кластер створено, але підключилось воркерів: $([Math]::Max($count,0)) з $WorkersWanted." -ForegroundColor Yellow
    Write-Host '  Зачекайте 2-3 хв і відкрийте сторінку Workers, або запустіть .\scripts\04-diagnose.ps1'
}
Write-Host ("  Веб-інтерфейс PARCS:  http://{0}:8080  (сторінка Workers - список воркерів)" -f $MasterIp)
Write-Host  "  Зона: $Zone, ВМ: $VmCount x $MachineType"
Write-Host  '  Далі:   powershell -ExecutionPolicy Bypass -File .\scripts\02-run-benchmark.ps1'
Write-Host  '  Потім ОБОВ''ЯЗКОВО:  powershell -ExecutionPolicy Bypass -File .\scripts\03-delete-cluster.ps1  і напишіть Максиму' -ForegroundColor Yellow
