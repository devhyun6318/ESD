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

`--model` 뒤에 아래 모델 이름을 넣는다. `--force` 를 붙이면 새 모델로 구간 판단부터 다시 한다 (안 붙이면 저장된 구간·점수를 그대로 씀).

```bash
python3 esd_score.py --model gemini-3.6-flash --force    # 기본값, 현재 결과가 가장 좋음
python3 esd_score.py --model gemini-3.7-flash --force
python3 esd_score.py --model gemini-3.8-flash --force    # 503(사용량 많음)이 잦음
python3 esd_score.py --model gemini-4-argon --force
python3 esd_score.py --model gemini-4-carbon --force
python3 esd_score.py --model gemini-4-hydrogen --force
python3 esd_score.py --model gemini-3.1-pro-preview --force
python3 esd_score.py --model gemini-2.5-pro --force
```

- 영상을 보고 판단해야 하므로 영상 입력이 되는 일반 모델만 적었다. `*-lite` 모델은 단계 판단이 부정확해서 뺐다.
- 기본값을 바꾸려면 `esd_pipeline.py` 의 `MODEL = "gemini-3.6-flash"` 를 고친다.
