"""
Gmail API OAuth2 Refresh Token 발급 스크립트 (1회 실행)

사용법:
  1. Google Cloud Console에서 OAuth2 Desktop app 클라이언트 생성
  2. 다운로드한 credentials JSON 파일 경로를 아래에 입력하거나 인수로 전달
  3. python get_gmail_token.py client_secret_XXXX.json
  4. 브라우저에서 Google 로그인 및 권한 허용
  5. 출력된 refresh_token을 Render 환경변수에 등록

Render에 추가해야 할 환경변수:
  GOOGLE_CLIENT_ID      (JSON 파일의 client_id)
  GOOGLE_CLIENT_SECRET  (JSON 파일의 client_secret)
  GOOGLE_REFRESH_TOKEN  (이 스크립트 실행 후 출력되는 값)
"""
import sys
import json
import webbrowser
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

if len(sys.argv) < 2:
    print("사용법: python get_gmail_token.py <client_secret_파일.json>")
    sys.exit(1)

with open(sys.argv[1], encoding="utf-8") as f:
    creds = json.load(f)

# Desktop app 또는 Web app 형식 모두 지원
info = creds.get("installed") or creds.get("web")
CLIENT_ID = info["client_id"]
CLIENT_SECRET = info["client_secret"]
REDIRECT_URI = "http://localhost:8765"
SCOPE = "https://www.googleapis.com/auth/gmail.send"

auth_url = (
    "https://accounts.google.com/o/oauth2/v2/auth?"
    + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })
)

auth_code = None

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Authorization complete. You can close this window.</h2>")

    def log_message(self, *args):
        pass

print(f"\nBrowser opening for Google authorization...")
print(f"URL: {auth_url}\n")
webbrowser.open(auth_url)

server = HTTPServer(("localhost", 8765), Handler)
server.handle_request()

if not auth_code:
    print("Authorization code not received.")
    sys.exit(1)

# Exchange code for tokens
data = urllib.parse.urlencode({
    "code": auth_code,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "redirect_uri": REDIRECT_URI,
    "grant_type": "authorization_code",
}).encode()

req = urllib.request.Request(
    "https://oauth2.googleapis.com/token",
    data=data,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
with urllib.request.urlopen(req) as resp:
    tokens = json.loads(resp.read())

refresh_token = tokens.get("refresh_token")
if not refresh_token:
    print("refresh_token not in response:", tokens)
    sys.exit(1)

print("\n" + "="*60)
print("Render 환경변수에 아래 값들을 추가하세요:")
print("="*60)
print(f"GOOGLE_CLIENT_ID      = {CLIENT_ID}")
print(f"GOOGLE_CLIENT_SECRET  = {CLIENT_SECRET}")
print(f"GOOGLE_REFRESH_TOKEN  = {refresh_token}")
print("="*60 + "\n")
