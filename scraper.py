"""
쌍곰 홈페이지 기술자료 페이지 스크래핑
실행: python scraper.py
결과: document_map.json 생성

테이블 구조: [체크박스 | 제품명 | 자료구분 | 다운로드]
다운로드 URL: download_dispatch.php?d=숫자 (ID 기반, 파일 수정 시 자동 최신화)
"""
import json
import time
import requests
from bs4 import BeautifulSoup
from collections import defaultdict

BASE_URL = "https://ssangkom.co.kr"
DISPATCH_BASE = "https://ssangkom.co.kr/description/"
LIST_URL = "https://ssangkom.co.kr/description/data.php"

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

def scrape_all():
    print("쌍곰 홈페이지 스크래핑 시작...")

    soup = get_page(1)
    total_pages = get_total_pages(soup)
    print(f"총 {total_pages}페이지 발견")

    all_items = parse_page(soup)
    print(f"  1/{total_pages} 페이지 완료 ({len(all_items)}건)")

    for page in range(2, total_pages + 1):
        time.sleep(0.5)
        soup = get_page(page)
        items = parse_page(soup)
        all_items.extend(items)
        print(f"  {page}/{total_pages} 페이지 완료 (누적: {len(all_items)}건)")

    print(f"\n총 {len(all_items)}개 파일 수집 완료")
    return all_items

def build_document_map(items):
    """
    품목명 기준으로 그룹핑
    {
      "제품명": [
        {"type": "MSDS", "url": "https://...dispatch.php?d=12"},
        ...
      ]
    }
    """
    doc_map = defaultdict(list)
    for item in items:
        doc_map[item["product"]].append({
            "type": item["type"],
            "url": item["url"],
        })
    return dict(doc_map)

def print_summary(doc_map):
    print("\n=== 수집 결과 요약 ===")
    print(f"총 품목 수: {len(doc_map)}개")
    total_files = sum(len(v) for v in doc_map.values())
    print(f"총 파일 수: {total_files}개")
    print("\n품목별 파일 수 (상위 10개):")
    sorted_items = sorted(doc_map.items(), key=lambda x: len(x[1]), reverse=True)
    for name, files in sorted_items[:10]:
        print(f"  {name}: {len(files)}개")
    print("\n품목 목록 (전체):")
    for name in sorted(doc_map.keys()):
        print(f"  - {name}")

if __name__ == "__main__":
    items = scrape_all()

    with open("raw_items.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    doc_map = build_document_map(items)

    with open("document_map.json", "w", encoding="utf-8") as f:
        json.dump(doc_map, f, ensure_ascii=False, indent=2)

    print_summary(doc_map)
    print("\n완료! document_map.json을 확인하세요.")
