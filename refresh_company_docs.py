"""
회사 기본서류 갱신 스크립트
ssangkom.co.kr에서 최신 파일을 다운로드해 company-docs/ 폴더에 저장합니다.
실행 후 git add company-docs/ && git push 하면 Render에 자동 반영됩니다.

사용법:
    python refresh_company_docs.py
"""
import os
import sys
import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

SSANGKOM_BASE = "https://ssangkom.co.kr"
COMPANY_DOCS_URL = f"{SSANGKOM_BASE}/description/documents.php?gubun=2"
OUTPUT_DIR = "company-docs"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

LABEL_TO_FILENAME = {
    "국세/지방세납세증명서": "tax_certificate.pdf",
    "품질경영시스템인증서":   "quality_certification.pdf",
    "사업자등록증":           "business_registration.jpg",
    "공장등록증":             "factory_registration.jpg",
    "납품실적서":             "delivery_record.jpg",
}

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
for doc in docs:
    fname = LABEL_TO_FILENAME.get(doc["label"])
    if not fname:
        print(f"  [SKIP] 매핑 없음: {doc['label']}")
        continue
    r = session.get(doc["url"], headers={"Referer": COMPANY_DOCS_URL}, timeout=20)
    if r.status_code == 200:
        path = os.path.join(OUTPUT_DIR, fname)
        with open(path, "wb") as f:
            f.write(r.content)
        print(f"  [OK] {doc['label']} -> {fname} ({len(r.content):,} bytes)")
        updated += 1
    else:
        print(f"  [FAIL] {doc['label']}: HTTP {r.status_code}")

print(f"\n완료: {updated}개 갱신")
if updated > 0:
    print("\n다음 명령어로 GitHub에 반영하세요:")
    print("  git add company-docs/")
    print("  git commit -m 'chore: 회사 기본서류 갱신'")
    print("  git push")
