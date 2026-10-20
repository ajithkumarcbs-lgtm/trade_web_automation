# Runs the bot in the background, hidden, whenever you log in to Windows (and keeps restarting it).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1            # install + start now
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove    # stop + uninstall
#
# Set HEADLESS=true in .env first so no Chrome window appears either. Nothing is shown on screen: follow the
# bot through your Telegram alerts (heartbeat every HEARTBEAT_HOURS) and data\bot.log.
# Stop it any time:  Stop-ScheduledTask -TaskName TradeBot   (start again: Start-ScheduledTask -TaskName TradeBot)

param([switch]$Remove)

$name = "TradeBot"
$root = Split-Path $PSScriptRoot -Parent

if ($Remove) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*$root*main.py*" -or $_.ExecutablePath -like "$root*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host "TradeBot stopped and removed."
    return
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\run_forever.ps1`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Telegram tips -> iob7 trading bot (hidden, restarts itself)" -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "TradeBot installed and started in the background. It starts again every time you log in."
Write-Host "Check: Telegram alerts, data\bot.log. Stop: Stop-ScheduledTask -TaskName TradeBot"
