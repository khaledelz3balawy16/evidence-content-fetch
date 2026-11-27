param([string]$RunDirectory)
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $RunDirectory) {
    $RunDirectory = (Get-ChildItem -LiteralPath (Join-Path $projectRoot 'debug\modern-reviews') -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'run-status.json') } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
if (-not $RunDirectory) { throw 'No Modern review run found.' }
Write-Output "Monitoring: $RunDirectory"
while ($true) {
    $applyFile = Join-Path $RunDirectory 'apply-progress.json'
    $captureFile = Join-Path $RunDirectory 'capture\manifest.json'
    $statusFile = Join-Path $RunDirectory 'run-status.json'
    try {
        if (Test-Path -LiteralPath $applyFile) {
            $status = Get-Content -LiteralPath $applyFile -Raw | ConvertFrom-Json
            Write-Output ("{0} Apply {1}: learners {2}/{3}; updated {4}; inserted {5}; historical {6}" -f
                $status.updated_at, $status.state, $status.committed_learners.Count, $status.learners_total,
                $status.committed_updates, $status.committed_inserts, $status.committed_historical)
            if ($status.state -in @('complete','failed')) { break }
        } elseif (Test-Path -LiteralPath $captureFile) {
            $status = Get-Content -LiteralPath $captureFile -Raw | ConvertFrom-Json
            $completed = $status.completed
            if ($status.queued -lt $status.candidate_count) {
                $completed += $status.candidate_count - $status.queued
            }
            Write-Output ("{0} Capture {1}: {2}/{3}; PID {4}; workers {5}" -f
                $status.updated_at, $status.state, $completed, $status.candidate_count, $status.pid, $status.workers)
            if ($status.state -in @('complete','failed')) { break }
        } else { Get-Content -LiteralPath $statusFile -Raw }
    } catch { Write-Output 'Progress file is being refreshed.' }
    Start-Sleep -Seconds 5
}
