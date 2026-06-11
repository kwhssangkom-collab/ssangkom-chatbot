"""
쌍곰 카카오톡 챗봇 서버
- 카카오 오픈빌더 웹훅 처리
- 품목 검색 → ZIP 생성 → 다운로드 링크 이메일 발송
"""
import base64
import io
import json
import os
import re
import threading
import urllib.parse
import uuid
import zipfile
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, Response, redirect

load_dotenv()

app = Flask(__name__)

# ── 설정 ─────────────────────────────────────────────
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")       # SMTP 앱 비밀번호 (로컬 fallback)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")
COMPANY_NAME = os.getenv("COMPANY_NAME", "쌍곰")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://localhost:5000")

# Supabase Storage (다운로드 ZIP 보관 — 서버 재시작과 무관하게 24h 유지)
# anon 키는 외부 노출 전제로 설계된 공개키이며, 접근 제어는 Storage RLS 정책으로 처리.
# 버킷은 public(읽기) + 추측 불가능한 UUID 파일명 + 6시간마다 24h 경과분 자동삭제로 보호.
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "https://pqpjrrtjpljalgvifgbp.supabase.co").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBxcGpycnRqcGxqYWxndmlmZ2JwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODExMTEyNjAsImV4cCI6MjA5NjY4NzI2MH0."
    "BkHS7TchHgeHPUUbR67Y9C3lqt951OBd94Lonj1PE6A"
)
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "ssangkom-zips")
LINK_TTL_SECONDS = 86400  # 24시간

# 관리자 엔드포인트 보호 (미설정 시 /admin/* 비활성 — fail-closed)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
# 발송 실패/부분발송 경고를 받을 관리자 이메일 (미설정 시 발송 계정으로)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL") or os.getenv("GMAIL_USER")

# 발송 남용 방지: IP 레이트리밋 + ZIP 동시 생성 제한(OOM 방지)
RATE_PER_MIN  = int(os.getenv("RATE_PER_MIN", "5"))
RATE_PER_HOUR = int(os.getenv("RATE_PER_HOUR", "30"))
_rate_lock = threading.Lock()
_rate_hits: dict = {}              # {ip: [timestamps]}
_zip_semaphore = threading.Semaphore(2)  # 동시 ZIP 생성 최대 2건


def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_limited_memory(ip: str) -> bool:
    """프로세스별 인메모리 슬라이딩 윈도우 (DB 폴백용)."""
    now = datetime.now().timestamp()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(ip, []) if now - t < 3600]
        per_min = sum(1 for t in hits if now - t < 60)
        if per_min >= RATE_PER_MIN or len(hits) >= RATE_PER_HOUR:
            _rate_hits[ip] = hits
            return True
        hits.append(now)
        _rate_hits[ip] = hits
        if len(_rate_hits) > 5000:  # 메모리 누수 방지
            for k in [k for k, v in _rate_hits.items() if not v or now - v[-1] > 3600]:
                _rate_hits.pop(k, None)
        return False


def _rate_limited_db(ip: str):
    """Supabase 공유 카운터(워커/재시작 무관). 사용불가/오류 시 None."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return None
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/check_rate_limit",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json"},
            json={"p_ip": ip, "p_per_min": RATE_PER_MIN, "p_per_hour": RATE_PER_HOUR},
            timeout=4,
        )
        if r.status_code == 200:
            return r.json() is False   # allowed=False → 차단
        return None
    except Exception:
        return None


def _rate_limited() -> bool:
    """IP당 분/시간 한도 초과 시 True. 공유 카운터 우선, 장애 시 인메모리 폴백(fail-open)."""
    ip = _client_ip()
    db = _rate_limited_db(ip)
    if db is not None:
        return db
    return _rate_limited_memory(ip)

TEMP_DIR = os.path.join(os.getenv("TMPDIR", "/tmp"), "ssangkom_zips")
os.makedirs(TEMP_DIR, exist_ok=True)

# 이메일 도메인 드롭다운 (자주 쓰는 도메인)
COMMON_EMAIL_DOMAINS = [
    "gmail.com", "naver.com", "daum.net", "hanmail.net", "nate.com",
    "kakao.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
]

# DNS MX 조회용 (없으면 도메인 검증은 형식 검사로 degrade)
try:
    import dns.resolver as _dns_resolver
    _DNS_OK = True
except ImportError:
    _DNS_OK = False

_mx_cache: dict = {}  # {domain: (valid: bool, reason: str)}


def verify_email_domain(email: str):
    """이메일 도메인이 실제 메일을 받을 수 있는지 MX/A 레코드로 확인.
    반환: (valid: bool, reason: str). 라이브러리 미설치/네트워크 오류는 통과(오탐 방지)."""
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return False, "도메인이 비어 있습니다"
    if not _DNS_OK:
        return True, ""
    if domain in _mx_cache:
        return _mx_cache[domain]

    result = (True, "")
    try:
        answers = _dns_resolver.resolve(domain, "MX", lifetime=5)
        if len(answers) > 0:
            result = (True, "")
        else:
            raise _dns_resolver.NoAnswer
    except _dns_resolver.NXDOMAIN:
        result = (False, "존재하지 않는 도메인입니다")
    except (_dns_resolver.NoAnswer, _dns_resolver.NoNameservers):
        # MX 없으면 A 레코드라도 있으면 메일 수신 가능 (RFC 5321)
        try:
            _dns_resolver.resolve(domain, "A", lifetime=5)
            result = (True, "")
        except _dns_resolver.NXDOMAIN:
            result = (False, "존재하지 않는 도메인입니다")
        except Exception:
            result = (False, "메일을 받을 수 없는 도메인입니다")
    except Exception:
        # 타임아웃 등 일시적 오류는 통과
        result = (True, "")

    _mx_cache[domain] = result
    return result

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ── 품목 매핑 로드 ────────────────────────────────────
with open("document_map.json", encoding="utf-8") as f:
    DOCUMENT_MAP: dict = json.load(f)

PRODUCT_NAMES = list(DOCUMENT_MAP.keys())

# ── 회사 기본서류 ────────────────────────────────────
SSANGKOM_BASE = "https://ssangkom.co.kr"

_GITHUB_RAW = "https://raw.githubusercontent.com/kwhssangkom-collab/ssangkom-chatbot/master/company-docs"

# ssangkom.co.kr은 클라우드 IP에 JS 챌린지를 적용하므로 직접 스크래핑 불가.
# 회사 기본서류를 GitHub 레포에 보관하고 Raw URL로 서빙.
# 파일 갱신 시: python refresh_company_docs.py 실행 후 git push.
COMPANY_DOCS_LIST = [
    {"label": "국세/지방세납세증명서", "url": f"{_GITHUB_RAW}/tax_certificate.pdf",       "ext": "pdf"},
    {"label": "품질경영시스템인증서",   "url": f"{_GITHUB_RAW}/quality_certification.pdf", "ext": "pdf"},
    {"label": "사업자등록증",           "url": f"{_GITHUB_RAW}/business_registration.jpg", "ext": "jpg"},
    {"label": "공장등록증",             "url": f"{_GITHUB_RAW}/factory_registration.jpg",  "ext": "jpg"},
    {"label": "납품실적서",             "url": f"{_GITHUB_RAW}/delivery_record.jpg",        "ext": "jpg"},
]


def fetch_company_docs() -> list[dict]:
    return COMPANY_DOCS_LIST

# ── 임시 링크 만료 관리 ───────────────────────────────
expiry_map: dict = {}  # {file_id: datetime}

# ── 대화 세션 (사용자별 진행 상태) ────────────────────
# 실제 운영 시 Redis 권장 (재시작 시 세션 초기화됨)
sessions: dict = {}

# ═══════════════════════════════════════════════════
# 품목 검색
# ═══════════════════════════════════════════════════

def search_products(query: str, top_n: int = 5) -> list[str]:
    """입력 텍스트와 유사한 품목명 반환 (유사도 순)"""
    query = query.strip().lower()

    # 완전 일치
    for name in PRODUCT_NAMES:
        if name.lower() == query:
            return [name]

    # 부분 포함
    contains = [n for n in PRODUCT_NAMES if query in n.lower()]
    if contains:
        return contains[:top_n]

    # 유사도 기반 퍼지 매칭
    scored = []
    for name in PRODUCT_NAMES:
        ratio = SequenceMatcher(None, query, name.lower()).ratio()
        scored.append((ratio, name))
    scored.sort(reverse=True)

    return [name for ratio, name in scored[:top_n] if ratio > 0.3]


# ═══════════════════════════════════════════════════
# ZIP 생성 & 임시 링크
# ═══════════════════════════════════════════════════

def _parse_cd_filename(cd_header: str) -> str | None:
    """Content-Disposition 헤더에서 파일명 추출 (RFC 5987 filename* 우선)"""
    if not cd_header:
        return None
    m = re.search(r"filename\*\s*=\s*([^;]+)", cd_header, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if "''" in val:
            _, _, encoded = val.partition("''")
            return urllib.parse.unquote(encoded.strip(), encoding="utf-8")
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd_header, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def create_zip(product_names: list[str]):
    """품목들의 파일을 ZIP으로 묶고 (다운로드 URL, 실제 담긴 파일수) 반환 (디스크 스트리밍)"""
    file_id  = str(uuid.uuid4())
    zip_path = os.path.join(TEMP_DIR, f"{file_id}.zip")
    added    = 0
    with _zip_semaphore:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for product in product_names:
                docs = DOCUMENT_MAP.get(product, [])
                safe_folder = re.sub(r'[/\\:*?"<>|]', '', product)
                for i, doc in enumerate(docs, 1):
                    try:
                        # github_url 캐시 우선 사용 (Incapsula 우회)
                        fetch_url = doc.get("github_url") or doc["url"]
                        resp = requests.get(fetch_url, headers=HEADERS, timeout=20)
                        if resp.status_code != 200:
                            print(f"파일 다운로드 실패 (HTTP {resp.status_code}): {fetch_url}")
                            continue
                        # HTML(차단/오류 페이지)·빈 응답만 거름 (PDF/JPG/ZIP 등 모두 허용)
                        if not resp.content or resp.content.lstrip()[:1] == b"<":
                            print(f"비정상 응답(차단 추정, 건너뜀): {fetch_url} - 첫 bytes: {resp.content[:40]}")
                            continue
                        # 파일명 결정: 캐시된 filename → Content-Disposition → 타입+인덱스
                        if doc.get("filename"):
                            safe_filename = re.sub(r'[/\\:*?"<>|]', '', doc["filename"])
                        else:
                            cd = resp.headers.get("Content-Disposition", "")
                            real_name = _parse_cd_filename(cd)
                            if real_name:
                                safe_filename = re.sub(r'[/\\:*?"<>|]', '', real_name)
                            else:
                                doc_type = doc.get("type", "파일")
                                safe_filename = f"{doc_type}_{i}.pdf"
                        zf.writestr(f"{safe_folder}/{safe_filename}", resp.content)
                        added += 1
                    except Exception as e:
                        print(f"파일 다운로드 실패: {doc['url']} - {e}")

    return _finalize_zip(file_id, zip_path), added


def _cleanup(path: str, file_id: str):
    if os.path.exists(path):
        os.remove(path)
    expiry_map.pop(file_id, None)


def _upload_to_supabase(file_id: str, zip_path: str) -> str | None:
    """디스크의 ZIP을 Supabase Storage(public 버킷)에 스트리밍 업로드 후 직접 URL 반환."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return None
    path = f"{file_id}.zip"
    auth = {"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}
    try:
        with open(zip_path, "rb") as f:
            up = requests.post(
                f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{path}",
                headers={**auth, "Content-Type": "application/zip"},  # 새 UUID라 upsert 불필요(SELECT 권한 회피)
                data=f, timeout=120,   # 파일 객체 → requests가 스트리밍(메모리 절약)
            )
        if up.status_code not in (200, 201):
            print(f"[Supabase 업로드 실패 {up.status_code}] {up.text[:200]}")
            return None
        # public 버킷이므로 직접 URL 반환 (만료는 24h 후 자동삭제로 처리)
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{path}"
    except Exception as e:
        print(f"[Supabase 오류] {e}")
        return None


def _finalize_zip(file_id: str, zip_path: str) -> str:
    """디스크 ZIP을 업로드(Supabase 우선)하고 URL 반환. 실패 시 로컬 폴백 유지."""
    url = _upload_to_supabase(file_id, zip_path)
    if url:
        try:
            os.remove(zip_path)        # 업로드 성공 시 로컬본 제거
        except OSError:
            pass
        return url
    # 폴백: 로컬 임시 파일 유지 + /download (서버 재시작 시 만료될 수 있음)
    expiry_map[file_id] = datetime.now() + timedelta(hours=24)
    timer = threading.Timer(LINK_TTL_SECONDS, lambda: _cleanup(zip_path, file_id))
    timer.daemon = True
    timer.start()
    return f"{SERVER_BASE_URL}/download/{file_id}"


def create_specific_zip(selections: list):
    """selections: [{"product": str, "doc_indices": list[int]}, ...] → (URL, 실제 파일수)"""
    file_id  = str(uuid.uuid4())
    zip_path = os.path.join(TEMP_DIR, f"{file_id}.zip")
    added    = 0
    with _zip_semaphore:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for sel in selections:
                product    = sel.get("product", "")
                doc_indices = sel.get("doc_indices", [])
                docs = DOCUMENT_MAP.get(product, [])
                safe_folder = re.sub(r'[/\\:*?"<>|]', '', product)
                for idx in doc_indices:
                    if idx < 0 or idx >= len(docs):
                        continue
                    doc = docs[idx]
                    try:
                        fetch_url = doc.get("github_url") or doc["url"]
                        resp = requests.get(fetch_url, headers=HEADERS, timeout=20)
                        if resp.status_code != 200:
                            print(f"파일 다운로드 실패 (HTTP {resp.status_code}): {fetch_url}")
                            continue
                        if not resp.content or resp.content.lstrip()[:1] == b"<":
                            print(f"비정상 응답(차단 추정, 건너뜀): {fetch_url}")
                            continue
                        if doc.get("filename"):
                            safe_filename = re.sub(r'[/\\:*?"<>|]', '', doc["filename"])
                        else:
                            cd = resp.headers.get("Content-Disposition", "")
                            real_name = _parse_cd_filename(cd)
                            if real_name:
                                safe_filename = re.sub(r'[/\\:*?"<>|]', '', real_name)
                            else:
                                doc_type = doc.get("type", "파일")
                                safe_filename = f"{doc_type}_{idx + 1}.pdf"
                        zf.writestr(f"{safe_folder}/{safe_filename}", resp.content)
                        added += 1
                    except Exception as e:
                        print(f"파일 다운로드 실패: {doc.get('url', '')} - {e}")
    return _finalize_zip(file_id, zip_path), added


def create_basic_zip(doc_indices: list = None):
    """doc_indices: None or [] = 전체, list[int] = 특정 서류만 → (URL, 실제 파일수)"""
    if doc_indices:
        docs = [COMPANY_DOCS_LIST[i] for i in doc_indices if 0 <= i < len(COMPANY_DOCS_LIST)]
    else:
        docs = COMPANY_DOCS_LIST
    file_id  = str(uuid.uuid4())
    zip_path = os.path.join(TEMP_DIR, f"{file_id}.zip")
    added    = 0
    with _zip_semaphore:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for doc in docs:
                try:
                    resp = requests.get(doc["url"], headers=HEADERS, timeout=20)
                    if resp.status_code == 200:
                        filename = f"{doc['label']}.{doc['ext']}"
                        zf.writestr(filename, resp.content)
                        added += 1
                except Exception as e:
                    print(f"기본서류 다운로드 실패: {doc['url']} - {e}")
    return _finalize_zip(file_id, zip_path), added


# ═══════════════════════════════════════════════════
# 이메일 발송
# ═══════════════════════════════════════════════════

def build_company_docs_html(server_base_url: str) -> str:
    """회사 기본서류 단일 ZIP 다운로드 버튼 HTML"""
    company_docs_zip_url = f"{server_base_url}/download-company-docs"
    return f"""
    <!-- 구분선 -->
    <tr>
      <td style="padding:24px 36px 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="border-top:1px solid #e8ebf0;font-size:0;">&nbsp;</td></tr>
        </table>
      </td>
    </tr>
    <!-- 회사 기본 서류 -->
    <tr>
      <td style="padding:22px 36px 0;text-align:center;">
        <a href="{company_docs_zip_url}"
           style="display:inline-block;background:#ffffff;color:#003389;
                  text-decoration:none;font-size:15px;font-weight:700;
                  padding:14px 36px;border-radius:8px;
                  border:1.5px solid #003389;letter-spacing:0.1px;">
          &#9660;&nbsp; 기본서류 다운로드
        </a>
        <p style="font-size:13px;color:#666;margin:12px 0 0;line-height:1.7;">
          국세/지방세납세증명서 &middot; 사업자등록증 &middot; 공장등록증 외<br>
          <span style="color:#888;">홈페이지 최신본 자동 제공</span>
        </p>
      </td>
    </tr>"""


def _get_gmail_access_token() -> str:
    """Gmail API용 access token 발급 (refresh token 사용)"""
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": GOOGLE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _send_via_gmail_api(to_email: str, raw_msg_bytes: bytes):
    """Gmail REST API로 이메일 발송"""
    access_token = _get_gmail_access_token()
    encoded = base64.urlsafe_b64encode(raw_msg_bytes).decode()
    resp = requests.post(
        f"https://gmail.googleapis.com/gmail/v1/users/{GMAIL_USER}/messages/send",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"raw": encoded},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def send_email(to_email: str, product_names: list[str], download_url: str):
    product_list_html = "".join(
        f"""<tr><td style="padding:5px 0;color:#1a1a1a;font-size:16px;font-weight:500;line-height:1.6;">&#8226;&nbsp; {name}</td></tr>"""
        for name in product_names
    )

    company_docs_html = build_company_docs_html(SERVER_BASE_URL)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#eef1f6;font-family:'Malgun Gothic','Apple SD Gothic Neo',Arial,sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f6;padding:32px 0 48px;">
    <tr>
      <td align="center">

        <table width="680" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:16px;overflow:hidden;
                      box-shadow:0 4px 24px rgba(0,0,0,0.09);">

          <!-- 로고 -->
          <tr>
            <td style="padding:28px 36px 24px;background:#ffffff;">
              <img src="https://ssangkom.co.kr/img/hd_logo_on.png"
                   alt="쌍곰" width="192" height="auto" style="display:block;">
            </td>
          </tr>

          <!-- 헤더 -->
          <tr>
            <td style="background:#003389;padding:24px 36px 26px;">
              <p style="color:#ffffff;font-size:24px;font-weight:700;
                         margin:0;line-height:1.4;letter-spacing:-0.3px;">
                기술자료 이메일 송부
              </p>
            </td>
          </tr>

          <!-- 인사말 -->
          <tr>
            <td style="padding:28px 36px 0;">
              <p style="color:#333;font-size:16px;line-height:1.95;margin:0;">
                안녕하세요.<br>
                요청하신 품목별 기술자료의 다운로드 링크를 아래와 같이 송부해 드립니다.<br>
                아래 버튼을 클릭하시면 파일을 즉시 다운로드하실 수 있습니다.
              </p>
            </td>
          </tr>

          <!-- 품목 카드 -->
          <tr>
            <td style="padding:20px 36px 0;">
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="background:#f4f7fd;border-radius:10px;border-left:3px solid #003389;">
                <tr>
                  <td style="padding:18px 22px 20px;">
                    <p style="font-size:12px;color:#003389;font-weight:700;
                               letter-spacing:1.8px;margin:0 0 12px;text-transform:uppercase;">
                      포함된 품목
                    </p>
                    <table width="100%" cellpadding="0" cellspacing="0">
                      {product_list_html}
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- 메인 CTA -->
          <tr>
            <td style="padding:24px 36px 8px;text-align:center;">
              <a href="{download_url}"
                 style="display:inline-block;background:#003389;color:#ffffff;
                        text-decoration:none;font-size:17px;font-weight:700;
                        padding:17px 52px;border-radius:8px;letter-spacing:0.1px;
                        box-shadow:0 4px 14px rgba(0,51,137,0.28);">
                &#9660;&nbsp; 기술자료 다운로드
              </a>
              <p style="margin:12px 0 0;font-size:13px;color:#666;text-align:center;">
                ※ 링크 유효기간: 발송 후 24시간
              </p>
            </td>
          </tr>

          {company_docs_html}

          <!-- 안내 문구 -->
          <tr>
            <td style="padding:20px 36px 28px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="border-top:1px solid #ebebeb;padding-top:20px;">
                    <p style="color:#666;font-size:13px;line-height:2;margin:0;">
                      ※ 본 메일은 자동 발송 메일로 회신되지 않습니다.<br>
                      ※ 관련 문의사항은 담당 영업사원 또는 기술상담실(080-768-3030)로 연락 바랍니다.
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

        </table>

      </td>
    </tr>
  </table>

</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{COMPANY_NAME} <{GMAIL_USER}>"
    msg["To"] = to_email
    msg["Subject"] = f"[{COMPANY_NAME}] 기술자료 이메일 송부"
    msg.attach(MIMEText(html, "html", "utf-8"))

    if GOOGLE_REFRESH_TOKEN:
        _send_via_gmail_api(to_email, msg.as_bytes())
    else:
        # 로컬 개발용 SMTP fallback
        import smtplib, socket, ssl
        ipv4 = socket.getaddrinfo("smtp.gmail.com", 465, socket.AF_INET)[0][4][0]
        with smtplib.SMTP_SSL(ipv4, 465, timeout=15, context=ssl.create_default_context()) as s:
            s.login(GMAIL_USER, GMAIL_PASSWORD)
            s.sendmail(GMAIL_USER, to_email, msg.as_string())


def _send_mail(to_email: str, subject: str, html: str):
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{COMPANY_NAME} <{GMAIL_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html", "utf-8"))
    if GOOGLE_REFRESH_TOKEN:
        _send_via_gmail_api(to_email, msg.as_bytes())
    else:
        import smtplib, socket, ssl
        ipv4 = socket.getaddrinfo("smtp.gmail.com", 465, socket.AF_INET)[0][4][0]
        with smtplib.SMTP_SSL(ipv4, 465, timeout=15, context=ssl.create_default_context()) as s:
            s.login(GMAIL_USER, GMAIL_PASSWORD)
            s.sendmail(GMAIL_USER, to_email, msg.as_string())


def _email_shell(header_title: str, body_inner: str, download_url: str, btn_label: str,
                 include_basic_btn: bool = False) -> str:
    basic_btn_html = build_company_docs_html(SERVER_BASE_URL) if include_basic_btn else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#eef1f6;font-family:'Malgun Gothic','Apple SD Gothic Neo',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f6;padding:32px 0 48px;">
    <tr><td align="center">
      <table width="680" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.09);">
        <tr><td style="padding:28px 36px 24px;">
          <img src="https://ssangkom.co.kr/img/hd_logo_on.png" alt="SSANGKOM" width="160" height="auto" style="display:block;">
        </td></tr>
        <tr><td style="background:#003389;padding:22px 36px 24px;">
          <p style="color:#fff;font-size:22px;font-weight:700;margin:0;line-height:1.4;letter-spacing:-.3px;">{header_title}</p>
        </td></tr>
        <tr><td style="padding:24px 36px 0;">
          <p style="color:#333;font-size:15px;line-height:1.95;margin:0;">안녕하세요.<br>요청하신 기술자료의 다운로드 링크를 아래와 같이 송부해 드립니다.<br>아래 버튼을 클릭하시면 파일을 즉시 다운로드하실 수 있습니다.</p>
        </td></tr>
        {body_inner}
        <tr><td style="padding:22px 36px 8px;text-align:center;">
          <a href="{download_url}" style="display:inline-block;background:#003389;color:#fff;text-decoration:none;font-size:16px;font-weight:700;padding:16px 48px;border-radius:8px;box-shadow:0 4px 14px rgba(0,51,137,.28);">&#9660;&nbsp; {btn_label}</a>
          <p style="margin:10px 0 0;font-size:12px;color:#888;text-align:center;">※ 링크 유효기간: 발송 후 24시간</p>
        </td></tr>
        {basic_btn_html}
        <tr><td style="padding:18px 36px 28px;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr><td style="border-top:1px solid #ebebeb;padding-top:18px;">
              <p style="color:#888;font-size:12px;line-height:2;margin:0;">※ 본 메일은 자동 발송 메일로 회신되지 않습니다.<br>※ 관련 문의사항은 담당 영업사원 또는 기술상담실(080-768-3030)로 연락 바랍니다.</p>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def send_email_specific(to_email: str, summary: list, download_url: str):
    """summary: [{"product": str, "labels": list[str]}, ...]"""
    product_blocks = ""
    for item in summary:
        doc_rows = "".join(
            f'<tr><td style="padding:2px 0;color:#444;font-size:14px;line-height:1.6;">&#8226; {label}</td></tr>'
            for label in item["labels"]
        )
        product_blocks += f'<p style="font-size:13px;font-weight:700;color:#003389;margin:10px 0 4px;">{item["product"]}</p><table width="100%" cellpadding="0" cellspacing="0">{doc_rows}</table>'
    body = f"""<tr><td style="padding:16px 36px 0;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7fd;border-radius:10px;border-left:3px solid #003389;">
        <tr><td style="padding:16px 20px 18px;">
          <p style="font-size:11px;color:#003389;font-weight:700;letter-spacing:1.8px;margin:0 0 10px;text-transform:uppercase;">포함된 기술자료</p>
          {product_blocks}
        </td></tr>
      </table>
    </td></tr>"""
    html = _email_shell("기술자료 이메일 송부", body, download_url, "기술자료 다운로드", include_basic_btn=True)
    _send_mail(to_email, f"[{COMPANY_NAME}] 기술자료 이메일 송부", html)


def send_email_basic(to_email: str, download_url: str, doc_labels: list = None):
    if doc_labels is None:
        doc_labels = [d["label"] for d in COMPANY_DOCS_LIST]
    doc_rows = "".join(
        f'<tr><td style="padding:4px 0;color:#1a1a1a;font-size:15px;font-weight:500;line-height:1.6;">&#8226;&nbsp; {label}</td></tr>'
        for label in doc_labels
    )
    body = f"""<tr><td style="padding:16px 36px 0;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7fd;border-radius:10px;border-left:3px solid #003389;">
        <tr><td style="padding:16px 20px 18px;">
          <p style="font-size:11px;color:#003389;font-weight:700;letter-spacing:1.8px;margin:0 0 12px;text-transform:uppercase;">포함된 서류</p>
          <table width="100%" cellpadding="0" cellspacing="0">{doc_rows}</table>
        </td></tr>
      </table>
    </td></tr>"""
    html = _email_shell("기본서류 이메일 송부", body, download_url, "기본서류 다운로드")
    _send_mail(to_email, f"[{COMPANY_NAME}] 기본서류 이메일 송부", html)


def _log_send(**fields):
    """발송 기록을 Supabase send_logs에 저장 (service_role 필요). 실패해도 발송엔 무영향."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/send_logs",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"},
            json=fields, timeout=8,
        )
    except Exception as e:
        print(f"[send_logs 기록 실패] {e}")


def _log_pending(mode, email, requester, kakao, ip, summary, requested, payload=None):
    """요청 접수 시점에 '처리중' 기록을 남기고 row id 반환 (완료 시 갱신용)."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return None
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/send_logs",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json", "Prefer": "return=representation"},
            json={"email": email, "requester": requester, "kakao_user_id": kakao, "mode": mode,
                  "summary": summary, "files_requested": requested, "files_actual": 0,
                  "status": "처리중", "client_ip": ip, "payload": payload},
            timeout=8,
        )
        if r.status_code in (200, 201):
            arr = r.json()
            return arr[0]["id"] if arr else None
    except Exception as e:
        print(f"[send_logs pending 실패] {e}")
    return None


def _update_log(log_id, fields):
    if not (SUPABASE_URL and SUPABASE_KEY and log_id):
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/send_logs?id=eq.{log_id}",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"},
            json=fields, timeout=8,
        )
    except Exception as e:
        print(f"[send_logs update 실패] {e}")


def _alert_admin(subject: str, body_text: str):
    """발송 실패/부분발송 시 관리자에게 경고 메일."""
    to = ADMIN_EMAIL
    if not to:
        return
    try:
        safe = body_text.replace("<", "&lt;").replace(">", "&gt;")
        html = (f"<div style='font-family:monospace;font-size:13px;line-height:1.7;"
                f"white-space:pre-wrap;color:#222'>{safe}</div>")
        _send_mail(to, f"[쌍곰봇 경고] {subject}", html)
    except Exception as e:
        print(f"[관리자 알림 실패] {e}")


# ═══════════════════════════════════════════════════
# 카카오 응답 헬퍼
# ═══════════════════════════════════════════════════

def text_response(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]}
    }


def quick_reply_response(text: str, buttons: list[str]) -> dict:
    quick_replies = [
        {"label": b, "action": "message", "messageText": b}
        for b in buttons
    ]
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}],
            "quickReplies": quick_replies
        }
    }


def list_card_response(title: str, items: list[str], message: str) -> dict:
    """품목 목록을 카드 형태로 표시 (최대 5개)"""
    buttons = [
        {"label": name[:14], "action": "message", "messageText": name}
        for name in items[:5]
    ]
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": message}}],
            "quickReplies": buttons
        }
    }


def guide_response() -> dict:
    """기술자료 요청 이용안내 메시지 + 요청 페이지 버튼."""
    text = (
        "📌 기술자료 요청 이용안내\n\n"
        "쌍곰 기술자료(MSDS·시험성적서·인증서 등)를 이메일로 받아보실 수 있습니다.\n\n"
        "▣ 요청 방법\n"
        "① 아래 ‘기술자료 요청하기’ 버튼을 누릅니다.\n"
        "② 화면 상단 3가지 방식 중 선택\n"
        "  • 품목별 전체 — 품목 선택 시 해당 품목의 모든 자료\n"
        "  • 개별서류 직접선택 — 품목에서 특정 서류만 골라서\n"
        "  • 기본서류 선택 — 사업자등록증·납세증명 등 회사 기본서류\n"
        "③ 받으실 이메일을 입력하고 발송 요청\n\n"
        "▣ 유의사항\n"
        "• 다운로드 링크는 발송 후 24시간 동안 유효합니다.\n"
        "• 이메일 주소를 정확히 입력해 주세요(도메인 자동 확인).\n"
        "• ‘품목별 전체’·‘개별서류’ 발송 시 회사 기본서류 다운로드 버튼이 함께 발송됩니다.\n"
        "• 관련 문의: 담당 영업사원 또는 기술상담실(080-768-3030)"
    )
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": text}},
                {"basicCard": {
                    "title": "기술자료 요청",
                    "description": "아래 버튼을 눌러 요청 또는 발송 확인을 진행하세요.",
                    "buttons": [
                        {"action": "webLink", "label": "기술자료 요청하기",
                         "webLinkUrl": f"{SERVER_BASE_URL}/request"},
                        {"action": "webLink", "label": "발송 확인하기",
                         "webLinkUrl": f"{SERVER_BASE_URL}/status"}
                    ]
                }}
            ]
        }
    }


def menu_redirect_response() -> dict:
    """채팅 입력 시 버튼/요청 페이지로 유도."""
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text":
                    "이 채널은 아래 버튼으로 이용해 주세요. 🙂\n\n"
                    "• 기술자료 요청하기 — 기술자료를 이메일로 받기\n"
                    "• 발송 확인하기 — 요청한 자료가 발송됐는지 확인\n\n"
                    "사용 방법이 궁금하시면 ‘이용안내’라고 입력해 주세요."}},
                {"basicCard": {
                    "title": "기술자료 요청 / 발송 확인",
                    "description": "버튼을 눌러 진행하세요.",
                    "buttons": [
                        {"action": "webLink", "label": "기술자료 요청하기",
                         "webLinkUrl": f"{SERVER_BASE_URL}/request"},
                        {"action": "webLink", "label": "발송 확인하기",
                         "webLinkUrl": f"{SERVER_BASE_URL}/status"}
                    ]
                }}
            ]
        }
    }


# ═══════════════════════════════════════════════════
# 웹훅 메인 핸들러
# ═══════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    """버튼/웹폼 전용 채널. 채팅 입력은 이용안내/메뉴로 유도."""
    data = request.json or {}
    try:
        utterance = data["userRequest"]["utterance"].strip()
    except (KeyError, TypeError, AttributeError):
        return jsonify(menu_redirect_response())

    # 이용안내
    if utterance in ["이용안내", "이용 안내", "사용방법", "사용 방법", "사용법", "도움말", "안내"]:
        return jsonify(guide_response())

    # 그 외 모든 채팅 입력 → 버튼/요청 페이지로 유도
    return jsonify(menu_redirect_response())


# ═══════════════════════════════════════════════════
# 회사 기본서류 ZIP 다운로드 (실시간 생성)
# ═══════════════════════════════════════════════════

@app.route("/download-company-docs")
def download_company_docs():
    """회사 기본서류를 홈페이지에서 실시간 수집 후 ZIP으로 반환"""
    docs = fetch_company_docs()
    if not docs:
        return "서류를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.", 503

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            try:
                resp = requests.get(doc["url"], headers=HEADERS, timeout=20)
                if resp.status_code == 200:
                    ext = doc["url"].split(".")[-1].lower()
                    filename = f"{doc['label']}.{ext}"
                    zf.writestr(filename, resp.content)
            except Exception as e:
                print(f"회사서류 다운로드 실패: {doc['url']} - {e}")

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        as_attachment=True,
        download_name="쌍곰_기본서류.zip",
        mimetype="application/zip"
    )


# ═══════════════════════════════════════════════════
# 다운로드 엔드포인트
# ═══════════════════════════════════════════════════

@app.route("/download/<file_id>")
def download_zip(file_id: str):
    zip_path = os.path.join(TEMP_DIR, f"{file_id}.zip")
    expiry = expiry_map.get(file_id)

    if not expiry or datetime.now() > expiry or not os.path.exists(zip_path):
        return """
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family:sans-serif;text-align:center;padding:80px;background:#f8f9fa;">
          <h2 style="color:#666;">링크가 만료되었습니다</h2>
          <p style="color:#999;">유효기간(24시간)이 지난 링크입니다.<br>카카오톡 채널에서 다시 요청해주세요.</p>
        </body>
        </html>
        """, 410

    return send_file(
        zip_path,
        as_attachment=True,
        download_name="기술자료.zip",
        mimetype="application/zip"
    )


# ═══════════════════════════════════════════════════
# 관리자 테스트 발송
# ═══════════════════════════════════════════════════

@app.route("/admin/send-test", methods=["POST"])
def admin_send_test():
    """관리자용: 품목 + 이메일 지정해서 즉시 발송 테스트 (ADMIN_TOKEN 필요)"""
    if not ADMIN_TOKEN or request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    data = request.json or {}
    products = data.get("products", [])
    email    = data.get("email", "")
    if not products or not email:
        return jsonify({"error": "products와 email 필요"}), 400
    valid = [p for p in products if p in DOCUMENT_MAP]
    if not valid:
        return jsonify({"error": "품목을 찾을 수 없음", "products": products}), 404
    try:
        download_url, _ = create_zip(valid)
        send_email(email, valid, download_url)
        return jsonify({"ok": True, "products": valid, "email": email, "download_url": download_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _esc(v) -> str:
    s = str(v if v is not None else "")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@app.route("/admin/logs")
def admin_logs():
    """발송 요청 기록 조회 (ADMIN_TOKEN 필요). 예: /admin/logs?token=XXXX"""
    token = request.args.get("token", "")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return "unauthorized", 403

    rows = []
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/send_logs?select=*&order=created_at.desc&limit=300",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                timeout=10,
            )
            if r.status_code == 200:
                rows = r.json()
            else:
                print(f"[admin/logs 조회 {r.status_code}] {r.text[:150]}")
        except Exception as e:
            print(f"[admin/logs 조회 실패] {e}")

    n_ok   = sum(1 for x in rows if x.get("status") == "success")
    n_part = sum(1 for x in rows if x.get("status") == "partial")
    n_fail = sum(1 for x in rows if x.get("status") == "failed")

    def badge(s):
        c = {"success": "#1a7f37", "partial": "#b7791f", "failed": "#dc2f3a", "처리중": "#888"}.get(s, "#888")
        t = {"success": "성공", "partial": "부분", "failed": "실패"}.get(s, _esc(s) or "-")
        return f'<span style="background:{c};color:#fff;border-radius:6px;padding:2px 8px;font-size:12px;font-weight:700">{t}</span>'

    trs = ""
    for x in rows:
        ts   = _esc(x.get("created_at", ""))[:19].replace("T", " ")
        cnt  = f'{x.get("files_actual", "?")}/{x.get("files_requested", "?")}'
        warn = ' style="color:#dc2f3a;font-weight:700"' if (x.get("status") in ("partial", "failed")) else ""
        link = x.get("download_url") or ""
        link_html = f'<a href="{_esc(link)}" target="_blank">열기</a>' if link.startswith("http") else "-"
        trs += (
            "<tr>"
            f"<td class='nowrap'>{ts}</td>"
            f"<td>{badge(x.get('status'))}</td>"
            f"<td>{_esc(x.get('requester'))}</td>"
            f"<td class='nowrap'>{_esc(x.get('email'))}</td>"
            f"<td>{_esc(x.get('mode'))}</td>"
            f"<td class='sum'>{_esc(x.get('summary'))}</td>"
            f"<td{warn}>{cnt}</td>"
            f"<td>{link_html}</td>"
            f"<td class='nowrap'>{_esc(x.get('client_ip'))}</td>"
            f"<td class='err'>{_esc(x.get('error'))}</td>"
            "</tr>"
        )
    if not rows:
        trs = '<tr><td colspan="10" style="text-align:center;padding:40px;color:#999">기록이 없습니다</td></tr>'

    html = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>쌍곰봇 기술자료 발송 기록</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;font-family:'Malgun Gothic',sans-serif;background:#f0f3f8;color:#222}}
.top{{background:#003389;color:#fff;padding:16px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
.top h1{{font-size:18px;margin:0}}
.stat{{font-size:13px;background:rgba(255,255,255,.15);border-radius:8px;padding:4px 10px}}
.wrap{{padding:14px;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 6px rgba(0,0,0,.06);font-size:13px}}
th,td{{padding:9px 11px;border-bottom:1px solid #eef1f6;text-align:left;vertical-align:top}}
th{{background:#f4f7fd;color:#003389;font-size:12px;letter-spacing:.4px;position:sticky;top:0}}
tr:hover td{{background:#f9fbff}}
.nowrap{{white-space:nowrap}}
.sum{{max-width:320px;font-size:12px;color:#444;word-break:break-all}}
.err{{max-width:200px;font-size:11px;color:#dc2f3a;word-break:break-all}}
a{{color:#003389}}
.refresh{{margin-left:auto;color:#fff;font-size:13px;text-decoration:none;background:rgba(255,255,255,.18);padding:6px 12px;border-radius:8px}}
</style></head><body>
<div class="top">
  <h1>📋 기술자료 발송 기록</h1>
  <span class="stat">총 {len(rows)}</span>
  <span class="stat">성공 {n_ok}</span>
  <span class="stat">부분 {n_part}</span>
  <span class="stat">실패 {n_fail}</span>
  <a class="refresh" href="/admin/logs?token={_esc(token)}">새로고침</a>
</div>
<div class="wrap">
<table>
<thead><tr>
<th>시각(UTC)</th><th>상태</th><th>요청자</th><th>이메일</th><th>모드</th><th>내용</th><th>파일(실/요청)</th><th>링크</th><th>IP</th><th>오류</th>
</tr></thead>
<tbody>{trs}</tbody>
</table>
</div>
</body></html>"""
    return Response(html, mimetype="text/html; charset=utf-8")


# ═══════════════════════════════════════════════════
# 웹뷰 승인서류 요청 페이지
# ═══════════════════════════════════════════════════

@app.route("/request")
def request_page():
    products_json    = json.dumps(PRODUCT_NAMES, ensure_ascii=False)
    basic_docs_json  = json.dumps([d["label"] for d in COMPANY_DOCS_LIST], ensure_ascii=False)
    domain_opts = "".join(f'<option value="{d}">@{d}</option>' for d in COMMON_EMAIL_DOMAINS)

    def email_block(n: int) -> str:
        return (
            f'<input type="email" class="email-input" id="email{n}" placeholder="example@company.com" '
            f'autocomplete="email" oninput="clearEmailStatus({n})" onblur="checkEmail({n})">'
            f'<select class="email-select" id="emailSel{n}" onchange="pickDomain({n})">'
            f'<option value="">도메인 빠른선택</option>{domain_opts}</select>'
            f'<div class="email-status" id="emailStatus{n}"></div>'
        )

    email1, email2, email3 = email_block(1), email_block(2), email_block(3)
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>쌍곰 기술자료 요청</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;background:#f0f3f8;min-height:100vh}}
.header{{background:#003389;padding:20px 20px 18px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center}}
.header img{{height:36px;filter:brightness(0) invert(1)}}
.header span{{color:#fff;font-size:18px;font-weight:700;letter-spacing:-.3px}}
.tabs{{display:flex;background:#fff;border-bottom:2px solid #e0e6f0;position:sticky;top:0;z-index:10}}
.tab{{flex:1;padding:13px 6px;text-align:center;font-size:12px;font-weight:600;color:#888;cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-2px;transition:.2s;line-height:1.35}}
.tab.active{{color:#003389;border-bottom-color:#003389}}
.notice{{background:#f0f4ff;border-left:3px solid #003389;margin:12px 12px 0;border-radius:8px;padding:11px 16px}}
.notice p{{font-size:12px;color:#555;line-height:1.8;margin:0}}
.tab-content{{display:none}}
.tab-content.active{{display:block}}
.section{{background:#fff;margin:12px;border-radius:12px;padding:16px;box-shadow:0 1px 6px rgba(0,0,0,.07)}}
.section-title{{font-size:13px;font-weight:700;color:#003389;letter-spacing:.8px;margin-bottom:10px;text-transform:uppercase}}
.step-label{{font-size:11px;font-weight:700;color:#003389;letter-spacing:1.2px;text-transform:uppercase;margin-bottom:8px}}
.search-wrap{{position:relative}}
.search-wrap input{{width:100%;padding:11px 16px 11px 40px;border:1.5px solid #dde3ef;border-radius:8px;font-size:15px;font-family:inherit;outline:none;transition:.2s}}
.search-wrap input:focus{{border-color:#003389}}
.search-icon{{position:absolute;left:13px;top:50%;transform:translateY(-50%);color:#aaa;font-size:16px}}
.summary-head{{display:flex;align-items:center;gap:8px;margin-bottom:12px}}
.summary-head .label{{font-size:13px;font-weight:700;color:#003389;letter-spacing:.5px}}
.summary-head .spacer{{flex:1}}
.summary-count{{background:#003389;color:#fff;font-size:12px;font-weight:700;border-radius:20px;min-width:22px;height:22px;padding:0 8px;display:inline-flex;align-items:center;justify-content:center}}
.summary-count.zero{{background:#c2cbe0}}
.chips{{display:flex;flex-wrap:wrap;gap:7px;min-height:24px}}
.chips.empty{{color:#9aa5bf;font-size:13px;align-items:center}}
.chip{{background:#003389;color:#fff;border-radius:8px;padding:5px 10px;font-size:12.5px;font-weight:500;display:inline-flex;align-items:center;gap:6px;line-height:1.35;max-width:100%}}
.chip span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.chip button{{background:rgba(255,255,255,.22);border:none;color:#fff;width:16px;height:16px;border-radius:50%;cursor:pointer;font-size:12px;line-height:1;display:flex;align-items:center;justify-content:center;padding:0;flex-shrink:0}}
.chip button:hover{{background:rgba(255,255,255,.4)}}
.product-list{{max-height:280px;overflow-y:auto;border:1.5px solid #dde3ef;border-radius:8px;margin-top:10px}}
.product-item{{display:flex;align-items:center;padding:11px 14px;border-bottom:1px solid #f0f3f8;cursor:pointer;transition:.15s}}
.product-item:last-child{{border-bottom:none}}
.product-item:hover{{background:#f4f7ff}}
.product-item.checked{{background:#eef2fb}}
.product-item.focused{{background:#e6edff;border-left:3px solid #003389}}
.product-item input[type=checkbox]{{width:18px;height:18px;accent-color:#003389;margin-right:12px;flex-shrink:0;cursor:pointer}}
.product-item label{{font-size:15px;color:#1a1a1a;cursor:pointer;line-height:1.4;flex:1}}
.count-badge{{display:inline-block;background:#003389;color:#fff;font-size:11px;font-weight:700;border-radius:10px;padding:1px 7px;margin-left:6px;vertical-align:middle}}
.no-result{{text-align:center;padding:24px;color:#aaa;font-size:14px}}
.email-input{{width:100%;padding:12px 14px;border:1.5px solid #dde3ef;border-radius:8px;font-size:15px;font-family:inherit;outline:none;transition:.2s}}
.email-input:focus{{border-color:#003389}}
.email-select{{width:100%;margin-top:8px;padding:11px 12px;border:1.5px solid #dde3ef;border-radius:8px;font-size:14px;font-family:inherit;background:#fff;color:#444;outline:none;cursor:pointer}}
.email-select:focus{{border-color:#003389}}
.email-status{{font-size:12.5px;margin-top:8px;min-height:17px;font-weight:500}}
.email-status.ok{{color:#1a7f37}}
.email-status.err{{color:#dc2f3a}}
.email-status.checking{{color:#888}}
.submit-btn{{width:100%;padding:15px;background:#003389;color:#fff;border:none;border-radius:10px;font-size:17px;font-weight:700;cursor:pointer;margin-top:4px;font-family:inherit;transition:.2s}}
.submit-btn:active{{background:#002270}}
.submit-btn:disabled{{background:#aaa;cursor:not-allowed}}
.hint{{font-size:12px;color:#999;margin-top:6px;text-align:center}}
.loading{{display:none;text-align:center;padding:16px;color:#003389;font-size:14px}}
.success{{display:none;text-align:center;padding:40px 20px}}
.success .icon{{font-size:56px;margin-bottom:16px}}
.success h2{{color:#003389;font-size:20px;margin-bottom:8px}}
.success p{{color:#666;font-size:14px;line-height:1.7}}
.doc-section-product{{font-size:14px;font-weight:600;color:#1a1a1a;margin-bottom:10px;line-height:1.4;padding:8px 12px;background:#f4f7fd;border-radius:6px}}
.row-between{{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}}
.select-all-btn{{background:none;border:1.5px solid #003389;color:#003389;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}}
.doc-list{{border:1.5px solid #dde3ef;border-radius:8px;max-height:260px;overflow-y:auto}}
.doc-item{{display:flex;align-items:center;padding:11px 14px;border-bottom:1px solid #f0f3f8;cursor:pointer;transition:.15s}}
.doc-item:last-child{{border-bottom:none}}
.doc-item:hover{{background:#f4f7ff}}
.doc-item.checked{{background:#eef2fb}}
.doc-item input[type=checkbox]{{width:18px;height:18px;accent-color:#003389;margin-right:12px;flex-shrink:0;cursor:pointer}}
.doc-item label{{font-size:15px;color:#1a1a1a;line-height:1.4;flex:1}}
.doc-main{{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}}
.doc-name{{font-size:13.5px;color:#1a1a1a;line-height:1.4;word-break:break-all}}
.doc-type-badge{{align-self:flex-start;background:#eef2fb;color:#003389;font-size:10.5px;font-weight:700;border-radius:5px;padding:1px 7px}}
.doc-preview{{flex-shrink:0;margin-left:10px;color:#003389;font-size:12px;font-weight:600;text-decoration:none;border:1px solid #c5d2ee;border-radius:6px;padding:6px 10px;background:#f4f7ff;white-space:nowrap}}
.doc-preview:hover{{background:#e6edff}}
</style>
</head>
<body>

<div class="header">
  <img src="https://ssangkom.co.kr/img/hd_logo_on.png" alt="SSANGKOM">
  <span>기술자료 요청</span>
</div>

<div class="tabs">
  <div class="tab active" id="tab1" onclick="switchTab(1)">품목별<br>전체</div>
  <div class="tab" id="tab2" onclick="switchTab(2)">개별서류<br>직접선택</div>
  <div class="tab" id="tab3" onclick="switchTab(3)">기본서류<br>선택</div>
</div>

<div class="notice">
  <p>&#8226; 기술자료 관련 문의사항은 <strong>기술연구소</strong>로 문의해 주시기 바랍니다.<br>&#8226; 기술자료는 홈페이지에 업로드된 자료를 기반으로 발송됩니다.<br>&#8226; <strong>품목별 전체</strong> / <strong>서류 직접선택</strong> 발송 시 회사 <strong>기본서류 다운로드 버튼</strong>이 기본적으로 이메일에 함께 발송되며, <strong>기술자료 다운로드 링크는 발송 후 24시간 동안만 유효</strong>합니다.</p>
</div>

<!-- ── Tab 1: 품목별 전체 ── -->
<div class="tab-content active" id="content1">
  <div id="main1">
    <div class="section">
      <div class="summary-head">
        <span class="label">선택된 품목</span>
        <span class="summary-count zero" id="count1">0</span>
      </div>
      <div class="chips empty" id="chips1">선택된 품목이 없습니다</div>
    </div>
    <div class="section">
      <div class="section-title">품목 선택</div>
      <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input type="text" id="searchInput1" placeholder="품목명 검색..." oninput="filterProducts1()">
      </div>
      <div class="product-list" id="productList1"></div>
    </div>
    <div class="section">
      <div class="section-title">수신 이메일</div>
      {email1}
    </div>
    <div class="section">
      <button class="submit-btn" id="submitBtn1" onclick="submitAll()">기술자료 발송 요청</button>
      <p class="hint">요청 후 수분 내 이메일로 ZIP 파일이 발송됩니다</p>
      <div class="loading" id="loading1">⏳ 처리 중입니다...</div>
    </div>
  </div>
  <div class="success" id="success1">
    <div class="icon">✅</div><h2>발송 요청 완료!</h2><p id="successMsg1"></p>
  </div>
</div>

<!-- ── Tab 2: 서류 직접선택 ── -->
<div class="tab-content" id="content2">
  <div id="main2">
    <div class="section">
      <div class="summary-head">
        <span class="label">선택된 서류</span>
        <span class="summary-count zero" id="count2">0</span>
      </div>
      <div class="chips empty" id="chips2">선택된 서류가 없습니다</div>
    </div>
    <div class="section">
      <div class="step-label">① 품목 선택</div>
      <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input type="text" id="searchInput2" placeholder="품목명 검색..." oninput="filterProducts2()">
      </div>
      <div class="product-list" id="productList2" style="margin-top:10px"></div>
    </div>
    <div class="section" id="docSection2" style="display:none">
      <div class="step-label">② 서류 선택</div>
      <div class="doc-section-product" id="docSectionLabel2"></div>
      <div class="doc-list" id="docList2"></div>
    </div>
    <div class="section">
      <div class="section-title">수신 이메일</div>
      {email2}
    </div>
    <div class="section">
      <button class="submit-btn" id="submitBtn2" onclick="submitSpecific()">선택 서류 발송 요청</button>
      <p class="hint">선택한 서류만 ZIP으로 발송됩니다</p>
      <div class="loading" id="loading2">⏳ 처리 중입니다...</div>
    </div>
  </div>
  <div class="success" id="success2">
    <div class="icon">✅</div><h2>발송 요청 완료!</h2><p id="successMsg2"></p>
  </div>
</div>

<!-- ── Tab 3: 기본서류 선택 ── -->
<div class="tab-content" id="content3">
  <div id="main3">
    <div class="section">
      <div class="summary-head">
        <span class="label">선택된 서류</span>
        <span class="summary-count zero" id="count3">0</span>
        <span class="spacer"></span>
        <button class="select-all-btn" id="selectAllBtn3" onclick="toggleAllBasic3()">전체 선택</button>
      </div>
      <div class="chips empty" id="chips3">선택된 서류가 없습니다</div>
    </div>
    <div class="section">
      <div class="section-title">서류 목록</div>
      <div class="doc-list" id="basicDocList3" style="margin-top:10px"></div>
    </div>
    <div class="section">
      <div class="section-title">수신 이메일</div>
      {email3}
    </div>
    <div class="section">
      <button class="submit-btn" id="submitBtn3" onclick="submitBasic()">기본서류 발송 요청</button>
      <p class="hint">요청 후 수분 내 이메일로 ZIP 파일이 발송됩니다</p>
      <div class="loading" id="loading3">⏳ 처리 중입니다...</div>
    </div>
  </div>
  <div class="success" id="success3">
    <div class="icon">✅</div><h2>발송 요청 완료!</h2><p id="successMsg3"></p>
  </div>
</div>

<script>
var ALL_PRODUCTS = {products_json};
var BASIC_DOCS   = {basic_docs_json};
var KAKAO_UID    = new URLSearchParams(location.search).get('u') || '';

function switchTab(n) {{
  for (var i = 1; i <= 3; i++) {{
    document.getElementById('tab' + i).className = 'tab' + (i === n ? ' active' : '');
    document.getElementById('content' + i).className = 'tab-content' + (i === n ? ' active' : '');
  }}
}}

// ── 공통: 선택 칩 렌더 ─────────────────────────
function renderChips(chipsId, countId, items, emptyText) {{
  var box = document.getElementById(chipsId);
  var cnt = document.getElementById(countId);
  cnt.textContent = items.length;
  cnt.className = 'summary-count' + (items.length ? '' : ' zero');
  box.innerHTML = '';
  if (!items.length) {{
    box.className = 'chips empty';
    box.textContent = emptyText;
    return;
  }}
  box.className = 'chips';
  items.forEach(function(it) {{
    var chip = document.createElement('span');
    chip.className = 'chip';
    var txt = document.createElement('span');
    txt.textContent = it.text;
    txt.title = it.title || it.text;
    chip.appendChild(txt);
    var btn = document.createElement('button');
    btn.innerHTML = '&times;';
    btn.addEventListener('click', function(e) {{ e.stopPropagation(); it.remove(); }});
    chip.appendChild(btn);
    box.appendChild(chip);
  }});
}}

// ── 공통: 이메일 입력 ─────────────────────────
function getEmail(n) {{
  return document.getElementById('email' + n).value.trim();
}}

function pickDomain(n) {{
  var sel = document.getElementById('emailSel' + n);
  if (!sel.value) return;
  var inp = document.getElementById('email' + n);
  var v = inp.value.trim();
  var local = v.indexOf('@') !== -1 ? v.split('@')[0] : v;
  inp.value = local + '@' + sel.value;
  sel.selectedIndex = 0;   // '도메인 빠른선택'으로 리셋
  checkEmail(n);
  inp.focus();
}}

function clearEmailStatus(n) {{
  var s = document.getElementById('emailStatus' + n);
  s.className = 'email-status';
  s.textContent = '';
}}

function checkEmail(n) {{
  var email = getEmail(n);
  var s = document.getElementById('emailStatus' + n);
  if (!email) {{ s.className = 'email-status'; s.textContent = ''; return; }}
  if (!/^[\\w.+-]+@[\\w.-]+\\.[\\w]{{2,}}$/.test(email)) {{
    s.className = 'email-status err';
    s.textContent = '이메일 형식이 올바르지 않습니다';
    return;
  }}
  s.className = 'email-status checking';
  s.textContent = '도메인 확인 중...';
  fetch('/api/check-email', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{email: email}})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(d) {{
    if (d.valid) {{
      s.className = 'email-status ok';
      s.textContent = '✓ 수신 가능한 도메인입니다';
    }} else {{
      s.className = 'email-status err';
      s.textContent = '✗ ' + (d.reason || '확인할 수 없는 이메일 도메인입니다');
    }}
  }})
  .catch(function() {{ s.className = 'email-status'; s.textContent = ''; }});
}}

// ── Tab 1 ─────────────────────────────────────
var selected1 = [];
var listEl1 = null;

function renderList1(products) {{
  listEl1 = listEl1 || document.getElementById('productList1');
  listEl1.innerHTML = '';
  if (!products.length) {{
    var empty = document.createElement('div');
    empty.className = 'no-result';
    empty.textContent = '검색 결과가 없습니다';
    listEl1.appendChild(empty);
    return;
  }}
  products.forEach(function(p) {{
    var div = document.createElement('div');
    div.className = 'product-item' + (selected1.indexOf(p) !== -1 ? ' checked' : '');
    var chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.checked = selected1.indexOf(p) !== -1;
    var lbl = document.createElement('label');
    lbl.textContent = p;
    div.appendChild(chk);
    div.appendChild(lbl);
    div.addEventListener('click', function() {{ toggleProduct1(p); }});
    listEl1.appendChild(div);
  }});
}}

function filterProducts1() {{
  var q = document.getElementById('searchInput1').value.trim().toLowerCase();
  var filtered = q ? ALL_PRODUCTS.filter(function(p) {{ return p.toLowerCase().indexOf(q) !== -1; }}) : ALL_PRODUCTS;
  renderList1(filtered);
}}

function toggleProduct1(p) {{
  var idx = selected1.indexOf(p);
  if (idx === -1) selected1.push(p);
  else selected1.splice(idx, 1);
  updateSelectedBar1();
  filterProducts1();
}}

function removeProduct1(p) {{
  selected1 = selected1.filter(function(x) {{ return x !== p; }});
  updateSelectedBar1();
  filterProducts1();
}}

function updateSelectedBar1() {{
  var items = selected1.map(function(p) {{
    return {{text: p, remove: function() {{ removeProduct1(p); }}}};
  }});
  renderChips('chips1', 'count1', items, '선택된 품목이 없습니다');
}}

function submitAll() {{
  var email = getEmail(1);
  if (!selected1.length) {{ alert('품목을 1개 이상 선택해주세요.'); return; }}
  if (!email || !/^[\\w.+-]+@[\\w.-]+\\.[\\w]{{2,}}$/.test(email)) {{ alert('올바른 이메일 주소를 입력해주세요.'); return; }}
  document.getElementById('submitBtn1').disabled = true;
  document.getElementById('loading1').style.display = 'block';
  fetch('/api/request', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{mode: 'all', products: selected1, email: email, kakao_user_id: KAKAO_UID}})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(d) {{
    if (d.ok) {{
      document.getElementById('main1').style.display = 'none';
      document.getElementById('success1').style.display = 'block';
      document.getElementById('successMsg1').innerHTML =
        '<b>' + selected1.join(', ') + '</b><br>총 ' + d.file_count + '개 파일<br><br>📧 ' + email + '<br>으로 발송되었습니다.<br><br>잠시 후 이메일을 확인해주세요.';
    }} else {{
      alert('오류: ' + (d.error || ''));
      document.getElementById('submitBtn1').disabled = false;
      document.getElementById('loading1').style.display = 'none';
    }}
  }})
  .catch(function() {{
    alert('서버 오류가 발생했습니다. 잠시 후 다시 시도해주세요.');
    document.getElementById('submitBtn1').disabled = false;
    document.getElementById('loading1').style.display = 'none';
  }});
}}

// ── Tab 2 ─────────────────────────────────────
var selectedItems2  = [];
var focusedProduct2 = null;
var docsCache2      = {{}};

function renderList2(products) {{
  var listEl = document.getElementById('productList2');
  listEl.innerHTML = '';
  if (!products.length) {{
    var empty = document.createElement('div');
    empty.className = 'no-result';
    empty.textContent = '검색 결과가 없습니다';
    listEl.appendChild(empty);
    return;
  }}
  products.forEach(function(p) {{
    var count = selectedItems2.filter(function(x) {{ return x.product === p; }}).length;
    var div = document.createElement('div');
    div.className = 'product-item' + (focusedProduct2 === p ? ' focused' : '');
    var lbl = document.createElement('label');
    lbl.textContent = p;
    div.appendChild(lbl);
    if (count > 0) {{
      var badge = document.createElement('span');
      badge.className = 'count-badge';
      badge.textContent = count;
      div.appendChild(badge);
    }}
    div.addEventListener('click', function() {{ focusProduct2(p); }});
    listEl.appendChild(div);
  }});
}}

function filterProducts2() {{
  var q = document.getElementById('searchInput2').value.trim().toLowerCase();
  var filtered = q ? ALL_PRODUCTS.filter(function(p) {{ return p.toLowerCase().indexOf(q) !== -1; }}) : ALL_PRODUCTS;
  renderList2(filtered);
}}

function focusProduct2(p) {{
  focusedProduct2 = p;
  filterProducts2();
  document.getElementById('docSectionLabel2').textContent = p;
  document.getElementById('docSection2').style.display = 'block';
  if (docsCache2[p]) {{
    renderDocs2(docsCache2[p]);
  }} else {{
    fetch('/api/product-docs', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{product: p}})
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      if (d.ok) {{ docsCache2[p] = d.docs; renderDocs2(d.docs); }}
    }});
  }}
}}

function renderDocs2(docs) {{
  var listEl = document.getElementById('docList2');
  listEl.innerHTML = '';
  docs.forEach(function(doc) {{
    var already = selectedItems2.some(function(x) {{
      return x.product === focusedProduct2 && x.docIndex === doc.index;
    }});
    var div = document.createElement('div');
    div.className = 'doc-item' + (already ? ' checked' : '');

    var chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.checked = already;

    var main = document.createElement('div');
    main.className = 'doc-main';
    var name = document.createElement('span');
    name.className = 'doc-name';
    name.textContent = doc.name || doc.type;
    var badge = document.createElement('span');
    badge.className = 'doc-type-badge';
    badge.textContent = doc.type;
    main.appendChild(name);
    main.appendChild(badge);

    var prev = document.createElement('a');
    prev.className = 'doc-preview';
    prev.textContent = '파일 열어보기';
    prev.target = '_blank';
    prev.rel = 'noopener';
    prev.href = '/preview?product=' + encodeURIComponent(focusedProduct2) + '&index=' + doc.index;
    prev.addEventListener('click', function(e) {{ e.stopPropagation(); }});

    div.appendChild(chk);
    div.appendChild(main);
    div.appendChild(prev);
    (function(d, el, c) {{
      el.addEventListener('click', function() {{
        toggleDoc2(focusedProduct2, d.index, d.type, d.name, el, c);
      }});
    }})(doc, div, chk);
    listEl.appendChild(div);
  }});
}}

function toggleDoc2(product, docIndex, docType, docName, div, chk) {{
  var pos = -1;
  for (var i = 0; i < selectedItems2.length; i++) {{
    if (selectedItems2[i].product === product && selectedItems2[i].docIndex === docIndex) {{
      pos = i; break;
    }}
  }}
  if (pos === -1) {{
    selectedItems2.push({{product: product, docIndex: docIndex, docType: docType, docName: docName}});
    div.classList.add('checked');
    chk.checked = true;
  }} else {{
    selectedItems2.splice(pos, 1);
    div.classList.remove('checked');
    chk.checked = false;
  }}
  updateSelectedBar2();
  filterProducts2();
}}

function removeItem2(product, docIndex) {{
  selectedItems2 = selectedItems2.filter(function(x) {{
    return !(x.product === product && x.docIndex === docIndex);
  }});
  updateSelectedBar2();
  filterProducts2();
  if (focusedProduct2 && docsCache2[focusedProduct2]) renderDocs2(docsCache2[focusedProduct2]);
}}

function updateSelectedBar2() {{
  var items = selectedItems2.map(function(item) {{
    var label = item.docName || item.docType;
    return {{
      text: item.product + ' · ' + label,
      title: item.product + ' · ' + label + ' [' + item.docType + ']',
      remove: function() {{ removeItem2(item.product, item.docIndex); }}
    }};
  }});
  renderChips('chips2', 'count2', items, '선택된 서류가 없습니다');
}}

function submitSpecific() {{
  var email = getEmail(2);
  if (!selectedItems2.length) {{ alert('서류를 1개 이상 선택해주세요.'); return; }}
  if (!email || !/^[\\w.+-]+@[\\w.-]+\\.[\\w]{{2,}}$/.test(email)) {{ alert('올바른 이메일 주소를 입력해주세요.'); return; }}
  var grouped = {{}};
  selectedItems2.forEach(function(item) {{
    if (!grouped[item.product]) grouped[item.product] = [];
    grouped[item.product].push(item.docIndex);
  }});
  var selections = Object.keys(grouped).map(function(p) {{
    return {{product: p, doc_indices: grouped[p]}};
  }});
  document.getElementById('submitBtn2').disabled = true;
  document.getElementById('loading2').style.display = 'block';
  fetch('/api/request', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{mode: 'specific', selections: selections, email: email, kakao_user_id: KAKAO_UID}})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(d) {{
    if (d.ok) {{
      document.getElementById('main2').style.display = 'none';
      document.getElementById('success2').style.display = 'block';
      document.getElementById('successMsg2').innerHTML =
        '선택 서류 ' + d.file_count + '개<br><br>📧 ' + email + '<br>으로 발송되었습니다.<br><br>잠시 후 이메일을 확인해주세요.';
    }} else {{
      alert('오류: ' + (d.error || ''));
      document.getElementById('submitBtn2').disabled = false;
      document.getElementById('loading2').style.display = 'none';
    }}
  }})
  .catch(function() {{
    alert('서버 오류가 발생했습니다. 잠시 후 다시 시도해주세요.');
    document.getElementById('submitBtn2').disabled = false;
    document.getElementById('loading2').style.display = 'none';
  }});
}}

// ── Tab 3 ─────────────────────────────────────
var selectedBasic3 = [];

function initBasic3() {{
  var listEl = document.getElementById('basicDocList3');
  listEl.innerHTML = '';
  BASIC_DOCS.forEach(function(label, i) {{
    var div = document.createElement('div');
    div.className = 'doc-item';
    div.id = 'basicItem3_' + i;
    var chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.checked = false;
    var lbl = document.createElement('label');
    lbl.textContent = label;
    div.appendChild(chk);
    div.appendChild(lbl);
    (function(idx, el, c) {{
      el.addEventListener('click', function() {{ toggleBasic3(idx, el, c); }});
    }})(i, div, chk);
    listEl.appendChild(div);
  }});
}}

function toggleBasic3(idx, div, chk) {{
  var pos = selectedBasic3.indexOf(idx);
  if (pos === -1) {{
    selectedBasic3.push(idx);
    div.classList.add('checked');
    chk.checked = true;
  }} else {{
    selectedBasic3.splice(pos, 1);
    div.classList.remove('checked');
    chk.checked = false;
  }}
  updateSelectedBar3();
  updateSelectAllBtn3();
}}

function toggleAllBasic3() {{
  if (selectedBasic3.length === BASIC_DOCS.length) {{
    selectedBasic3 = [];
    BASIC_DOCS.forEach(function(_, i) {{
      var el = document.getElementById('basicItem3_' + i);
      if (el) {{ el.classList.remove('checked'); el.querySelector('input').checked = false; }}
    }});
  }} else {{
    selectedBasic3 = BASIC_DOCS.map(function(_, i) {{ return i; }});
    BASIC_DOCS.forEach(function(_, i) {{
      var el = document.getElementById('basicItem3_' + i);
      if (el) {{ el.classList.add('checked'); el.querySelector('input').checked = true; }}
    }});
  }}
  updateSelectedBar3();
  updateSelectAllBtn3();
}}

function updateSelectAllBtn3() {{
  var btn = document.getElementById('selectAllBtn3');
  btn.textContent = selectedBasic3.length === BASIC_DOCS.length ? '전체 해제' : '전체 선택';
}}

function removeBasic3(idx) {{
  selectedBasic3 = selectedBasic3.filter(function(x) {{ return x !== idx; }});
  var el = document.getElementById('basicItem3_' + idx);
  if (el) {{ el.classList.remove('checked'); el.querySelector('input').checked = false; }}
  updateSelectedBar3();
  updateSelectAllBtn3();
}}

function updateSelectedBar3() {{
  var items = selectedBasic3.map(function(idx) {{
    return {{text: BASIC_DOCS[idx], remove: function() {{ removeBasic3(idx); }}}};
  }});
  renderChips('chips3', 'count3', items, '선택된 서류가 없습니다');
}}

function submitBasic() {{
  var email = getEmail(3);
  if (!selectedBasic3.length) {{ alert('서류를 1개 이상 선택해주세요.'); return; }}
  if (!email || !/^[\\w.+-]+@[\\w.-]+\\.[\\w]{{2,}}$/.test(email)) {{ alert('올바른 이메일 주소를 입력해주세요.'); return; }}
  document.getElementById('submitBtn3').disabled = true;
  document.getElementById('loading3').style.display = 'block';
  fetch('/api/request', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{mode: 'basic', doc_indices: selectedBasic3, email: email, kakao_user_id: KAKAO_UID}})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(d) {{
    if (d.ok) {{
      document.getElementById('main3').style.display = 'none';
      document.getElementById('success3').style.display = 'block';
      document.getElementById('successMsg3').innerHTML =
        '기본서류 ' + d.file_count + '개<br><br>📧 ' + email + '<br>으로 발송되었습니다.<br><br>잠시 후 이메일을 확인해주세요.';
    }} else {{
      alert('오류: ' + (d.error || ''));
      document.getElementById('submitBtn3').disabled = false;
      document.getElementById('loading3').style.display = 'none';
    }}
  }})
  .catch(function() {{
    alert('서버 오류가 발생했습니다. 잠시 후 다시 시도해주세요.');
    document.getElementById('submitBtn3').disabled = false;
    document.getElementById('loading3').style.display = 'none';
  }});
}}

renderList1(ALL_PRODUCTS);
renderList2(ALL_PRODUCTS);
initBasic3();
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/check-email", methods=["POST"])
def api_check_email():
    """이메일 형식 + 도메인 실존(MX/A) 확인. 발송 전 사전 검증용."""
    data  = request.json or {}
    email = (data.get("email") or "").strip()
    if not re.match(r"^[\w.+-]+@[\w.-]+\.\w{2,}$", email):
        return jsonify({"ok": True, "valid": False, "reason": "형식이 올바르지 않습니다"})
    valid, reason = verify_email_domain(email)
    return jsonify({"ok": True, "valid": valid, "reason": reason})


def _finish_send(log_id, mode, email, requester, kakao, ip, summary, requested, actual, status, url, err=""):
    """발송 완료 시 '처리중' 기록을 결과로 갱신(없으면 신규 기록), 실패/부분이면 관리자 경고."""
    fields = {"status": status, "files_actual": actual, "download_url": url, "error": err}
    if log_id:
        _update_log(log_id, fields)
    else:
        _log_send(email=email, requester=requester, kakao_user_id=kakao, mode=mode,
                  summary=summary, files_requested=requested, files_actual=actual,
                  status=status, download_url=url, client_ip=ip, error=err)
    if status != "success":
        _alert_admin(
            f"{status} · {email}",
            f"상태: {status}\n요청자: {requester or '-'}\n이메일: {email}\n모드: {mode}\n"
            f"내용: {summary}\n요청 {requested}건 / 실제 {actual}건\nIP: {ip}\n"
            f"카톡ID: {kakao or '-'}\n오류: {err or '-'}"
        )


def _dispatch_send(mode, email, requester, kakao, ip, *, products=None, selections=None, doc_indices=None):
    """검증된 입력으로 발송 작업 시작(처리중 로그 + 백그라운드 ZIP/발송). file_count 반환.
    재전송 시에도 동일 경로 사용(payload로 재구성)."""
    if mode == "basic":
        doc_indices = doc_indices or []
        if doc_indices:
            doc_labels = [COMPANY_DOCS_LIST[i]["label"] for i in doc_indices if 0 <= i < len(COMPANY_DOCS_LIST)]
        else:
            doc_labels = [d["label"] for d in COMPANY_DOCS_LIST]
        file_count = len(doc_labels)
        summary = ", ".join(doc_labels)
        payload = {"mode": "basic", "doc_indices": doc_indices}
        log_id = _log_pending("basic", email, requester, kakao, ip, summary, file_count, payload)
        def worker():
            url = ""; actual = 0; status = "success"; err = ""
            try:
                url, actual = create_basic_zip(doc_indices if doc_indices else None)
                send_email_basic(email, url, doc_labels)
                if actual < file_count:
                    status = "partial"
            except Exception as e:
                status = "failed"; err = str(e); print(f"[basic 오류] {e}")
            _finish_send(log_id, "basic", email, requester, kakao, ip, summary, file_count, actual, status, url, err)
        threading.Thread(target=worker, daemon=True).start()
        return file_count

    if mode == "specific":
        selections = selections or []
        summary_list = []
        file_count = 0
        for sel in selections:
            docs = DOCUMENT_MAP.get(sel["product"], [])
            labels = [docs[i].get("type", "파일") for i in sel["doc_indices"] if 0 <= i < len(docs)]
            summary_list.append({"product": sel["product"], "labels": labels})
            file_count += len(sel["doc_indices"])
        summary_text = "; ".join(f"{s['product']}: " + ", ".join(s["labels"]) for s in summary_list)
        payload = {"mode": "specific", "selections": selections}
        log_id = _log_pending("specific", email, requester, kakao, ip, summary_text, file_count, payload)
        def worker():
            url = ""; actual = 0; status = "success"; err = ""
            try:
                url, actual = create_specific_zip(selections)
                send_email_specific(email, summary_list, url)
                if actual < file_count:
                    status = "partial"
            except Exception as e:
                status = "failed"; err = str(e); print(f"[specific 오류] {e}")
            _finish_send(log_id, "specific", email, requester, kakao, ip, summary_text, file_count, actual, status, url, err)
        threading.Thread(target=worker, daemon=True).start()
        return file_count

    # all
    products = products or []
    file_count = sum(len(DOCUMENT_MAP.get(p, [])) for p in products)
    summary = ", ".join(products)
    payload = {"mode": "all", "products": products}
    log_id = _log_pending("all", email, requester, kakao, ip, summary, file_count, payload)
    def worker():
        url = ""; actual = 0; status = "success"; err = ""
        try:
            url, actual = create_zip(products)
            send_email(email, products, url)
            if actual < file_count:
                status = "partial"
        except Exception as e:
            status = "failed"; err = str(e); print(f"[all 오류] {e}")
        _finish_send(log_id, "all", email, requester, kakao, ip, summary, file_count, actual, status, url, err)
    threading.Thread(target=worker, daemon=True).start()
    return file_count


@app.route("/api/request", methods=["POST"])
def api_request():
    if _rate_limited():
        return jsonify({"ok": False, "error": "요청이 너무 많습니다. 잠시 후 다시 시도해주세요."}), 429

    data  = request.json or {}
    mode  = data.get("mode", "all")
    email = data.get("email", "").strip()
    requester = (data.get("requester") or "").strip()[:100]
    kakao     = (data.get("kakao_user_id") or "").strip()[:100]
    client_ip = _client_ip()

    if not email or not re.match(r"^[\w.+-]+@[\w.-]+\.\w{2,}$", email):
        return jsonify({"ok": False, "error": "올바른 이메일 주소를 입력해주세요"}), 400

    ok_domain, reason = verify_email_domain(email)
    if not ok_domain:
        return jsonify({"ok": False, "error": reason or "존재하지 않는 이메일 도메인입니다. 주소를 다시 확인해주세요."}), 400

    if mode == "basic":
        doc_indices = [i for i in data.get("doc_indices", [])
                       if isinstance(i, int) and 0 <= i < len(COMPANY_DOCS_LIST)]
        file_count = _dispatch_send("basic", email, requester, kakao, client_ip, doc_indices=doc_indices)
        return jsonify({"ok": True, "file_count": file_count})

    if mode == "specific":
        raw_selections = data.get("selections", [])
        if not raw_selections:
            return jsonify({"ok": False, "error": "서류를 선택해주세요"}), 400
        selections = []
        for sel in raw_selections:
            product = sel.get("product")
            if not product or product not in DOCUMENT_MAP:
                return jsonify({"ok": False, "error": f'품목을 찾을 수 없습니다: {product or ""}'}), 400
            docs = DOCUMENT_MAP[product]
            idxs = [i for i in sel.get("doc_indices", []) if isinstance(i, int) and 0 <= i < len(docs)]
            if idxs:
                selections.append({"product": product, "doc_indices": idxs})
        if not selections:
            return jsonify({"ok": False, "error": "유효한 서류가 없습니다"}), 400
        file_count = _dispatch_send("specific", email, requester, kakao, client_ip, selections=selections)
        return jsonify({"ok": True, "file_count": file_count})

    # mode == "all"
    products = data.get("products", [])
    if not products:
        return jsonify({"ok": False, "error": "품목을 선택해주세요"}), 400
    valid = [p for p in products if p in DOCUMENT_MAP]
    if not valid:
        return jsonify({"ok": False, "error": "유효한 품목이 없습니다"}), 400
    file_count = _dispatch_send("all", email, requester, kakao, client_ip, products=valid)
    return jsonify({"ok": True, "file_count": file_count})


def _doc_display_name(doc: dict) -> str:
    """서류의 사람이 읽을 수 있는 이름. filename 우선, 없으면 type."""
    fn = doc.get("filename")
    if fn:
        return re.sub(r"\.pdf$", "", fn, flags=re.IGNORECASE).strip()
    return doc.get("type", "파일")


@app.route("/api/product-docs", methods=["POST"])
def api_product_docs():
    data    = request.json or {}
    product = data.get("product", "")
    if not product or product not in DOCUMENT_MAP:
        return jsonify({"ok": False, "error": "품목을 찾을 수 없습니다"}), 404
    docs = DOCUMENT_MAP[product]
    return jsonify({
        "ok": True,
        "product": product,
        "docs": [
            {"index": i, "type": doc.get("type", "파일"), "name": _doc_display_name(doc)}
            for i, doc in enumerate(docs)
        ]
    })


def _sniff_content(content: bytes):
    """매직바이트로 (mimetype, disposition) 결정. PDF/이미지는 inline, 그 외는 download."""
    if content.startswith(b"%PDF"):
        return "application/pdf", "inline"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "inline"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "inline"
    if content[:2] == b"PK":
        return "application/zip", "attachment"
    return "application/octet-stream", "attachment"


@app.route("/preview")
def preview_doc():
    """선택한 서류를 브라우저에서 열어보기 (PDF/이미지는 인라인, ZIP 등은 다운로드)."""
    product = request.args.get("product", "")
    try:
        idx = int(request.args.get("index", "-1"))
    except (TypeError, ValueError):
        idx = -1
    docs = DOCUMENT_MAP.get(product, [])
    if not (0 <= idx < len(docs)):
        return "서류를 찾을 수 없습니다", 404
    doc = docs[idx]
    fetch_url = doc.get("github_url") or doc.get("url")
    if not fetch_url:
        return "파일을 불러올 수 없습니다 (원본 미캐싱)", 404
    try:
        resp = requests.get(fetch_url, headers=HEADERS, timeout=20)
    except Exception:
        return "파일을 불러올 수 없습니다", 502
    # HTML(차단/오류 페이지)·빈 응답 거름
    if resp.status_code != 200 or not resp.content or resp.content.lstrip()[:1] == b"<":
        return "파일을 불러올 수 없습니다 (원본 접근 불가)", 502
    ctype, disp = _sniff_content(resp.content)
    fname = doc.get("filename") or f"{doc.get('type', '파일')}"
    quoted = urllib.parse.quote(fname)
    return Response(
        resp.content,
        mimetype=ctype,
        headers={"Content-Disposition": f"{disp}; filename*=UTF-8''{quoted}"},
    )


def _age_hours(ts: str):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600
    except Exception:
        return None


@app.route("/status/resend", methods=["POST"])
def status_resend():
    """영업사원용: 기록된 수신 이메일로 동일 자료 재발송(원본 수신처로만)."""
    email  = (request.form.get("email") or "").strip()
    log_id = request.form.get("id", "")
    back = f"/status?email={urllib.parse.quote(email)}&resent=1" if email else "/status"
    if _rate_limited() or not (email and log_id.isdigit() and SUPABASE_URL and SUPABASE_KEY):
        return redirect(back)
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/send_logs?select=email,payload&id=eq.{log_id}",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=10,
        )
        rows = r.json() if r.status_code == 200 else []
    except Exception as e:
        print(f"[resend 조회 실패] {e}")
        rows = []
    if not rows or (rows[0].get("email") or "") != email:   # 이메일 일치할 때만 허용
        return redirect(back)
    payload = rows[0].get("payload") or {}
    mode = payload.get("mode")
    if mode in ("all", "specific", "basic"):
        _dispatch_send(mode, email, "재전송", "", _client_ip(),
                       products=payload.get("products"),
                       selections=payload.get("selections"),
                       doc_indices=payload.get("doc_indices"))
    return redirect(back)


@app.route("/status")
def status_page():
    """영업사원용: 수신 이메일로 발송 요청의 처리 현황 확인 + 재전송."""
    email = (request.args.get("email") or "").strip()
    resent = request.args.get("resent") == "1"
    rows = []
    searched = bool(email)
    if email and re.match(r"^[\w.+-]+@[\w.-]+\.\w{2,}$", email) and SUPABASE_URL and SUPABASE_KEY:
        try:
            q = urllib.parse.quote(email)
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/send_logs?select=*&email=eq.{q}&order=created_at.desc&limit=20",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                timeout=10,
            )
            if r.status_code == 200:
                rows = r.json()
        except Exception as e:
            print(f"[status 조회 실패] {e}")

    def sbadge(s):
        m = {"처리중": ("#6b7280", "처리 중"), "success": ("#1a7f37", "발송 완료"),
             "partial": ("#b7791f", "일부 누락"), "failed": ("#dc2f3a", "발송 실패")}
        c, t = m.get(s, ("#888", _esc(s) or "-"))
        return f'<span style="background:{c};color:#fff;border-radius:14px;padding:3px 12px;font-size:13px;font-weight:700">{t}</span>'

    cards = ""
    for x in rows:
        status = x.get("status")
        ts   = _esc(x.get("created_at", ""))[:16].replace("T", " ")
        cnt  = f'{x.get("files_actual", 0)}/{x.get("files_requested", 0)}'
        link = x.get("download_url") or ""
        age  = _age_hours(x.get("created_at", "") or "")
        expired = age is not None and age >= 24
        link_ok = status in ("success", "partial") and link.startswith("http") and not expired

        # 링크 유효성 안내(영업사원 관점)
        if status == "처리중":
            info = '<span class="muted">발송 처리 중입니다…</span>'
        elif link_ok:
            rem = max(0, int(24 - age)) if age is not None else 24
            info = (f'<a class="open" href="{_esc(link)}" target="_blank">내용 직접 확인</a>'
                    f'<span class="valid">· 다운로드 링크 유효 (약 {rem}시간 남음)</span>')
        elif status in ("success", "partial") and expired:
            info = '<span class="exp">다운로드 링크 만료됨 (발송 후 24시간 경과)</span>'
        else:
            info = '<span class="exp">다운로드 링크 없음</span>'

        # 재전송 버튼(완료/실패 건만, 원본 수신처로만)
        resend = ""
        if status in ("success", "partial", "failed") and str(x.get("id", "")).isdigit():
            resend = (
                f'<form method="post" action="/status/resend" '
                f'onsubmit="return confirm(\'{_esc(email)}\\n위 주소로 동일 자료를 다시 발송할까요?\')">'
                f'<input type="hidden" name="id" value="{x.get("id")}">'
                f'<input type="hidden" name="email" value="{_esc(email)}">'
                f'<button type="submit" class="resend">🔄 재전송 요청</button>'
                f'</form>'
            )

        cards += (
            '<div class="card">'
            f'<div class="row1"><span class="ts">{ts}</span>{sbadge(status)}</div>'
            f'<div class="sum">{_esc(x.get("summary"))}</div>'
            f'<div class="files">발송 파일 {cnt}건</div>'
            f'<div class="info">{info}</div>'
            f'<div class="act">{resend}</div>'
            '</div>'
        )
    if searched and not rows:
        cards = ('<div class="empty">해당 이메일로 접수된 <b>발송 요청이 없습니다.</b><br>'
                 '수신 이메일 주소가 정확한지 확인해 주세요.</div>')

    intro = "" if searched else ('<div class="empty"><b>수신 이메일 주소</b>를 입력하면<br>'
                                 '요청이 정상 처리됐는지(발송 완료 여부)와 재전송을 진행할 수 있습니다.</div>')
    banner = ('<div class="banner">✅ 재전송을 접수했습니다. 잠시 후 목록을 새로고침하면 새 발송 건이 표시됩니다.</div>'
              if resent else "")

    html = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>기술자료 발송 처리 현황</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;background:#f0f3f8;min-height:100vh}}
.header{{background:#003389;padding:20px;text-align:center}}
.header img{{height:34px;filter:brightness(0) invert(1)}}
.header div{{color:#fff;font-size:17px;font-weight:700;margin-top:8px}}
.search{{background:#fff;margin:12px;border-radius:12px;padding:16px;box-shadow:0 1px 6px rgba(0,0,0,.07)}}
.search label{{font-size:13px;font-weight:700;color:#003389;display:block;margin-bottom:8px}}
.search form{{display:flex;gap:8px}}
.search input{{flex:1;min-width:0;padding:12px 14px;border:1.5px solid #dde3ef;border-radius:8px;font-size:15px;outline:none;font-family:inherit}}
.search input:focus{{border-color:#003389}}
.search button{{flex-shrink:0;padding:0 18px;background:#003389;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:700;cursor:pointer;font-family:inherit}}
.search .hint{{font-size:12px;color:#999;margin-top:8px}}
.banner{{background:#e6f4ea;color:#1a7f37;margin:0 12px 4px;border-radius:10px;padding:11px 14px;font-size:13px;font-weight:600}}
.list{{padding:4px 12px 24px}}
.card{{background:#fff;border-radius:12px;padding:15px 16px;margin-bottom:10px;box-shadow:0 1px 6px rgba(0,0,0,.06)}}
.row1{{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}}
.ts{{font-size:12px;color:#888}}
.sum{{font-size:14px;color:#1a1a1a;line-height:1.5;word-break:break-all;margin-bottom:8px}}
.files{{font-size:13px;color:#555;font-weight:600;margin-bottom:8px}}
.info{{font-size:12.5px;margin-bottom:12px;line-height:1.6}}
.open{{color:#003389;font-weight:700;text-decoration:none;border-bottom:1px solid #9bb4e6}}
.valid{{color:#888;margin-left:4px}}
.exp{{color:#c0392b;font-weight:600}}
.muted{{color:#aaa}}
.act{{border-top:1px solid #f0f3f8;padding-top:11px}}
.resend{{width:100%;padding:12px;background:#003389;color:#fff;border:none;border-radius:9px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit}}
.resend:active{{background:#002270}}
.empty{{background:#fff;margin:12px;border-radius:12px;padding:28px 20px;text-align:center;color:#777;font-size:14px;line-height:1.8;box-shadow:0 1px 6px rgba(0,0,0,.06)}}
.note{{font-size:12px;color:#999;text-align:center;padding:4px 20px 22px;line-height:1.7}}
</style></head><body>
<div class="header">
  <img src="https://ssangkom.co.kr/img/hd_logo_on.png" alt="SSANGKOM">
  <div>기술자료 발송 처리 현황</div>
</div>
<div class="search">
  <label>수신 이메일 주소</label>
  <form method="get" action="/status">
    <input type="email" name="email" value="{_esc(email)}" placeholder="발송 요청한 이메일" autocomplete="off">
    <button type="submit">조회</button>
  </form>
  <div class="hint">기술자료를 발송 요청하신 수신 이메일 주소를 입력하세요.</div>
</div>
{banner}
<div class="list">{intro}{cards}</div>
<div class="note">※ 다운로드 링크는 발송 후 24시간 동안 유효합니다. 만료 시 ‘재전송’으로 다시 보낼 수 있습니다.<br>※ 문의: 기술상담실(080-768-3030)</div>
</body></html>"""
    return Response(html, mimetype="text/html; charset=utf-8")


# ═══════════════════════════════════════════════════
# 상태 확인 (배포 후 서버 동작 확인용)
# ═══════════════════════════════════════════════════

@app.route("/")
def index():
    return """
    <html><head><meta charset="utf-8">
    <style>body{font-family:sans-serif;text-align:center;padding:80px;background:#f4f6f9;}
    h2{color:#003389;}p{color:#555;}</style></head>
    <body>
      <h2>쌍곰 기술자료 자동발송 서버</h2>
      <p>정상 운영 중입니다.</p>
    </body></html>
    """





@app.route("/guide")
def guide_page():
    request_url = f"{SERVER_BASE_URL}/request"
    basic_docs_html = "".join(
        f'<li><span class="check">✓</span> <span>{doc["label"]}</span></li>'
        for doc in COMPANY_DOCS_LIST
    )
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>쌍곰 서비스 이용안내</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;background:#f0f3f8;min-height:100vh}}
.header{{background:#003389;padding:20px 20px 18px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center}}
.header img{{height:44px;filter:brightness(0) invert(1)}}
.header span{{color:#fff;font-size:18px;font-weight:700;letter-spacing:-.3px}}
.section{{background:#fff;margin:12px;border-radius:12px;padding:20px;box-shadow:0 1px 6px rgba(0,0,0,.07)}}
.badge{{display:inline-block;background:#003389;color:#fff;font-size:11px;font-weight:700;letter-spacing:1px;padding:4px 10px;border-radius:20px;margin-bottom:12px}}
.section h3{{font-size:17px;font-weight:700;color:#1a1a1a;margin-bottom:10px}}
.section p{{font-size:14px;color:#555;line-height:1.75;margin-bottom:10px}}
.step-list{{list-style:none;margin-top:6px}}
.step-list li{{font-size:14px;color:#444;padding:5px 0;line-height:1.6;display:flex;align-items:flex-start;gap:8px}}
.step-num{{background:#eef2fb;color:#003389;font-weight:700;font-size:12px;border-radius:50%;width:20px;height:20px;min-width:20px;display:flex;align-items:center;justify-content:center;margin-top:2px}}
.check{{color:#003389;font-weight:700;margin-top:2px}}
.divider{{margin:14px 0;border:none;border-top:1px solid #f0f3f8}}
.note{{font-size:13px;color:#888;margin-top:0}}
.cta-wrap{{padding:8px 12px 28px}}
.cta-btn{{display:block;width:100%;padding:15px;background:#003389;color:#fff;border:none;border-radius:10px;font-size:17px;font-weight:700;cursor:pointer;text-align:center;text-decoration:none;font-family:inherit}}
</style>
</head>
<body>
<div class="header">
  <img src="https://ssangkom.co.kr/img/hd_logo_on.png" alt="SSANGKOM">
  <span>서비스 이용안내</span>
</div>

<div class="section">
  <span class="badge">기능 1</span>
  <h3>전체 기술자료 요청</h3>
  <p>원하는 품목을 선택하면 해당 품목의 모든 기술자료를 이메일 ZIP 파일로 발송해 드립니다. 여러 품목 동시 선택 가능합니다.</p>
  <ul class="step-list">
    <li><span class="step-num">1</span><span>품목명을 검색하거나 목록에서 선택 (다중 선택 가능)</span></li>
    <li><span class="step-num">2</span><span>수신 이메일 주소 입력</span></li>
    <li><span class="step-num">3</span><span>발송 요청 버튼 클릭 → 수분 내 이메일 수신</span></li>
  </ul>
</div>

<div class="section">
  <span class="badge">기능 2</span>
  <h3>특정 서류만 선택</h3>
  <p>품목의 서류 중 MSDS, 시험성적서 등 원하는 종류만 골라서 받을 수 있습니다.</p>
  <ul class="step-list">
    <li><span class="step-num">1</span><span>품목을 1개 검색·선택</span></li>
    <li><span class="step-num">2</span><span>해당 품목의 서류 목록에서 원하는 종류 체크</span></li>
    <li><span class="step-num">3</span><span>수신 이메일 입력 후 발송 요청</span></li>
  </ul>
</div>

<div class="section">
  <span class="badge">기능 3</span>
  <h3>기본서류 요청</h3>
  <p>회사 기본서류 전체를 이메일로 받을 수 있습니다.</p>
  <ul class="step-list">
    {basic_docs_html}
  </ul>
  <hr class="divider">
  <p class="note">이메일 입력 후 발송 요청하면 수분 내 수신됩니다.</p>
</div>

<div class="cta-wrap">
  <a href="{request_url}" class="cta-btn">&#9660;&nbsp; 서류 요청하러 가기</a>
</div>
</body>
</html>"""
    return Response(html, mimetype="text/html; charset=utf-8")


def _key_role(key: str):
    """JWT의 role만 디코드(값 노출 없음). 'service_role'/'anon' 확인용."""
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except Exception:
        return None


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "products": len(PRODUCT_NAMES),
        "total_files": sum(len(v) for v in DOCUMENT_MAP.values()),
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY),
        "supabase_key_role": _key_role(SUPABASE_KEY or ""),  # service_role 이면 Render env 반영됨
    })


if __name__ == "__main__":
    print(f"서버 시작 - 품목 {len(PRODUCT_NAMES)}개, 파일 {sum(len(v) for v in DOCUMENT_MAP.values())}개 로드됨")
    app.run(host="0.0.0.0", port=5000, debug=False)
