# 建立 Windows 工作排程器：每天 12:00、20:00 各跑一次 run_daily.py --live --max 1。
# 2026-11-10 使用者裁示：學「一分鐘新聞」改成人間故事＋原圖，頻率從每小時一則降到一天 2 則，觀察 28 天。
# 會先移除舊的 autonews-hourly / autonews-0700 / autonews-1500。
# 放一個 STOP 檔到專案根目錄即可停機；移除排程：schtasks /Delete /TN "autonews-daily2" /F
$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$script = Join-Path $root "run_daily.py"
# ExecutionTimeLimit 與 config.toml 的 [run] deadline_minutes 一致
$m = Select-String -Path (Join-Path $root "config\config.toml") -Pattern '^\s*deadline_minutes\s*=\s*(\d+)' | Select-Object -First 1
if (-not $m) { throw "config.toml 找不到 deadline_minutes" }
$deadline = [int]$m.Matches[0].Groups[1].Value

foreach ($old in @("autonews-hourly", "autonews-0700", "autonews-1500")) {
    if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $old -Confirm:$false
        Write-Host "已移除 $old"
    }
}

$action   = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" --live --max 1" -WorkingDirectory $root
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At "12:00"),
    (New-ScheduledTaskTrigger -Daily -At "20:00")
)
$setting = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes $deadline) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "autonews-daily2" -Action $action -Trigger $triggers -Settings $setting -Force | Out-Null
Write-Host "已建立 autonews-daily2（每天 12:00、20:00，--max 1，時限 $deadline 分鐘）"
Write-Host "確認：schtasks /Query /TN autonews-daily2 /V /FO LIST"
