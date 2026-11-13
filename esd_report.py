"""ESD 영상 하나 -> Gemini 채점 -> 보고서를 로컬 브라우저로 띄운다.

    1. 영상 경로를 받는다 (인자, 없으면 입력창 — 터미널에 파일을 끌어다 놓아도 된다)
    2. esd_pipeline.py 로 단계 판단 + 채점 (GEMINI_API_KEY 는 .env). 이미 만든 결과는 다시 쓰므로 재실행은 API 를 쓰지 않는다
    3. 결과는 report/cases/<case_id>.json
    4. report/server.py (웹 배포와 같은 서버·같은 report.html) 를 로컬에서 띄우고 ?case=<case_id> 를 연다

usage:
    python3 esd_report.py "/경로/2025-07-12 11-39-41.mkv"
    python3 esd_report.py                       # 경로를 물어본다
    python3 esd_report.py 영상.mkv --no-score    # 채점 없이 이미 있는 결과만 띄우기
    Ctrl+C 로 서버 종료
"""

import argparse
import shlex
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from esd_pipeline import CASES, MODEL, case_info  # noqa: E402

VIDEO_EXT = {".mkv", ".mp4", ".mov", ".avi"}


def ask_video():
    raw = input("ESD 영상 경로 (파일을 끌어다 놓아도 됩니다): ").strip()
    parts = shlex.split(raw) if raw else []  # 끌어다 놓으면 공백이 '\ ' 로 escape 되거나 따옴표로 감싸진다
    return parts[0] if parts else ""


def free_port(start=8090, tries=20):
    for port in range(start, start + tries):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    sys.exit(f"{start}~{start + tries - 1} 포트가 모두 사용 중입니다")


def wait_port(port, proc, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            sys.exit("보고서 서버가 시작하자마자 종료됐습니다")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    sys.exit("보고서 서버가 응답하지 않습니다")


def main():
    ap = argparse.ArgumentParser(description="ESD 영상 -> Gemini 채점 -> 로컬 보고서")
    ap.add_argument("video", nargs="?", help="영상 경로 (없으면 물어본다)")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--allow-fallback", action="store_true", help="오류가 이어지면 다른 모델로 바꾸는 것을 허용")
    ap.add_argument("--force", action="store_true", help="이미 있는 결과를 버리고 처음부터 다시 채점")
    ap.add_argument("--no-score", action="store_true", help="채점하지 않고 이미 있는 보고서만 띄우기")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()

    video = Path(args.video or ask_video()).expanduser().resolve()
    if not video.is_file():
        sys.exit(f"영상 파일이 없습니다: {video}")
    if video.suffix.lower() not in VIDEO_EXT:
        sys.exit(f"영상 파일이 아닙니다 ({', '.join(sorted(VIDEO_EXT))}): {video}")
    _, case_id = case_info(video)
    case_json = CASES / f"{case_id}.json"

    if not args.no_score:
        cmd = [sys.executable, "-u", str(ROOT / "esd_pipeline.py"), str(video), "--model", args.model]
        if not args.allow_fallback:
            cmd.append("--no-fallback")
        if args.force:
            cmd.append("--force")
        print(f"[1/2] Gemini 채점 ({args.model}) — {video.name}", flush=True)
        rc = subprocess.call(cmd, cwd=ROOT)
        if rc != 0:
            print(f"채점이 실패했습니다 (exit {rc}). 위 로그를 확인하세요.", flush=True)
            if not case_json.exists():
                sys.exit(1)
            print("이전에 만든 보고서를 대신 띄웁니다.", flush=True)
    if not case_json.exists():
        sys.exit(f"보고서가 없습니다: {case_json} (--no-score 없이 실행해 채점하세요)")

    port = free_port(args.port)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # kill 로 끝내도 아래 finally 에서 서버를 같이 끈다
    server = subprocess.Popen([sys.executable, "server.py", str(port)], cwd=ROOT / "report")
    try:
        wait_port(port, server)
        url = f"http://127.0.0.1:{port}/?case={quote(case_id)}"
        print(f"[2/2] 보고서: {url}   (Ctrl+C 로 종료)", flush=True)
        webbrowser.open(url)
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
