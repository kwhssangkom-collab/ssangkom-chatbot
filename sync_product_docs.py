"""
품목별 승인서류를 ssangkom.co.kr에서 다운로드해 product-docs/ 폴더에 저장하고
document_map.json에 github_url / filename 필드를 추가한다.

사용법:
  python sync_product_docs.py          # 미캐싱 파일만 다운로드
  python sync_product_docs.py --check  # HEAD로 크기 비교 → 달라진 것만 다운로드 (일간용)
  python sync_product_docs.py --force  # 전체 재다운로드 (저빈도 정밀검사용)

--check는 424건을 HEAD로 훑어(헤더만, 2~4분) Content-Length가 캐시와 다른 건만 GET한다.
--force는 266MB 전량을 받는다(3분). --check가 못 잡는 경우는 개정됐는데 바이트 수가
정확히 같은 서류, 또는 WAF가 HEAD에만 캐시 응답을 주는 구성 — 그래서 --force 전량
패스를 저빈도로 남겨 둔다.

로컬 PC 또는 GitHub Actions에서 실행 (ssangkom.co.kr 접근 가능 환경).
"""
import json
import mimetypes
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


def cached_size(folder_dir: str, filename: str | None) -> int | None:
    """캐시된 파일의 바이트 크기. 없으면 None."""
    if not filename:
        return None
    path = os.path.join(folder_dir, safe_name(filename))
    return os.path.getsize(path) if os.path.exists(path) else None


def remote_size(url: str) -> int | None:
    """HEAD로 Content-Length 조회. 실패·미제공이면 None → 호출부는 GET으로 폴백한다."""
    try:
        r = requests.head(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            return None
        cl = r.headers.get("Content-Length")
        return int(cl) if cl and cl.isdigit() else None
    except Exception:
        return None


def main():
    force = "--force" in sys.argv
    check = "--check" in sys.argv

    with open(MAP_PATH, encoding="utf-8") as f:
        doc_map: dict = json.load(f)

    total  = sum(len(v) for v in doc_map.values())
    done   = 0
    skip   = 0
    errors = 0
    probed = 0   # --check: HEAD를 시도한 건수
    unread = 0   # --check: HEAD로 크기를 못 얻은 건수(차단 감지용)

    for product, docs in doc_map.items():
        folder     = safe_name(product)
        folder_dir = os.path.join(OUTPUT_DIR, folder)
        os.makedirs(folder_dir, exist_ok=True)

        for i, doc in enumerate(docs, 1):
            done += 1
            url = doc["url"]

            if doc.get("github_url") and not force:
                if not check:
                    # 기본 모드: 이미 캐싱된 파일은 건너뜀
                    skip += 1
                    continue
                # --check: 크기가 같으면 내용도 같다고 보고 GET 생략.
                # 크기를 못 얻으면(HEAD 미지원·차단) 안전하게 GET으로 내려간다.
                local = cached_size(folder_dir, doc.get("filename"))
                probed += 1
                size = remote_size(url)
                if size is None:
                    unread += 1
                elif local is not None and size == local:
                    skip += 1
                    continue
                else:
                    print(f"[{done}/{total}] 변경 감지: {folder}/{doc.get('filename')} "
                          f"({local} → {size} bytes)")

            try:
                resp = requests.get(url, headers=HEADERS, timeout=30)
                if resp.status_code != 200:
                    print(f"[{done}/{total}] HTTP {resp.status_code}: {url}")
                    errors += 1
                    continue

                ctype = resp.headers.get("Content-Type", "").lower()
                # HTML(Incapsula 차단/오류 페이지) 또는 빈 응답만 거름. PDF/JPG/PNG/ZIP 등 모두 허용.
                if (not resp.content
                        or "text/html" in ctype
                        or resp.content.lstrip()[:1] == b"<"):
                    print(f"[{done}/{total}] 비정상 응답(차단 추정), 건너뜀: {url}")
                    errors += 1
                    continue

                # 내용이 기존 캐시와 동일하면 유지 — 서버가 다운로드마다 새 파일명을
                # 부여하는 서류(ZIP 등)의 무의미한 파일명 교체·커밋 오염 방지
                old_fn = doc.get("filename")
                if old_fn:
                    old_path = os.path.join(folder_dir, safe_name(old_fn))
                    if os.path.exists(old_path):
                        with open(old_path, "rb") as f:
                            if f.read() == resp.content:
                                skip += 1
                                continue

                cd       = resp.headers.get("Content-Disposition", "")
                filename = parse_cd_filename(cd)
                if not filename:
                    ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) or ".bin"
                    filename = f"{safe_name(doc.get('type', '파일'))}_{i}{ext}"
                safe_fname = safe_name(filename)
                if "." not in safe_fname:        # 확장자 없으면 PDF 가정(과거 호환)
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

    # 맵에서 참조하지 않는 캐시 파일 정리 — 서버가 매 다운로드마다 새 파일명을 주는
    # 서류(ZIP 등)는 --force 재실행 시 구파일이 고아로 남아 저장소가 무한 증식한다.
    referenced = {(safe_name(p), d["filename"]) for p, ds in doc_map.items() for d in ds if d.get("filename")}
    removed = 0
    for folder in os.listdir(OUTPUT_DIR):
        fdir = os.path.join(OUTPUT_DIR, folder)
        if not os.path.isdir(fdir):
            continue
        for fn in os.listdir(fdir):
            if (folder, fn) not in referenced:
                os.remove(os.path.join(fdir, fn))
                removed += 1
    if removed:
        print(f"고아 캐시 파일 정리: {removed}건 삭제")

    mode_label = "--check" if check else ("--force" if force else "기본")
    print(f"\n완료[{mode_label}]: {done}건 처리 / {skip}건 스킵 / {errors}건 실패")
    if check:
        print(f"HEAD 검사 {probed}건 / 크기 미확인 {unread}건")
    print("document_map.json 업데이트 완료")

    # --check에서 HEAD가 전 건 실패하면 "변경 없음"으로 조용히 성공 처리된다.
    # 그 위장이 5/26~7/7 구버전 발송 사고의 원인이었으므로 차단으로 간주해 실패시킨다.
    if check and probed > 1 and unread >= probed:
        sys.exit("HEAD 전 건 크기 미확인 — 접근 차단 추정. 실패 처리합니다.")

    attempted = done - skip
    # ponytail: attempted>1 기준 — 상시 접근불가 서류 1건(S-905, d=333)의 단독 실패는 통과,
    # 차단 환경(전 건 수백 개 실패)만 잡는다. 상시 실패 서류가 늘면 임계값 재검토.
    if errors and errors >= attempted and attempted > 1:
        sys.exit("시도한 전 건 실패 — 접근 차단 추정. 실패 처리합니다.")
    print("\n다음 명령 실행:")
    print('  git add product-docs document_map.json')
    print('  git commit -m "Add product docs cache (GitHub Raw serving)"')
    print('  git push origin master')


if __name__ == "__main__":
    main()
