# ESD 시술 보고서 웹사이트

ESD 시술 평가 보고서 웹페이지. 사용자가 화면에서 직접 입력·수정하고 PDF 로 저장할 수 있다.

- URL: https://senorita-unit-epidermis.ngrok-free.dev/
- AI 채점 데모(로컬 실행)는 [`demo/`](demo/README.md) 참고.

## 파일

| 파일 | 역할 |
|---|---|
| `report/report.html` | 보고서 페이지 (화면·입력 폼·PDF 저장 모두 이 파일 하나) |
| `report/sample_case.json` | 기본으로 보여주는 예시 데이터 |
| `report/server.py` | 정적 서버. `/` -> report.html, `/sample_case.json`, `/cases/<id>.json` 만 열고 나머지는 404 |

`?case=<id>` 로 열면 `report/cases/<id>.json` 을 읽어 보여준다 (없으면 예시 데이터).

## 로컬에서 확인

```bash
cd report
python3 server.py          # http://127.0.0.1:8090/
```

## 배포

배포 서버에서 systemd 서비스 두 개가 계속 띄워 둔다.

- `esd-report.service`: `report/` 에서 `python3 server.py 8090`
- `ngrok.service`: 8090 포트를 위 URL 로 공개

수정 반영:

```bash
# 로컬: 수정 후 push
git push origin main

# 배포 서버
cd ~/ESD && git pull
sudo systemctl restart esd-report   # server.py 를 고쳤을 때만 (report.html 은 pull 만 해도 바로 반영)
```
