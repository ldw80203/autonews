# 建立 Windows 工作排程器：每小時整點跑一次 run_daily.py --live --max 1（排到下一個整點發文）。
# 2026-11-09 使用者裁示：24 小時、每小時一則。會先移除舊的 autonews-0700 / autonews-1500。
# 放一個 STOP 檔到專案根目錄即可停機；移除排程：schtasks /Delete /TN "autonews-hourly" /F
$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$script = Join-Path $root "run_daily.py"
# ExecutionTimeLimit 與 config.toml 的 [run] deadline_minutes 一致
$m = Select-String -Path (Join-Path $root "config\config.toml") -Pattern '^\s*deadline_minutes\s*=\s*(\d+)' | Select-Object -First 1
if (-not $m) { throw "config.toml 找不到 deadline_minutes" }
$deadline = [int]$m.Matches[0].Groups[1].Value

foreach ($old in @("autonews-0700", "autonews-1500")) {
    if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $old -Confirm:$false
        Write-Host "已移除 $old"
    }
}

$action  = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" --live --max 1" -WorkingDirectory $root
# 每日 00:00 起、每 1 小時重複、持續 24 小時 = 全天每個整點
$trigger = New-ScheduledTaskTrigger -Daily -At "00:00"
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At "00:00" -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition
$setting = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes $deadline) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "autonews-hourly" -Action $action -Trigger $trigger -Settings $setting -Force | Out-Null
Write-Host "已建立 autonews-hourly（每小時整點，--max 1，時限 $deadline 分鐘）"
Write-Host "確認：schtasks /Query /TN autonews-hourly /V /FO LIST"
