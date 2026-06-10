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
from flask import Flask, jsonify, request, send_file, Response

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
# 관리자 테스트 발송
# ═══════════════════════════════════════════════════

@app.route("/admin/send-test", methods=["POST"])
def admin_send_test():
    """관리자용: 품목 + 이메일 지정해서 즉시 발송 테스트"""
    data = request.json or {}
    products = data.get("products", [])
    email    = data.get("email", "")
    if not products or not email:
        return jsonify({"error": "products와 email 필요"}), 400
    valid = [p for p in products if p in DOCUMENT_MAP]
    if not valid:
        return jsonify({"error": "품목을 찾을 수 없음", "products": products}), 404
    try:
        download_url = create_zip(valid)
        send_email(email, valid, download_url)
        return jsonify({"ok": True, "products": valid, "email": email, "download_url": download_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════
# 웹뷰 승인서류 요청 페이지
# ═══════════════════════════════════════════════════

@app.route("/request")
def request_page():
    products_json = json.dumps(PRODUCT_NAMES, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>쌍곰 승인서류 요청</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;background:#f0f3f8;min-height:100vh}}
.header{{background:#003389;padding:16px 20px;display:flex;align-items:center;gap:12px}}
.header img{{height:32px}}
.header span{{color:#fff;font-size:17px;font-weight:700}}
.section{{background:#fff;margin:12px;border-radius:12px;padding:16px;box-shadow:0 1px 6px rgba(0,0,0,.07)}}
.section-title{{font-size:13px;font-weight:700;color:#003389;letter-spacing:.8px;margin-bottom:10px;text-transform:uppercase}}
.search-wrap{{position:relative}}
.search-wrap input{{width:100%;padding:11px 16px 11px 40px;border:1.5px solid #dde3ef;border-radius:8px;font-size:15px;font-family:inherit;outline:none;transition:.2s}}
.search-wrap input:focus{{border-color:#003389}}
.search-icon{{position:absolute;left:13px;top:50%;transform:translateY(-50%);color:#aaa;font-size:16px}}
.selected-bar{{background:#eef2fb;border-radius:8px;padding:10px 14px;margin-top:10px;font-size:13px;color:#003389;font-weight:600;min-height:38px;display:flex;align-items:center;flex-wrap:wrap;gap:6px}}
.selected-bar.empty{{color:#aaa;font-weight:400}}
.tag{{background:#003389;color:#fff;border-radius:20px;padding:3px 10px;font-size:12px;display:flex;align-items:center;gap:4px}}
.tag button{{background:none;border:none;color:#fff;cursor:pointer;font-size:14px;line-height:1;padding:0}}
.product-list{{max-height:320px;overflow-y:auto;border:1.5px solid #dde3ef;border-radius:8px;margin-top:10px}}
.product-item{{display:flex;align-items:center;padding:11px 14px;border-bottom:1px solid #f0f3f8;cursor:pointer;transition:.15s}}
.product-item:last-child{{border-bottom:none}}
.product-item:hover{{background:#f4f7ff}}
.product-item.checked{{background:#eef2fb}}
.product-item input[type=checkbox]{{width:18px;height:18px;accent-color:#003389;margin-right:12px;flex-shrink:0;cursor:pointer}}
.product-item label{{font-size:15px;color:#1a1a1a;cursor:pointer;line-height:1.4}}
.no-result{{text-align:center;padding:24px;color:#aaa;font-size:14px}}
.email-input{{width:100%;padding:12px 16px;border:1.5px solid #dde3ef;border-radius:8px;font-size:15px;font-family:inherit;outline:none;transition:.2s}}
.email-input:focus{{border-color:#003389}}
.submit-btn{{width:100%;padding:15px;background:#003389;color:#fff;border:none;border-radius:10px;font-size:17px;font-weight:700;cursor:pointer;margin-top:4px;font-family:inherit;transition:.2s}}
.submit-btn:active{{background:#002270}}
.submit-btn:disabled{{background:#aaa;cursor:not-allowed}}
.hint{{font-size:12px;color:#999;margin-top:6px;text-align:center}}
#success{{display:none;text-align:center;padding:40px 20px}}
#success .icon{{font-size:56px;margin-bottom:16px}}
#success h2{{color:#003389;font-size:20px;margin-bottom:8px}}
#success p{{color:#666;font-size:14px;line-height:1.7}}
.loading{{display:none;text-align:center;padding:16px;color:#003389;font-size:14px}}
</style>
</head>
<body>

<div class="header">
  <img src="https://ssangkom.co.kr/img/hd_logo_on.png" alt="쌍곰">
  <span>승인서류 요청</span>
</div>

<div id="main">
  <!-- 품목 선택 -->
  <div class="section">
    <div class="section-title">품목 선택</div>
    <div class="search-wrap">
      <span class="search-icon">🔍</span>
      <input type="text" id="searchInput" placeholder="품목명 검색..." oninput="filterProducts()">
    </div>
    <div class="selected-bar empty" id="selectedBar">선택된 품목이 없습니다</div>
    <div class="product-list" id="productList"></div>
  </div>

  <!-- 이메일 입력 -->
  <div class="section">
    <div class="section-title">수신 이메일</div>
    <input type="email" class="email-input" id="emailInput" placeholder="example@company.com">
  </div>

  <!-- 발송 버튼 -->
  <div class="section">
    <button class="submit-btn" id="submitBtn" onclick="submitRequest()">승인서류 발송 요청</button>
    <p class="hint">요청 후 수분 내 이메일로 ZIP 파일이 발송됩니다</p>
    <div class="loading" id="loading">⏳ 처리 중입니다...</div>
  </div>
</div>

<div id="success">
  <div class="icon">✅</div>
  <h2>발송 요청 완료!</h2>
  <p id="successMsg"></p>
</div>

<script>
var ALL_PRODUCTS = {products_json};
var selected = [];

function renderList(products) {{
  var list = document.getElementById('productList');
  if (!products.length) {{
    list.innerHTML = '<div class="no-result">검색 결과가 없습니다</div>';
    return;
  }}
  list.innerHTML = products.map(function(p) {{
    var chk = selected.indexOf(p) !== -1;
    return '<div class="product-item' + (chk ? ' checked' : '') + '" onclick="toggleProduct(' + JSON.stringify(p) + ')">'
      + '<input type="checkbox"' + (chk ? ' checked' : '') + ' onclick="event.stopPropagation();toggleProduct(' + JSON.stringify(p) + ')">'
      + '<label>' + p + '</label></div>';
  }}).join('');
}}

function filterProducts() {{
  var q = document.getElementById('searchInput').value.trim().toLowerCase();
  var filtered = q ? ALL_PRODUCTS.filter(function(p) {{ return p.toLowerCase().indexOf(q) !== -1; }}) : ALL_PRODUCTS;
  renderList(filtered);
}}

function toggleProduct(p) {{
  var idx = selected.indexOf(p);
  if (idx === -1) selected.push(p);
  else selected.splice(idx, 1);
  updateSelectedBar();
  filterProducts();
}}

function removeProduct(p) {{
  selected = selected.filter(function(x) {{ return x !== p; }});
  updateSelectedBar();
  filterProducts();
}}

function updateSelectedBar() {{
  var bar = document.getElementById('selectedBar');
  if (!selected.length) {{
    bar.className = 'selected-bar empty';
    bar.innerHTML = '선택된 품목이 없습니다';
    return;
  }}
  bar.className = 'selected-bar';
  bar.innerHTML = selected.map(function(p) {{
    return '<span class="tag">' + p + '<button onclick="removeProduct(' + JSON.stringify(p) + ')">×</button></span>';
  }}).join('');
}}

function submitRequest() {{
  var email = document.getElementById('emailInput').value.trim();
  if (!selected.length) {{ alert('품목을 1개 이상 선택해주세요.'); return; }}
  if (!email || !/^[\\w.-]+@[\\w.-]+\\.\\w{{2,}}$/.test(email)) {{ alert('올바른 이메일 주소를 입력해주세요.'); return; }}

  document.getElementById('submitBtn').disabled = true;
  document.getElementById('loading').style.display = 'block';

  fetch('/api/request', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{products: selected, email: email}})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(d) {{
    if (d.ok) {{
      document.getElementById('main').style.display = 'none';
      document.getElementById('success').style.display = 'block';
      document.getElementById('successMsg').innerHTML =
        '<b>' + selected.join(', ') + '</b><br>총 ' + d.file_count + '개 파일<br><br>📧 ' + email + '<br>으로 발송되었습니다.<br><br>잠시 후 이메일을 확인해주세요.';
    }} else {{
      alert('오류가 발생했습니다: ' + (d.error || ''));
      document.getElementById('submitBtn').disabled = false;
      document.getElementById('loading').style.display = 'none';
    }}
  }})
  .catch(function() {{
    alert('서버 오류가 발생했습니다. 잠시 후 다시 시도해주세요.');
    document.getElementById('submitBtn').disabled = false;
    document.getElementById('loading').style.display = 'none';
  }});
}}

renderList(ALL_PRODUCTS);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/request", methods=["POST"])
def api_request():
    data = request.json or {}
    products = data.get("products", [])
    email    = data.get("email", "").strip()

    if not products:
        return jsonify({"ok": False, "error": "품목을 선택해주세요"}), 400
    if not email or not re.match(r"^[\w\.-]+@[\w\.-]+\.\w{2,}$", email):
        return jsonify({"ok": False, "error": "올바른 이메일 주소를 입력해주세요"}), 400

    valid = [p for p in products if p in DOCUMENT_MAP]
    if not valid:
        return jsonify({"ok": False, "error": "유효한 품목이 없습니다"}), 400

    file_count = sum(len(DOCUMENT_MAP.get(p, [])) for p in valid)

    def process():
        try:
            download_url = create_zip(valid)
            send_email(email, valid, download_url)
        except Exception as e:
            print(f"[api/request 오류] {e}")

    threading.Thread(target=process, daemon=True).start()
    return jsonify({"ok": True, "file_count": file_count})


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
