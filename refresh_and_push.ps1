# 주간 서류 자동 갱신 (Windows 작업 스케줄러용)
# 홈페이지(ssangkom.co.kr)는 클라우드 IP를 차단하므로 실제 갱신은 이 로컬 스크립트가 담당한다.
# 갱신 여부와 무관하게 last_sync.txt(하트비트)를 커밋 → GitHub Actions 감시 워크플로가
# 10일 넘게 하트비트가 없으면 실패 알림을 보낸다.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot "refresh_and_push.log"
Start-Transcript -Path $log -Force | Out-Null

try {
    git pull --rebase origin master

    py -3 refresh_company_docs.py
    if ($LASTEXITCODE -ne 0) { throw "회사 기본서류 갱신 실패 (exit $LASTEXITCODE)" }

    py -3 sync_product_docs.py --force
    if ($LASTEXITCODE -ne 0) { throw "제품 승인서류 동기화 실패 (exit $LASTEXITCODE)" }

    Get-Date -Format "yyyy-MM-dd HH:mm" | Set-Content last_sync.txt -Encoding ascii

    git add company-docs product-docs document_map.json last_sync.txt
    git commit -m "chore: 서류 자동 갱신 [$(Get-Date -Format 'yyyy-MM-dd')]"
    git push origin master
    Write-Output "완료: 갱신분 push (Render 자동 반영)"
}
finally {
    Stop-Transcript | Out-Null
}
