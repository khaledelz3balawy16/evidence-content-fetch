$ErrorActionPreference = "Stop"
$logDir = "E:\desk\evidence content fetch\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = "$logDir\cycle_$stamp.txt"

"[$stamp] Starting modern reviews cycle..." | Tee-Object $logFile

cd "E:\desk\evidence content fetch\backend"
$env:APTEM_BROWSER_HEADLESS = "false"

python manage.py run_modern_reviews_cycle --workers 3 --apply 2>&1 | Tee-Object -Append $logFile

"[$(Get-Date -Format 'yyyyMMdd_HHmmss')] Done." | Tee-Object -Append $logFile
