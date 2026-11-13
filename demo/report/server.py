"""ESD 보고서 정적 서버.

  /                  -> report.html
  /sample_case.json  -> 보고서가 읽는 데이터 (페이지 내부용)
  /cases/<id>.json   -> esd_pipeline.py 가 만든 케이스 (보고서에서 ?case=<id>), /cases/index.json = 목록
  그 외               -> 404 (디렉터리 목록 없음)

사용: python3 server.py [포트]   (기본 8090, 127.0.0.1에만 바인딩 → ngrok으로 공개)
"""
import re
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROUTES = {
    "/": ("report.html", "text/html; charset=utf-8"),
    "/sample_case.json": ("sample_case.json", "application/json; charset=utf-8"),
}


class ReportHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        self._serve(send_body=True)

    def do_HEAD(self):
        self._serve(send_body=False)

    def _serve(self, send_body):
        path = self.path.split("?", 1)[0]
        route = ROUTES.get(path)
        m = re.fullmatch(r"/cases/([\w.-]+)\.json", path)  # 파일 이름만 허용 (../ 등 경로 이동 차단)
        if route is None and m and (ROOT / "cases" / f"{m.group(1)}.json").is_file():
            route = (f"cases/{m.group(1)}.json", "application/json; charset=utf-8")
        if route is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        name, ctype = route
        body = (ROOT / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    server = ThreadingHTTPServer(("127.0.0.1", port), ReportHandler)
    print(f"Serving ESD report on http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
