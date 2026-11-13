# demo

## 설치

```bash
git clone https://github.com/devhyun6318/ESD.git && cd ESD
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # GEMINI_API_KEY=<키>
```

ffmpeg 필요: `brew install ffmpeg` / `sudo apt install ffmpeg` / `winget install ffmpeg`

## 실행

```bash
cd demo
python3 esd_score.py                       # demo/ 안의 영상 사용 (여러 개면 번호로 고르기)
python3 esd_score.py "/경로/영상.mkv"        # 다른 위치의 영상
```

| 옵션 | 설명 |
|---|---|
| `--model <이름>` | 사용할 모델 (기본 `gemini-3.6-flash`) |
| `--force` | 저장된 구간·클립·점수를 버리고 처음부터 다시 |
| `--no-score` | 채점 없이 이미 만든 보고서만 열기 |
| `--allow-fallback` | 503 이 이어지면 `gemini-3.5-flash-lite` 로 자동 전환 (기본: 전환 없이 멈춤) |
| `--port <번호>` | 보고서 서버 포트 (기본 8090, 사용 중이면 다음 빈 포트) |

Ctrl+C 로 종료.

## 모델 바꾸기

```bash
python3 esd_score.py --model gemini-3.8-flash            # 이번 실행만
python3 esd_score.py --model gemini-3.8-flash --force    # 새 모델로 구간부터 다시
```

- 기본값을 바꾸려면 `esd_pipeline.py` 의 `MODEL = "gemini-3.6-flash"` 를 고친다.
- `--force` 없이 모델만 바꾸면 이미 저장된 구간·점수를 그대로 쓴다.

## 사용 가능한 모델

영상 입력이 되는 텍스트 생성 모델 (2026-11-13 기준, 키로 조회한 목록):

| 모델 | 무료 tier | 비고 |
|---|---|---|
| `gemini-3.6-flash` | O | 기본값, 현재 결과가 가장 좋음 |
| `gemini-3.8-flash` | O | 503(사용량 많음)이 잦음 |
| `gemini-3.5-flash-lite` | O | 가볍지만 단계 판단이 부정확 (Marking 을 놓침) |
| `gemini-4-argon` | X (유료) | |
| `gemini-3.7-flash`, `gemini-3.5-flash` | 미확인 | |
| `gemini-4-carbon`, `gemini-4-hydrogen` | 미확인 | |
| `gemini-flash-latest`, `gemini-pro-latest`, `gemini-flash-lite-latest` | 미확인 | 최신 버전을 가리키는 별칭 |
| `gemini-3.1-pro-preview`, `gemini-3.1-flash-lite`, `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite` | 미확인 | 이전 세대 |

지금 키로 쓸 수 있는 전체 목록:

```bash
cd demo
python3 -c "from google import genai; import esd_pipeline as e; e.load_env(); c = genai.Client(); [print(m.name) for m in c.models.list() if 'generateContent' in (m.supported_actions or [])]"
```

## 무료 tier

- 모델별 하루 약 20 요청. 한도는 오후 5시(KST)에 초기화.
- 영상 하나에 약 12 요청 (단계 구간 판단 ~8 + 단계별 채점 4). 저장된 결과를 재사용하면 다시 쓰지 않는다.
- 하루 한도를 넘으면 `429 ... PerDay` 오류로 멈춘다.
- 503(사용량 많음)은 자동으로 기다렸다가 재시도한다 (최대 5번).
