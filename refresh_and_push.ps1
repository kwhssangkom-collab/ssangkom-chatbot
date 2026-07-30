# 서류 자동 갱신 (Windows 작업 스케줄러용)
# 홈페이지(ssangkom.co.kr)는 클라우드 IP를 차단하므로 실제 갱신은 이 로컬 스크립트가 담당한다.
# 갱신 여부와 무관하게 last_sync.txt(하트비트)를 커밋 → GitHub Actions 감시 워크플로가
# 3일 넘게 하트비트가 없으면 실패 알림을 보낸다.
#
#   (기본) 일간: 제품서류를 HEAD 크기비교로 훑어 변경분만 받는다(대역폭 ~0).
#   -Full  저빈도 정밀검사: 제품서류 266MB 전량 재수신. HEAD가 놓치는 경우를 덮는다.
# 둘 다 약 3분이며 차이는 소요시간이 아니라 홈페이지에 걸리는 부하다.
param([switch]$Full)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot "refresh_and_push.log"
Start-Transcript -Path $log -Force | Out-Null

# git은 네이티브 exe라 실패해도 $ErrorActionPreference="Stop"에 걸리지 않는다.
# 감싸지 않으면 pull/push 실패를 삼킨 채 "갱신분 push 완료"를 출력한다(2026-07-30 확인).
function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    & git @GitArgs
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') 실패 (exit $LASTEXITCODE)" }
}

try {
    Invoke-Git pull --rebase origin master

    # -u(무버퍼) 필수: 파이썬 stdout이 파이프에서 블록 버퍼링되면 실행이 강제 종료될 때
    # 진행 로그가 버퍼째 유실돼 어디서 죽었는지 알 수 없다(2026-07-27 사례).
    py -3 -u refresh_company_docs.py
    if ($LASTEXITCODE -ne 0) { throw "회사 기본서류 갱신 실패 (exit $LASTEXITCODE)" }

    $syncMode = if ($Full) { "--force" } else { "--check" }
    Write-Output "제품 승인서류 동기화 모드: $syncMode"
    py -3 -u sync_product_docs.py $syncMode
    if ($LASTEXITCODE -ne 0) { throw "제품 승인서류 동기화 실패 ($syncMode, exit $LASTEXITCODE)" }

    Get-Date -Format "yyyy-MM-dd HH:mm" | Set-Content last_sync.txt -Encoding ascii

    Invoke-Git add company-docs company_docs.json product-docs document_map.json last_sync.txt
    # 커밋 전에 스테이징된 서류 변경분 캡처 (변경 없는 날에 이전 커밋으로 오판하지 않도록)
    $changed = @(git diff --cached --name-only -- company-docs product-docs)
    # last_sync.txt가 매 실행 바뀌므로 커밋할 것은 항상 있다 — 실패하면 진짜 오류다.
    Invoke-Git commit -m "chore: 서류 자동 갱신 [$(Get-Date -Format 'yyyy-MM-dd')] $syncMode"
    Invoke-Git push origin master
    Write-Output "완료: 갱신분 push (Render 자동 반영)"

    if ($changed.Count) {
        # 갱신된 서류가 있으면 카카오톡으로 목록 통지 (정보성, 실패해도 무시)
        try {
            $tok = ((Get-Content .env -ErrorAction Stop) -match '^ALERT_TOKEN=' |
                    Select-Object -First 1) -replace '^ALERT_TOKEN=', ''
            if ($tok) {
                $names = ($changed | ForEach-Object { [System.IO.Path]::GetFileName($_) } |
                          Select-Object -First 5) -join ", "
                $body = @{ service = "서류 갱신"; level = "info"
                           message = "서류 갱신 $($changed.Count)건: $names" } | ConvertTo-Json
                Invoke-RestMethod -Uri "https://ssangkom-chatbot.onrender.com/alert" -Method Post `
                    -Headers @{ "X-Alert-Token" = $tok } -ContentType "application/json; charset=utf-8" `
                    -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) | Out-Null
            }
        } catch { Write-Output "갱신 통지 실패(무시): $($_.Exception.Message)" }
    }
}
catch {
    # 실패 시 알림 게이트웨이(카카오톡/메일)로 통지 — .env의 ALERT_TOKEN 사용
    try {
        $tok = ((Get-Content .env -ErrorAction Stop) -match '^ALERT_TOKEN=' |
                Select-Object -First 1) -replace '^ALERT_TOKEN=', ''
        if ($tok) {
            $body = @{ service = "서류 자동 갱신(로컬)"; message = "$($_.Exception.Message)" } | ConvertTo-Json
            Invoke-RestMethod -Uri "https://ssangkom-chatbot.onrender.com/alert" -Method Post `
                -Headers @{ "X-Alert-Token" = $tok } -ContentType "application/json; charset=utf-8" `
                -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) | Out-Null
            Write-Output "알림 발송 완료"
        }
    } catch { Write-Output "알림 발송 실패: $($_.Exception.Message)" }
    throw
}
finally {
    Stop-Transcript | Out-Null
}
