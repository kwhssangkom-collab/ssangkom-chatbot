"""
Supabase Storage의 ssangkom-zips 버킷에서 24시간 지난 ZIP을 삭제한다.
동시에 호출 자체가 프로젝트 활동으로 기록되어 무료 플랜 일시정지(7일 미사용)를 막는 keep-alive 역할도 한다.

GitHub Actions에서 주기적으로 실행 (SUPABASE_URL / SUPABASE_SERVICE_KEY 시크릿 필요).
로컬 테스트: 환경변수 설정 후 `python cleanup_supabase_zips.py`
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

URL    = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
KEY    = os.environ.get("SUPABASE_SERVICE_KEY")
BUCKET = os.environ.get("SUPABASE_BUCKET", "ssangkom-zips")
TTL_HOURS = 24

if not (URL and KEY):
    print("SUPABASE_URL / SUPABASE_SERVICE_KEY 미설정")
    sys.exit(1)

AUTH = {"Authorization": f"Bearer {KEY}", "apikey": KEY, "Content-Type": "application/json"}


def _parse(ts: str) -> datetime:
    # 예: "2026-06-10T17:07:40.123Z"
    ts = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return datetime.fromisoformat(ts.split(".")[0] + "+00:00")


def main():
    resp = requests.post(
        f"{URL}/storage/v1/object/list/{BUCKET}",
        headers=AUTH,
        json={"limit": 1000, "offset": 0, "prefix": "",
              "sortBy": {"column": "created_at", "order": "asc"}},
        timeout=30,
    )
    resp.raise_for_status()
    objs = resp.json()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=TTL_HOURS)

    stale = []
    for o in objs:
        name = o.get("name")
        created = o.get("created_at")
        if not name or not created:
            continue
        if _parse(created) < cutoff:
            stale.append(name)

    print(f"객체 {len(objs)}개 / 만료 대상 {len(stale)}개 (keep-alive 핑 완료)")

    if stale:
        d = requests.delete(
            f"{URL}/storage/v1/object/{BUCKET}",
            headers=AUTH, json={"prefixes": stale}, timeout=30,
        )
        if d.status_code == 200:
            print(f"삭제 완료: {len(stale)}개")
        else:
            print(f"삭제 실패 {d.status_code}: {d.text[:200]}")
            sys.exit(1)


if __name__ == "__main__":
    main()
