# 서류 자동갱신 예약작업 등록
#   powershell -ExecutionPolicy Bypass -File register_task.ps1
#
# ⚠️ PC 1대에만 등록할 것(현재 회사 PC). 2대에 등록하면 같은 시각에 둘 다 돌아서
# 먼저 push한 쪽만 성공하고 나머지는 non-fast-forward로 실패 → 오알림이 뜬다.
# 다른 PC로 옮길 때는 기존 PC에서 Unregister-ScheduledTask로 먼저 해제한다.
#
# 작업 2개를 등록한다.
#   [일간]  매일 09:00 — 기본서류 전량 + 제품서류 HEAD 크기비교 후 변경분만 수신(대역폭 ~0)
#   [전량]  4주마다 월요일 09:30 — 제품서류 266MB 전량 재수신(-Full).
#           HEAD 비교가 놓치는 경우(개정됐는데 바이트 수 동일, WAF가 HEAD에만 캐시 응답)를 덮는 안전망.
# 둘 다 약 3분. 겹치지 않도록 전량은 일간보다 30분 뒤에 돈다.
$script = Join-Path $PSScriptRoot "refresh_and_push.ps1"
$base   = "-NoProfile -ExecutionPolicy Bypass -File `"$script`""

# ExecutionTimeLimit 4h: 제한이 1시간이던 2026-07-27 실행이 절전/배터리로 멈춘 채 월클록이
# 흘러 강제 종료(0xC000013A)됐고, 강제 종료는 ps1의 catch를 타지 않아 알림도 못 나갔다.
# 배터리 옵션 해제: 전원 상태 때문에 실행이 조용히 차단/중단되는 경로를 없앤다.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "쌍곰 챗봇 서류 갱신" `
    -Action (New-ScheduledTaskAction -Execute "powershell.exe" -Argument $base) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 09:00) -Settings $settings `
    -Description "매일 09:00 ssangkom.co.kr에서 기본서류 갱신 + 제품서류 변경분 수신 후 GitHub push (Render 자동반영). 하트비트 3일 초과 시 GitHub Actions가 알림." -Force | Out-Null

Register-ScheduledTask -TaskName "쌍곰 챗봇 서류 갱신(전량)" `
    -Action (New-ScheduledTaskAction -Execute "powershell.exe" -Argument "$base -Full") `
    -Trigger (New-ScheduledTaskTrigger -Weekly -WeeksInterval 4 -DaysOfWeek Monday -At 09:30) -Settings $settings `
    -Description "4주마다 월요일 09:30 제품서류 전량 재수신(--force). HEAD 크기비교가 놓치는 개정본을 잡는 안전망." -Force | Out-Null

Write-Output "등록 완료"
Write-Output "  [일간] 매일 09:00 (꺼져 있었으면 부팅 직후 실행)"
Write-Output "  [전량] 4주마다 월요일 09:30 -Full"
Write-Output "  실행 제한 4시간, 배터리 차단/중단 해제"
