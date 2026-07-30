# 서류 자동갱신 예약작업 등록 (PC당 1회 실행 — 회사/재택 PC 모두 등록 권장)
#   powershell -ExecutionPolicy Bypass -File register_task.ps1
$script = Join-Path $PSScriptRoot "refresh_and_push.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 09:00
# ExecutionTimeLimit 4h: 제품서류 424건 전량 재다운로드(--force)는 1시간을 넘길 수 있다.
# 제한이 1시간이던 2026-07-27 실행이 sync 도중 강제 종료(0xC000013A)돼 갱신분이 유실됐다.
# 강제 종료는 ps1의 catch를 타지 않아 알림도 못 나가므로 제한은 넉넉해야 한다.
# 배터리 옵션 해제: 전원 상태 때문에 실행이 조용히 차단/중단되는 경로를 없앤다.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "쌍곰 챗봇 서류 갱신" -Action $action -Trigger $trigger -Settings $settings `
    -Description "매주 월요일 ssangkom.co.kr에서 기본서류·제품승인서류 갱신 후 GitHub push (Render 자동반영). 미실행 시 화요일 GitHub Actions가 알림 메일." -Force
Write-Output "등록 완료: 매주 월요일 09:00 (꺼져 있었으면 부팅 직후 실행), 실행 제한 4시간"
