# demo — 로컬 ESD AI 채점 데모

상위 폴더 코드(`esd_pipeline.py`, `report/`)는 그대로 두고, 이 폴더의 복사본으로 실행한다.
흐름: 단계 나누기 (Gemini 3.6-flash) -> 단계별 클립 저장 -> 단계별 채점 (API) -> 보고서 -> 로컬 브라우저

## 준비 (한 번만)

```bash
git clone https://github.com/devhyun6318/ESD.git && cd ESD
brew install ffmpeg                      # Ubuntu: sudo apt install ffmpeg
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # .env 에 GEMINI_API_KEY 넣기 (demo/.env 가 없으면 상위 .env 를 쓴다)
```

## 실행

1. 영상을 `demo/<영상이름>.mkv` 로 넣는다 (예: `demo/2025-07-12 11-39-41.mkv`).
2. 실행:
   ```bash
   cd demo
   python3 esd_score.py                    # demo/ 안의 영상 사용 (여러 개면 번호로 고르기)
   python3 esd_score.py "/다른/경로/영상.mkv" # 다른 위치의 영상
   python3 esd_score.py --no-score         # 채점 없이 이미 만든 보고서만 열기
   ```
3. 브라우저에 보고서가 열린다 (`http://127.0.0.1:8090/?case=<케이스>`). Ctrl+C 로 종료.

## 결과 위치

```
demo/
├ <영상이름>.mkv
├ trial/
│ ├ <영상이름>_marking.mp4        단계별 클립 (원본 화질)
│ ├ <영상이름>_marking_720p.mp4   Gemini 에 올린 720p/10fps 사본
│ ├ <영상이름>_injection.mp4 ... _precut ... _dissection ...
│ └ <영상이름>_gemini/            phases.json(구간), timestamp.md, boundaries.json, scores.json, report.md
└ report/cases/<케이스>.json      보고서 데이터
```

- 이미 만든 구간·클립·점수는 다시 실행할 때 재사용한다 (API 를 다시 쓰지 않음). 처음부터 다시: `--force`.
- 클립·720p 사본은 지워도 다음 실행 때 다시 만든다.
- 영상, `trial/`, `report/cases/` 는 GitHub 에 올라가지 않는다.
- 무료 tier 는 모델별 하루 약 20 요청, 영상 하나에 약 12 요청 (구간 판단 ~8 + 채점 4).
