"""
회사 기본서류 갱신 스크립트
ssangkom.co.kr에서 최신 파일을 다운로드해 company-docs/ 폴더에 저장합니다.
실행 후 git add company-docs/ && git push 하면 Render에 자동 반영됩니다.

사용법:
    python refresh_company_docs.py
"""
import json
import os
import sys
import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

SSANGKOM_BASE = "https://ssangkom.co.kr"
COMPANY_DOCS_URL = f"{SSANGKOM_BASE}/description/documents.php?gubun=2"
OUTPUT_DIR = "company-docs"
MANIFEST_PATH = "company_docs.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 확장자 없는 기준명. 이 순서가 발송 화면·ZIP·doc_indices의 순서다.
# send_logs payload의 doc_indices는 위치 인덱스이므로, 순서를 바꾸면 과거 건 재발송이
# 다른 서류를 보내게 된다 — 항목 추가는 반드시 뒤에 붙일 것.
LABEL_TO_BASENAME = {
    "국세/지방세납세증명서": "tax_certificate",
    "품질경영시스템인증서":   "quality_certification",
    "사업자등록증":           "business_registration",
    "공장등록증":             "factory_registration",
    "납품실적서":             "delivery_record",
}

ALLOWED_EXTS = {"pdf", "jpg", "png", "zip"}


def detect_ext(content: bytes, content_type: str, url: str) -> str | None:
    """실제 내용으로 확장자 판별. 확장자를 하드코딩하면 홈페이지가 같은 서류를 다른
    포맷으로 교체할 때 'jpg 이름의 PDF'가 발송된다(2026-07-27 납품실적서 JPG→PDF).
    HTML(차단 페이지)·미지 포맷은 None."""
    head = content[:8]
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:4] == b"\x89PNG":
        return "png"
    if head[:2] == b"PK":
        return "zip"
    ct = (content_type or "").lower()
    for key, ext in (("pdf", "pdf"), ("jpeg", "jpg"), ("png", "png"), ("zip", "zip")):
        if key in ct:
            return ext
    ext = url.rsplit(".", 1)[-1].lower()
    return ext if ext in ALLOWED_EXTS else None

os.makedirs(OUTPUT_DIR, exist_ok=True)

session = requests.Session()
session.headers.update(HEADERS)
print("메인 페이지 방문...")
session.get(SSANGKOM_BASE, timeout=10)

print("문서 페이지 스크래핑...")
resp = session.get(COMPANY_DOCS_URL, headers={"Referer": SSANGKOM_BASE + "/"}, timeout=15)
resp.encoding = "utf-8"
soup = BeautifulSoup(resp.text, "html.parser")

docs = []
for a in soup.find_all("a", href=True):
    if "/data/document" not in a["href"]:
        continue
    url = SSANGKOM_BASE + a["href"]
    label = ""
    p = a.parent
    for _ in range(8):
        parts = [x.strip() for x in p.get_text(separator="|", strip=True).split("|")
                 if x.strip() and x.strip() != "pdf 파일입니다."]
        if parts:
            label = parts[0]; break
        p = p.parent
    docs.append({"label": label or "서류", "url": url})

for img in soup.find_all("img"):
    src = img.get("src", "")
    if "/data/document" not in src:
        continue
    url = SSANGKOM_BASE + src
    label = ""
    p = img.parent
    for _ in range(8):
        parts = [x.strip() for x in p.get_text(separator="|", strip=True).split("|") if x.strip()]
        if parts:
            label = parts[0]; break
        p = p.parent
    docs.append({"label": label or "서류", "url": url})

print(f"발견된 서류: {len(docs)}개")
updated = 0
saved: dict = {}   # {label: (basename, ext)}
for doc in docs:
    base = LABEL_TO_BASENAME.get(doc["label"])
    if not base:
        print(f"  [SKIP] 매핑 없음: {doc['label']}")
        continue
    r = session.get(doc["url"], headers={"Referer": COMPANY_DOCS_URL}, timeout=20)
    if r.status_code != 200:
        print(f"  [FAIL] {doc['label']}: HTTP {r.status_code}")
        continue

    ext = detect_ext(r.content, r.headers.get("Content-Type", ""), doc["url"])
    if not ext:
        # HTML 차단 페이지·미지 포맷을 서류로 저장하면 그대로 고객에게 발송된다.
        print(f"  [FAIL] {doc['label']}: 포맷 판별 불가(차단 페이지 추정), 기존 파일 유지")
        continue

    fname = f"{base}.{ext}"
    with open(os.path.join(OUTPUT_DIR, fname), "wb") as f:
        f.write(r.content)
    # 포맷이 바뀐 경우 이전 확장자 파일이 남으면 옛 서류가 계속 서빙된다.
    for stale in (f"{base}.{e}" for e in ALLOWED_EXTS if e != ext):
        stale_path = os.path.join(OUTPUT_DIR, stale)
        if os.path.exists(stale_path):
            os.remove(stale_path)
            print(f"  [FORMAT] {doc['label']}: 포맷 변경 → {stale} 삭제, {fname} 로 교체")
    saved[doc["label"]] = (base, ext)
    print(f"  [OK] {doc['label']} -> {fname} ({len(r.content):,} bytes)")
    updated += 1

print(f"\n완료: {updated}개 갱신")
if updated == 0:
    # 0건 = 접근 차단·페이지 변경 추정. 조용히 성공 처리하면 구버전 발송이 방치되므로 실패로 종료.
    sys.exit("갱신 0건 — 접근 차단 또는 페이지 구조 변경. 실패 처리합니다.")

# 확장자 정본 매니페스트 — app.py가 이 파일을 읽어 서빙한다.
# 이번에 못 받은 서류는 기존 매니페스트 값을 유지해 부분 실패로 목록이 줄지 않게 한다.
prev = {}
if os.path.exists(MANIFEST_PATH):
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            prev = {d["label"]: d for d in json.load(f)}
    except Exception as e:
        print(f"기존 매니페스트 읽기 실패(무시): {e}")

manifest = []
for label, base in LABEL_TO_BASENAME.items():   # 순서 고정(doc_indices 호환)
    if label in saved:
        base, ext = saved[label]
    elif label in prev:
        ext = prev[label].get("ext")
    else:
        existing = [e for e in ALLOWED_EXTS if os.path.exists(os.path.join(OUTPUT_DIR, f"{base}.{e}"))]
        ext = existing[0] if existing else None
    if not ext:
        print(f"  [WARN] {label}: 파일 없음 — 매니페스트에서 제외")
        continue
    manifest.append({"label": label, "basename": base, "ext": ext, "filename": f"{base}.{ext}"})

with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print(f"{MANIFEST_PATH} 기록: {len(manifest)}건 ({', '.join(d['filename'] for d in manifest)})")

print("\n다음 명령어로 GitHub에 반영하세요:")
print("  git add company-docs/ company_docs.json")
print("  git commit -m 'chore: 회사 기본서류 갱신'")
print("  git push")
