"""
품목별 승인서류를 ssangkom.co.kr에서 다운로드해 product-docs/ 폴더에 저장하고
document_map.json에 github_url / filename 필드를 추가한다.

사용법:
  python sync_product_docs.py          # 미캐싱 파일만 다운로드
  python sync_product_docs.py --force  # 전체 재다운로드 (최신본 갱신)

로컬 PC 또는 GitHub Actions에서 실행 (ssangkom.co.kr 접근 가능 환경).
"""
import json
import os
import re
import sys
import time
import urllib.parse

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://ssangkom.co.kr/",
}

GITHUB_RAW = "https://raw.githubusercontent.com/kwhssangkom-collab/ssangkom-chatbot/master/product-docs"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "product-docs")
MAP_PATH   = os.path.join(os.path.dirname(__file__), "document_map.json")


def safe_name(s: str) -> str:
    return re.sub(r'[/\\:*?"<>|]', '', s).strip()


def parse_cd_filename(cd: str) -> str | None:
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*([^;]+)", cd, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if "''" in val:
            _, _, encoded = val.partition("''")
            return urllib.parse.unquote(encoded.strip(), encoding="utf-8")
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def main():
    force = "--force" in sys.argv

    with open(MAP_PATH, encoding="utf-8") as f:
        doc_map: dict = json.load(f)

    total  = sum(len(v) for v in doc_map.values())
    done   = 0
    skip   = 0
    errors = 0

    for product, docs in doc_map.items():
        folder     = safe_name(product)
        folder_dir = os.path.join(OUTPUT_DIR, folder)
        os.makedirs(folder_dir, exist_ok=True)

        for i, doc in enumerate(docs, 1):
            done += 1
            # --force 없이는 이미 캐싱된 파일 건너뜀
            if not force and doc.get("github_url"):
                skip += 1
                continue

            url = doc["url"]
            try:
                resp = requests.get(url, headers=HEADERS, timeout=30)
                if resp.status_code != 200:
                    print(f"[{done}/{total}] HTTP {resp.status_code}: {url}")
                    errors += 1
                    continue

                if not resp.content.startswith(b"%PDF"):
                    print(f"[{done}/{total}] NOT PDF ({len(resp.content)}B): {url}")
                    errors += 1
                    continue

                cd       = resp.headers.get("Content-Disposition", "")
                filename = parse_cd_filename(cd)
                if not filename:
                    filename = f"{safe_name(doc.get('type', '파일'))}_{i}.pdf"
                safe_fname = safe_name(filename)
                if not safe_fname.lower().endswith(".pdf"):
                    safe_fname += ".pdf"

                filepath = os.path.join(folder_dir, safe_fname)
                with open(filepath, "wb") as f:
                    f.write(resp.content)

                github_url = f"{GITHUB_RAW}/{urllib.parse.quote(folder)}/{urllib.parse.quote(safe_fname)}"
                doc["github_url"] = github_url
                doc["filename"]   = safe_fname
                print(f"[{done}/{total}] OK  {folder}/{safe_fname}  ({len(resp.content)//1024}KB)")
                time.sleep(0.3)  # 서버 부하 방지

            except Exception as e:
                print(f"[{done}/{total}] ERROR: {url} - {e}")
                errors += 1

    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(doc_map, f, ensure_ascii=False, indent=2)

    print(f"\n완료: {done}건 처리 / {skip}건 스킵 / {errors}건 실패")
    print("document_map.json 업데이트 완료")
    print("\n다음 명령 실행:")
    print('  git add product-docs document_map.json')
    print('  git commit -m "Add product docs cache (GitHub Raw serving)"')
    print('  git push origin master')


if __name__ == "__main__":
    main()
