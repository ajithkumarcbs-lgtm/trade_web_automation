# Keeps the bot running 24/7: if it stops for any reason (crash, network, site problem) it is started again
# after 30 seconds, and the restart is logged in data\restarts.log.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_forever.ps1
#
# To start it automatically when Windows starts: Task Scheduler > Create Task > Trigger "At log on" >
# Action "powershell.exe" with arguments: -ExecutionPolicy Bypass -File "<project folder>\scripts\run_forever.ps1"
# Stop it with Ctrl+C in its window (or end the task).

Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data | Out-Null
while ($true) {
    & .\.venv\Scripts\python.exe main.py
    $line = "$(Get-Date -Format s) bot stopped (exit code $LASTEXITCODE); restarting in 30 s"
    Write-Host $line
    Add-Content -Path data\restarts.log -Value $line -Encoding utf8
    Start-Sleep -Seconds 30
}
