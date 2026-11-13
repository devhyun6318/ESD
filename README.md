# ESD 시술 웹사이트 구현
# AI와 연동되지는 않았고, AI scoring 값이 있다고 가정했을 때 보여지는 웹페이지
# 사용자가 쉽게 입력과 수정이 가능하다
# url : https://senorita-unit-epidermis.ngrok-free.dev/

---

## 로컬 AI 채점 프로그램 (`esd_report.py`)

ESD 시술 영상 하나를 Gemini 로 단계 구간 판단 + 단계별 채점한 뒤, 결과 보고서를 로컬 브라우저로 띄웁니다.
보고서 화면은 `report/report.html` + `report/server.py` 를 그대로 사용합니다 (웹 배포와 별개로 내 컴퓨터에서만 열림).

### 준비 (한 번만)

1. Python 3.10 이상, ffmpeg 설치
   - macOS: `brew install ffmpeg`
   - Ubuntu: `sudo apt install ffmpeg`
2. 코드 받기 + 패키지 설치
   ```bash
   git clone https://github.com/devhyun6318/ESD.git
   cd ESD
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Gemini API 키 넣기 (https://aistudio.google.com/apikey 에서 발급)
   ```bash
   cp .env.example .env
   # .env 를 열어 GEMINI_API_KEY= 뒤에 키를 붙여넣기
   ```
   `.env` 는 `.gitignore` 대상이라 GitHub 에 올라가지 않습니다.

### 실행

```bash
python3 esd_report.py "/경로/2025-07-12 11-39-41.mkv"   # 채점 -> 보고서 열기
python3 esd_report.py                                   # 경로를 물어봄 (터미널에 파일을 끌어다 놓아도 됨)
python3 esd_report.py "/경로/영상.mkv" --no-score       # 채점 없이 이미 만든 보고서만 열기
```

- 끝나면 브라우저에 `http://127.0.0.1:8090/?case=<케이스>` 가 열립니다. Ctrl+C 로 종료.
- 결과: `report/cases/<케이스>.json` (보고서 데이터), `<영상이름>_gemini/` (구간·프레임 분류·채점 원본·단계별 클립)
  - 영상 이름이 `YYYY-MM-DD 이름.mkv` 이면 날짜와 케이스 이름(`이름`)을 거기서 가져옵니다.
- 이미 만든 결과는 다시 쓰므로 같은 영상을 다시 실행하면 API 를 쓰지 않습니다.

| 옵션 | 설명 |
|---|---|
| `--model gemini-3.6-flash` | 사용할 모델 (기본값) |
| `--allow-fallback` | 503 이 이어지면 `gemini-3.5-flash-lite` 로 바꾸는 것을 허용 (기본은 바꾸지 않음) |
| `--force` | 이미 있는 결과를 버리고 처음부터 다시 |
| `--no-score` | 채점 없이 보고서만 띄우기 |
| `--port 8090` | 보고서 서버 포트 (사용 중이면 다음 빈 포트) |

### 참고

- 무료 tier 는 모델별로 하루 20 요청 정도이고, 영상 하나에 약 12 요청을 씁니다. 503(사용량 많음)이 나오면 자동으로 기다렸다가 재시도합니다.
- 영상은 Gemini API 로 전송됩니다. 실제 환자 영상은 기관 규정을 확인한 뒤 사용하세요.
- 단계 판단·채점만 따로 돌리려면 `python3 esd_pipeline.py --help`.
