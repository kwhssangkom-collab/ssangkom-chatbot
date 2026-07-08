"""카카오 '나에게 보내기' 최초 토큰 발급 (로컬 1회 실행).

사전 준비 (developers.kakao.com):
1. 애플리케이션 생성 → [앱 키]의 'REST API 키' 복사
2. [카카오 로그인] 활성화 + Redirect URI에 http://localhost:8899 등록
3. [동의항목]에서 '카카오톡 메시지 전송(talk_message)' 사용 설정

실행: py -3 get_kakao_token.py <REST_API_KEY>
출력된 refresh_token은 Supabase kakao_tokens(id=1)에 저장 (파일 저장·커밋 금지).
"""
import http.server
import json
import sys
import urllib.parse
import urllib.request
import webbrowser

REDIRECT = "http://localhost:8899"


def main():
    if len(sys.argv) < 2:
        sys.exit("사용법: py -3 get_kakao_token.py <REST_API_KEY>")
    key = sys.argv[1]

    auth_url = ("https://kauth.kakao.com/oauth/authorize?response_type=code"
                f"&client_id={key}&redirect_uri={urllib.parse.quote(REDIRECT)}"
                "&scope=talk_message")
    print("브라우저에서 카카오 로그인·동의를 완료하세요...")
    webbrowser.open(auth_url)

    holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            holder["code"] = q.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("발급 완료 — 이 창을 닫고 터미널을 확인하세요.".encode())

        def log_message(self, *a):
            pass

    with http.server.HTTPServer(("localhost", 8899), Handler) as srv:
        while not holder.get("code"):
            srv.handle_request()

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": key,
        "redirect_uri": REDIRECT, "code": holder["code"],
    }).encode()
    with urllib.request.urlopen("https://kauth.kakao.com/oauth/token", body) as r:
        tok = json.load(r)
    if "refresh_token" not in tok:
        sys.exit(f"발급 실패: {tok}")

    print("\n=== 발급 성공 ===")
    print(f"refresh_token: {tok['refresh_token']}")
    print("\n위 refresh_token을 Claude에게 전달하면 Supabase에 등록합니다.")


if __name__ == "__main__":
    main()
