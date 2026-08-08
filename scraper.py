"""
쌍곰 홈페이지 기술자료 페이지 스크래핑
실행: python scraper.py [--dry-run] [--prune]
결과: document_map.json 갱신 (기존 항목의 filename/github_url 은 보존하고 병합한다)

테이블 구조: [체크박스 | 제품명 | 자료구분 | 다운로드]
다운로드 URL: download_dispatch.php?d=숫자 (ID 기반, 파일 수정 시 자동 최신화)

--dry-run : 변경 내역만 출력하고 파일을 쓰지 않는다
--prune   : 사이트에서 사라진 항목을 map 에서 제거한다 (기본은 남긴다)

⚠️ 통째로 덮어쓰면 sync_product_docs.py 가 채운 filename/github_url 이 날아가
   424건 전량 재다운로드가 발생한다. 그래서 병합이 기본이다.
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

# 한글 제품명을 CP949 콘솔에 출력하다 죽지 않도록
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_URL = "https://ssangkom.co.kr"
DISPATCH_BASE = "https://ssangkom.co.kr/description/"
LIST_URL = "https://ssangkom.co.kr/description/data.php"
MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "document_map.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def get_page(page_num):
    params = {"categories_all": "1", "stext": "", "page": page_num}
    resp = requests.get(LIST_URL, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return BeautifulSoup(resp.text, "html.parser")


def parse_page(soup):
    items = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        product_name = tds[1].get_text(strip=True)
        doc_type = tds[2].get_text(strip=True)

        if not product_name:
            continue

        # dispatch URL 우선 사용 (ID 기반이라 파일 수정 후에도 URL 유지)
        hrefs = [a["href"] for a in tr.find_all("a", href=True)]
        dispatch_url = None
        direct_url = None

        for href in hrefs:
            if "download_dispatch.php" in href:
                dispatch_url = DISPATCH_BASE + href if not href.startswith("http") else href
            elif "/data/document/" in href:
                direct_url = BASE_URL + href if not href.startswith("http") else href

        # dispatch URL 우선, 없으면 direct URL
        url = dispatch_url or direct_url
        if not url:
            continue

        items.append({
            "product": product_name,
            "type": doc_type,
            "url": url,
        })

    return items


def get_total_pages(soup):
    max_page = 1
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "page=" in href:
            try:
                p = int(href.split("page=")[-1].split("&")[0])
                max_page = max(max_page, p)
            except ValueError:
                pass
    return max_page


def get_total_count(soup):
    """목록 상단 '총 N건의 기술자료가 있습니다' 를 읽는다. 수집 검증에 쓴다."""
    el = soup.find(class_="count")
    if el:
        m = re.search(r"\d+", el.get_text())
        if m:
            return int(m.group())
    return None


def scrape_all():
    print("쌍곰 홈페이지 스크래핑 시작...")

    soup = get_page(1)
    total_pages = get_total_pages(soup)
    total_count = get_total_count(soup)
    print(f"총 {total_pages}페이지 / 사이트 표시 건수 {total_count}건")

    all_items = parse_page(soup)
    print(f"  1/{total_pages} 페이지 완료 ({len(all_items)}건)")

    page = 2
    while page <= total_pages:
        time.sleep(0.5)
        items = parse_page(get_page(page))
        # 페이지네이션 링크가 끝을 덜 알려주는 경우가 있어, 빈 페이지가 나오면 멈춘다
        if not items:
            print(f"  {page} 페이지가 비어 종료")
            break
        all_items.extend(items)
        print(f"  {page}/{total_pages} 페이지 완료 (누적: {len(all_items)}건)")
        page += 1

    # 마지막 페이지 이후에도 남아 있는지 한 번 더 확인 (총건수가 링크보다 클 때)
    if total_count and len(all_items) < total_count:
        while True:
            time.sleep(0.5)
            items = parse_page(get_page(page))
            if not items:
                break
            all_items.extend(items)
            print(f"  {page}(추가) 페이지 완료 (누적: {len(all_items)}건)")
            page += 1

    print(f"\n총 {len(all_items)}개 파일 수집 완료")

    # 🔴 검증 — 사이트가 말한 건수와 다르면 부분 수집이다. 조용히 넘어가면
    #    "변경 없음"으로 위장되므로 여기서 끊는다.
    if total_count is not None and len(all_items) != total_count:
        print(f"[오류] 사이트 표시 {total_count}건 != 수집 {len(all_items)}건 — 부분 수집으로 판단해 중단",
              file=sys.stderr)
        sys.exit(1)

    return all_items


def load_existing():
    if not os.path.exists(MAP_PATH):
        return {}
    with open(MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


def merge(existing, items, prune=False):
    """
    (제품명, url) 을 키로 병합한다.
    기존 항목의 filename/github_url 은 sync_product_docs.py 가 채운 값이라 보존한다.
    """
    kept = {(p, d["url"]): d for p, ds in existing.items() for d in ds}
    site_keys = {(it["product"], it["url"]) for it in items}

    doc_map = defaultdict(list)
    added, preserved = [], 0

    for it in items:
        key = (it["product"], it["url"])
        old = kept.get(key)
        if old:
            entry = dict(old)
            entry["type"] = it["type"]          # 사이트에서 자료구분이 바뀌었을 수 있다
            preserved += 1
        else:
            entry = {"type": it["type"], "url": it["url"]}
            added.append(it)
        doc_map[it["product"]].append(entry)

    # 사이트에서 사라진 항목
    removed = [(p, d) for (p, u), d in kept.items() if (p, u) not in site_keys]
    if not prune:
        for (p, u), d in kept.items():
            if (p, u) not in site_keys:
                doc_map[p].append(d)

    return dict(doc_map), added, removed, preserved


def report(added, removed, preserved, prune):
    print("\n=== 변경 요약 ===")
    print(f"기존 유지(파일 재다운로드 없음): {preserved}건")
    print(f"신규: {len(added)}건")
    for it in added:
        print(f"  + [{it['product']}] {it['type']} :: {it['url']}")
    print(f"사이트에서 사라짐: {len(removed)}건" + ("  → --prune 지정으로 제거" if prune else "  → map 에 남김(제거하려면 --prune)"))
    for p, d in removed:
        print(f"  - [{p}] {d.get('type')} :: {d.get('filename') or d.get('url')}")


def print_summary(doc_map):
    print("\n=== 수집 결과 요약 ===")
    print(f"총 품목 수: {len(doc_map)}개")
    total_files = sum(len(v) for v in doc_map.values())
    print(f"총 파일 수: {total_files}개")
    print("\n품목별 파일 수 (상위 10개):")
    sorted_items = sorted(doc_map.items(), key=lambda x: len(x[1]), reverse=True)
    for name, files in sorted_items[:10]:
        print(f"  {name}: {len(files)}개")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="변경 내역만 출력하고 파일을 쓰지 않는다")
    ap.add_argument("--prune", action="store_true", help="사이트에서 사라진 항목을 map 에서 제거한다")
    args = ap.parse_args()

    items = scrape_all()
    existing = load_existing()
    doc_map, added, removed, preserved = merge(existing, items, prune=args.prune)

    report(added, removed, preserved, args.prune)

    if args.dry_run:
        print("\n[dry-run] 파일을 쓰지 않았습니다.")
        sys.exit(0)

    with open(os.path.join(os.path.dirname(MAP_PATH), "raw_items.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(doc_map, f, ensure_ascii=False, indent=2)

    print_summary(doc_map)
    print(f"\n완료! 신규 {len(added)}건이 document_map.json 에 추가됐습니다.")
    print("다음: py -3 sync_product_docs.py  (신규분만 내려받습니다)")
