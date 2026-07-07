# 서류 자동갱신 예약작업 등록 (PC당 1회 실행 — 회사/재택 PC 모두 등록 권장)
#   powershell -ExecutionPolicy Bypass -File register_task.ps1
$script = Join-Path $PSScriptRoot "refresh_and_push.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 09:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "쌍곰 챗봇 서류 갱신" -Action $action -Trigger $trigger -Settings $settings `
    -Description "매주 월요일 ssangkom.co.kr에서 기본서류·제품승인서류 갱신 후 GitHub push (Render 자동반영). 미실행 시 화요일 GitHub Actions가 알림 메일." -Force
Write-Output "등록 완료: 매주 월요일 09:00 (꺼져 있었으면 부팅 직후 실행)"
