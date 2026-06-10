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
from flask import Flask, jsonify, request, send_file

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

TEMP_DIR = os.path.join(os.getenv("TMPDIR", "/tmp"), "ssangkom_zips")
os.makedirs(TEMP_DIR, exist_ok=True)

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


def create_zip(product_names: list[str]) -> str:
    """품목들의 파일을 ZIP으로 묶고 임시 다운로드 URL 반환"""
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
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
                    # PDF 서명 확인
                    if not resp.content.startswith(b"%PDF"):
                        print(f"PDF가 아닌 응답 수신 (건너뜀): {fetch_url} - 첫 bytes: {resp.content[:40]}")
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
                except Exception as e:
                    print(f"파일 다운로드 실패: {doc['url']} - {e}")

    file_id = str(uuid.uuid4())
    zip_path = os.path.join(TEMP_DIR, f"{file_id}.zip")

    with open(zip_path, "wb") as f:
        f.write(zip_buffer.getvalue())

    # 24시간 후 자동 삭제
    expiry_map[file_id] = datetime.now() + timedelta(hours=24)
    timer = threading.Timer(
        86400,
        lambda: _cleanup(zip_path, file_id)
    )
    timer.daemon = True
    timer.start()

    return f"{SERVER_BASE_URL}/download/{file_id}"


def _cleanup(path: str, file_id: str):
    if os.path.exists(path):
        os.remove(path)
    expiry_map.pop(file_id, None)


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
                요청하신 품목별 승인서류의 다운로드 링크를 아래와 같이 송부해 드립니다.<br>
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
                &#9660;&nbsp; 승인서류 다운로드
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
                      ※ 문의사항은 담당 영업사원 또는 대표번호로 연락 바랍니다.
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


# ═══════════════════════════════════════════════════
# 웹훅 메인 핸들러
# ═══════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    user_id = data["userRequest"]["user"]["id"]
    utterance = data["userRequest"]["utterance"].strip()

    session = sessions.get(user_id, {"step": "search"})

    # ── 취소 명령 ──────────────────────────────────
    if utterance in ["취소", "처음", "다시", "초기화"]:
        sessions.pop(user_id, None)
        return jsonify(quick_reply_response(
            "처음으로 돌아왔습니다.\n원하시는 품목명을 입력해주세요.",
            ["전체 품목 보기"]
        ))

    # ── 전체 품목 보기 ─────────────────────────────
    if utterance == "전체 품목 보기":
        names = PRODUCT_NAMES[:20]
        text = "품목 목록 (상위 20개)입니다.\n원하시는 품목명을 직접 입력해주세요:\n\n"
        text += "\n".join(f"• {n}" for n in names)
        if len(PRODUCT_NAMES) > 20:
            text += f"\n\n... 외 {len(PRODUCT_NAMES) - 20}개"
        sessions.pop(user_id, None)
        return jsonify(text_response(text))

    # ── Step 1: 품목 검색 ──────────────────────────
    if session["step"] == "search":
        results = search_products(utterance)

        if not results:
            return jsonify(quick_reply_response(
                f"'{utterance}'에 해당하는 품목을 찾지 못했습니다.\n품목명을 다시 입력해주세요.",
                ["전체 품목 보기"]
            ))

        if len(results) == 1:
            # 정확히 하나만 매칭 → 바로 이메일 입력 단계
            product = results[0]
            file_count = len(DOCUMENT_MAP.get(product, []))
            sessions[user_id] = {"step": "email", "products": [product]}
            return jsonify(text_response(
                f"✅ '{product}' 승인서류\n총 {file_count}개 파일\n\n"
                f"발송받으실 이메일 주소를 입력해주세요."
            ))

        # 여러 품목 매칭 → 선택 요청
        sessions[user_id] = {"step": "select", "candidates": results}
        return jsonify(list_card_response(
            "품목 선택",
            results,
            f"'{utterance}' 검색 결과입니다.\n해당하는 품목을 선택해주세요:"
        ))

    # ── Step 2: 품목 선택 (여러 결과 중 선택) ────────
    if session["step"] == "select":
        candidates = session.get("candidates", [])
        selected = None

        for name in candidates:
            if utterance == name or utterance in name:
                selected = name
                break

        if not selected:
            # 재검색
            results = search_products(utterance)
            if results:
                sessions[user_id] = {"step": "select", "candidates": results}
                return jsonify(list_card_response(
                    "품목 선택",
                    results,
                    f"해당하는 품목을 선택해주세요:"
                ))
            sessions.pop(user_id, None)
            return jsonify(quick_reply_response(
                "품목을 찾지 못했습니다. 다시 검색해주세요.",
                ["전체 품목 보기"]
            ))

        file_count = len(DOCUMENT_MAP.get(selected, []))
        sessions[user_id] = {"step": "email", "products": [selected]}
        return jsonify(text_response(
            f"✅ '{selected}' 승인서류\n총 {file_count}개 파일\n\n"
            f"발송받으실 이메일 주소를 입력해주세요."
        ))

    # ── Step 3: 이메일 입력 → ZIP 생성 → 발송 ────────
    if session["step"] == "email":
        email = utterance

        # 이메일 형식 검증
        if not re.match(r"^[\w\.-]+@[\w\.-]+\.\w{2,}$", email):
            return jsonify(text_response(
                "올바른 이메일 주소 형식이 아닙니다.\n예: example@company.com\n\n이메일 주소를 다시 입력해주세요."
            ))

        products = session.get("products", [])
        sessions.pop(user_id, None)

        # ZIP 생성 및 이메일 발송 (백그라운드)
        def process():
            try:
                download_url = create_zip(products)
                send_email(email, products, download_url)
                print(f"[완료] {email} -> {products}")
            except Exception as e:
                print(f"[오류] {e}")

        thread = threading.Thread(target=process, daemon=True)
        thread.start()

        file_count = sum(len(DOCUMENT_MAP.get(p, [])) for p in products)
        product_names = ", ".join(products)
        return jsonify(text_response(
            f"✅ 승인서류 발송을 시작했습니다!\n\n"
            f"📦 품목: {product_names}\n"
            f"📄 파일: 총 {file_count}개\n"
            f"📧 수신: {email}\n\n"
            f"잠시 후 이메일을 확인해주세요.\n(처리 시간: 약 30초~1분)\n\n"
            f"다른 품목 서류가 필요하시면 품목명을 입력해주세요."
        ))

    # 예외 처리
    sessions.pop(user_id, None)
    return jsonify(quick_reply_response(
        "안녕하세요! 품목별 승인서류 자동 발송 서비스입니다.\n원하시는 품목명을 입력해주세요.",
        ["전체 품목 보기"]
    ))


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
        download_name="승인서류.zip",
        mimetype="application/zip"
    )


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



@app.route("/test-smtp-diag")
def test_smtp_diag():
    """TCP 수준 SMTP 연결 진단"""
    import socket, ssl
    result = {}
    # DNS 조회
    try:
        all_addrs = socket.getaddrinfo("smtp.gmail.com", 465)
        result["dns_all"] = [str(a[4]) for a in all_addrs]
        ipv4_addrs = socket.getaddrinfo("smtp.gmail.com", 465, socket.AF_INET)
        result["dns_ipv4"] = [str(a[4]) for a in ipv4_addrs]
    except Exception as e:
        result["dns_error"] = str(e)
    # 직접 TCP 연결 (465)
    for port in [465, 587, 25]:
        key = f"tcp_{port}"
        try:
            ip = socket.getaddrinfo("smtp.gmail.com", port, socket.AF_INET)[0][4][0]
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(6)
            s.connect((ip, port))
            s.close()
            result[key] = f"ok (ip={ip})"
        except Exception as e:
            result[key] = f"{type(e).__name__}: {e}"
    return jsonify(result)


@app.route("/test-email")
def test_email():
    """Gmail API 이메일 발송 테스트"""
    result = {
        "gmail_user": GMAIL_USER,
        "oauth_configured": bool(GOOGLE_REFRESH_TOKEN),
    }
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{COMPANY_NAME} <{GMAIL_USER}>"
        msg["To"] = GMAIL_USER
        msg["Subject"] = "[쌍곰] Render 발송 테스트"
        msg.attach(MIMEText("<p>Render 서버에서 Gmail API로 발송된 테스트 메일입니다.</p>", "html", "utf-8"))
        _send_via_gmail_api(GMAIL_USER, msg.as_bytes())
        result["send"] = "success"
    except Exception as e:
        result["send"] = "error"
        result["error"] = str(e)
    return jsonify(result)


@app.route("/test-dispatch")
def test_dispatch():
    """dispatch URL이 실제 PDF를 반환하는지 진단"""
    test_url = "https://ssangkom.co.kr/description/download_dispatch.php?d=12"
    try:
        resp = requests.get(test_url, headers=HEADERS, timeout=20)
        content_type = resp.headers.get("Content-Type", "")
        cd = resp.headers.get("Content-Disposition", "")
        first_bytes = resp.content[:20].hex()
        is_pdf = resp.content.startswith(b"%PDF")
        real_name = _parse_cd_filename(cd)
        return jsonify({
            "status_code": resp.status_code,
            "content_type": content_type,
            "content_disposition": cd,
            "first_bytes_hex": first_bytes,
            "is_pdf": is_pdf,
            "parsed_filename": real_name,
            "content_length": len(resp.content),
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "products": len(PRODUCT_NAMES),
        "total_files": sum(len(v) for v in DOCUMENT_MAP.values())
    })


if __name__ == "__main__":
    print(f"서버 시작 - 품목 {len(PRODUCT_NAMES)}개, 파일 {sum(len(v) for v in DOCUMENT_MAP.values())}개 로드됨")
    app.run(host="0.0.0.0", port=5000, debug=False)
