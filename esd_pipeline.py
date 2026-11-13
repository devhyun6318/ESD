"""ESD 시술 영상 -> 단계 구간 -> 단계별 클립 -> 단계별 채점 -> 보고서 JSON (Gemini API).

    1. 단계 판단: N초마다 프레임 한 장을 뽑아 Gemini 가 프레임별로 단계를 분류
       -> 순서(Marking -> Injection -> Precut -> Dissection) 를 지키도록 DP 로 정리해 구간을 만든다
    2. 자르기: 원본에서 구간별로 stream copy (재인코딩 없음, keyframe 단위라 경계가 1~2초 어긋날 수 있음)
    3. 채점: 단계 클립을 File API 로 올려 세부 항목 3개 (4/3/3점) 를 채점하고 근거를 작성
       (업로드는 720p / 10fps 로 줄인 사본. Gemini 는 기본 1fps 로 보므로 결과 차이는 거의 없고 업로드가 빠르다)
    4. 보고서: report/cases/<case_id>.json (sample_case.json 과 같은 형식) + report/cases/index.json
       + <영상이름>_gemini/ 에 timestamp / 프레임 분류 / 채점 원본 / report.md

usage (GEMINI_API_KEY 는 .env 에서 읽는다):
    python esd_pipeline.py                       # 이 폴더의 *.mkv 전부
    python esd_pipeline.py "2025-07-12 11-39-41.mkv" --steps phases   # 단계 판단만
    python esd_pipeline.py --force               # 이미 있는 결과도 다시 만든다
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "report" / "cases"
TEMPLATE = ROOT / "report" / "sample_case.json"
REPORT_HTML = ROOT / "report" / "report.html"
MODEL = "gemini-3.6-flash"  # 3.8-flash 는 503 이 잦아 기본에서 뺐다 (--model 로 지정 가능)
FALLBACK = ["gemini-3.5-flash-lite"]  # 503 / 시간 초과가 이어지면 바꾼다

# 단계 판단 근거 (시술자 설명에서 추론):
#   - 화면에는 시술 전부터 표시가 있다 (검은 점선 고리 = 병변 주변 원래 표시, 보라색 선 = 절개할 경로). 이것들은 흔적이 아니다.
#   - 기구 종류(색·모양)로는 단계를 못 가른다. 같은 기구가 여러 단계에 쓰인다 -> 기구가 조직에 "무엇을 하는지"로 판단한다.
#   - 동작은 한 장으로 안 보이므로 1초 간격 3장(직전·현재·직후)을 붙여 보여주고, 그 사이 조직 변화로 동작을 판단한다.
#   - 동작이 안 보이는 프레임(기구가 빠짐 등)은 남은 흔적으로 판단한다: 흔적은 한번 생기면 남는다.
PRE_EXISTING = ("The first image (START) shows the field before the procedure. Marks already visible there are "
                "pre-existing and are NOT procedure traces: the black dotted ring around the dark lesion and the purple "
                "line drawn further out. The purple line is the planned incision path.")
PHASES = [  # key, 보고서 이름, 설명 (기구가 조직에 하는 일 + 남는 흔적)
    ("marking", "Marking", "the device tip is pressed onto ONE point and burns it (smoke/glow/charring at a "
     "stationary tip, branding-like), then moves to the next point; each touch leaves a new small burn spot; the "
     "mucosa is NOT opened. The tissue right at the tip may look pushed up or discolored by the pressure - it is still "
     "marking when the tip burns a point"),
    ("injection", "Injection", "a needle is inserted into the mucosa and fluid is pushed under it WITHOUT burning (no "
     "smoke, glow or charring): across the strip a cushion-like bulge GROWS around the needle, beyond the tip "
     "contact point"),
    ("precut", "Precut", "the tip is dragged ALONG the purple line while burning, and the mucosa splits open behind it "
     "(a continuous opened groove with separated edges along the purple line)"),
    ("dissection", "Dissection", "the tip works BENEATH the already cut mucosal edge, inside the opened groove: the "
     "flap is lifted and pale/milky or fibrous submucosa underneath is cut, the raw cavity grows"),
]
OTHER = "other"  # 시술 전/후, 체외, 내시경 삽입 등

# 각 단계가 "이미 시작됐다"는 흔적 (한번 생기면 남는다). 경계 판단은 이 질문 하나로 한다.
MILESTONES = {
    "marking": "Has marking started: is the tip burning a single point right now, or are there fresh small burn "
               "spots that START does not have? (The black dotted ring and the purple line do not count, even when "
               "the camera is closer or farther than in START.)",
    "injection": "Compared with START, is the mucosa raised / swollen like a cushion by injected fluid, or is the tip "
                 "inserted with the tissue bulging around it?",
    "precut": "Has the mucosa been cut OPEN anywhere along the purple line (an opened groove with separated edges, "
              "not just burn spots and not the pre-existing black dotted ring)?",
    "dissection": "Is the tip working BENEATH the cut mucosal edge, or is a space opened under the lifted flap with "
                  "submucosa exposed (beyond just an opened groove on the surface)?",
}
END_MILESTONE = ("Is the resection finished: the lesion fully detached (only the raw resection bed remains, or the "
                 "specimen is being retrieved), or the scope is withdrawn / outside the body?")

# 대략 분류: 3장 묶음에서 동작을 먼저 보고, 동작이 없으면 남은 흔적 중 가장 진행된 것으로 단계를 정한다
ACTIONS = ["none", "point_burn", "injecting", "cutting_along_line", "cutting_beneath", "unclear"]
FEATURES = {  # 이름 -> (형식, 설명)
    "view_clear": ("BOOLEAN", "the target area is visible enough to judge (not washed out, blurred or looking away)"),
    "action": (ACTIONS, "what the device tip is DOING to the tissue across the 3 frames: none (no device or idle), "
               "point_burn (stationary tip burning one point), injecting (tip inserted, tissue swelling), "
               "cutting_along_line (tip moving along the purple line, mucosa opening), cutting_beneath (cutting under "
               "the lifted cut edge)"),
    "new_burn_spots": ("BOOLEAN", "burn spots that were NOT in START are visible"),
    "mucosa_raised": ("BOOLEAN", "mucosa is swollen/raised like a cushion compared with START"),
    "line_cut_open": ("BOOLEAN", "the mucosa is cut open along (part of) the purple line"),
    "space_under_flap": ("BOOLEAN", "a space is opened beneath the cut edge: lifted flap, exposed submucosa, raw cavity"),
    "lesion_detached": ("BOOLEAN", "the lesion is fully detached / only the resection bed remains"),
}
PHASE_RULES = """Decide the phase from the ACTION first:
- point_burn -> marking, injecting -> injection, cutting_along_line -> precut, cutting_beneath -> dissection
If the action is none/unclear, use the MOST ADVANCED trace reached so far (traces stay once made):
- lesion_detached, or outside the body / scope insertion -> other
- space_under_flap -> dissection
- line_cut_open -> precut
- mucosa_raised -> injection
- new_burn_spots -> marking ONLY if a point_burn action was already seen in an earlier sample; burn spots seen
  without any earlier point_burn are probably the pre-existing black dotted ring seen from a different distance -> other
- no new trace yet -> other
To tell marking from injection: any burning at the tip (smoke, glow, charring) means marking; injection has no burning
and the swelling spreads and grows.
Do not decide by device type or color; the same device can mark, cut and dissect. Smoke or a flash alone does not
decide the phase either. Phases go marking -> injection -> precut -> dissection over time (injection may be repeated
later; keep the most advanced phase reached unless the action clearly shows otherwise)."""

RUBRIC = {  # 세부 항목 (영문, 한글, 배점, 관찰 기준)
    "marking": [
        ("Margin Distance", "병변 경계 이격 거리", 4, "dots kept a safe, roughly constant margin (~5 mm) outside the lesion border, judged relative to lesion size"),
        ("Interval Uniformity", "마킹 점 간격 균일성", 3, "spacing between adjacent dots is regular"),
        ("Closed-curve Completeness", "폐곡선 완성도", 3, "dots cover the entire circumference without gaps"),
    ],
    "injection": [
        ("Sufficient Elevation", "융기 충분성", 4, "clear submucosal cushion is formed and maintained in the area about to be cut"),
        ("Uniformity of Elevation", "융기 균일성", 3, "lifting is even along the planned incision line, no localized collapse"),
        ("Injection Accuracy", "주입 정확도", 3, "each injection produces lifting without leakage or abnormal tissue change; repeat injections that are needed are not penalized"),
    ],
    "precut": [
        ("Incision Line Location", "절개선 위치", 4, "cut line follows the plan just outside the marking dots"),
        ("Incision Depth", "절개 깊이", 3, "reaches the submucosa without injuring the muscle layer"),
        ("Continuity & Bleeding Control", "연속성 및 출혈 관리", 3, "planned circumferential incision is completed; bleeding, if any, is handled promptly (pauses for device change are not penalized)"),
    ],
    "dissection": [
        ("Visual Clearance", "시야 확보", 4, "cutting point and dissection plane are visible when cutting (no blind dissection)"),
        ("Depth Consistency", "박리면 깊이 일관성", 3, "appropriate submucosal plane is kept and the muscle layer is avoided"),
        ("Complication Management", "합병증 관리", 3, "bleeding/exposed vessels are prevented or treated; clean ulcer bed"),
    ],
}


def load_env():
    """키는 이 폴더의 .env 에만 둔다 (.gitignore 대상, GitHub 에 올리지 않음). 셸에 남은 예전 키보다 우선."""
    env = ROOT / ".env"
    if env.exists():
        for ln in env.read_text().splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                os.environ[k.strip()] = v.strip()


def report_rules():
    """report.html 의 채점 규칙을 그대로 읽는다 (등급 구간, 결과 지표별 선택지)."""
    html = REPORT_HTML.read_text()
    bands = json.loads(re.search(r"var bands = (\[\[.*?\]\]);", html).group(1))
    options = json.loads(re.search(r"var OUTCOME_OPTIONS = (\[.*?\]);", html, re.S).group(1))
    return bands, options


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def mmss(sec):
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out)


def generate(client, contents, schema, retries=6):
    """JSON 응답. 429/5xx 는 기다렸다가 다시 (402 크레딧 소진 같은 나머지 4xx 는 바로 실패)."""
    global MODEL
    cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0)
    for i in range(retries):
        try:
            return json.loads(client.models.generate_content(model=MODEL, contents=contents, config=cfg).text)
        except Exception as e:  # noqa: BLE001  (SDK 예외 종류가 많다)
            code = getattr(e, "code", None)
            if i == retries - 1 or (isinstance(code, int) and 400 <= code < 500 and code != 429):
                raise
            if code == 429 and "PerDay" in str(e):  # 하루 한도: 기다려도 소용없음 -> 다른 모델로, 없으면 실패
                if not FALLBACK:
                    raise
                MODEL = FALLBACK.pop(0)
                log(f"  {code} 하루 한도 -> {MODEL} 로 바꿉니다")
                continue
            if (code == 503 or code is None) and i % 2 == 1 and FALLBACK:  # None: 시간 초과 등
                MODEL = FALLBACK.pop(0)
                log(f"  503 이 이어져 {MODEL} 로 바꿉니다")
            wait = 30 * (i + 1)
            log(f"  재시도 {i + 1}/{retries - 1} ({wait}s 후): {str(e)[:150]}")
            time.sleep(wait)


# ---------------------------------------------------------------- 1. 단계 판단

def extract_frames(video, interval, out_dir, width=320):
    """interval 초마다 1초 간격 3장(직전·현재·직후)을 가로로 붙인 이미지 -> 기구의 동작이 보이도록.
    + 시술 전 기준 화면(START). 왼쪽 300px 은 환자 정보 글자 영역 (내시경 화면은 그 오른쪽) -> 잘라낸다."""
    from PIL import Image
    raw = out_dir / "raw"
    raw.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps=1,crop=iw-300:ih:300:0,scale={width}:-2",
                    "-q:v", "4", str(raw / "f_%05d.jpg")], check=True)
    secs = sorted(raw.glob("f_*.jpg"))  # secs[k] = k+0.5 초
    frames = []
    for i in range(int(len(secs) // interval)):
        k = min(len(secs) - 2, max(1, int(interval * (i + 0.5))))
        ims = [Image.open(secs[j]) for j in (k - 1, k, k + 1)]
        strip = Image.new("RGB", (sum(im.width for im in ims) + 8, ims[0].height), "white")
        x = 0
        for im in ims:
            strip.paste(im, (x, 0))
            x += im.width + 4
        dst = out_dir / f"s_{i:05d}.jpg"
        strip.save(dst, quality=85)
        frames.append((k + 0.5, dst))
    return frames, start_frame(video, out_dir)


def start_frame(video, out_dir, t=5.0):
    """시술 전 기준 화면 (영상 시작 직후)."""
    dst = Path(out_dir) / "start.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                    "-vf", "crop=iw-300:ih:300:0,scale=640:-2", "-q:v", "4", str(dst)], check=True)
    return dst


def classify_frames(client, frames, start, batch):
    keys = [k for k, _, _ in PHASES] + [OTHER]
    desc = "\n".join(f"- {k}: {d}" for k, _, d in PHASES)
    feats = "\n".join(f"- {n}: {d}" for n, (_, d) in FEATURES.items())
    props = {"t": {"type": "STRING"}}
    for n, (kind, _) in FEATURES.items():
        props[n] = {"type": "STRING", "enum": kind} if isinstance(kind, list) else {"type": kind}
    props["phase"] = {"type": "STRING", "enum": keys}  # 특징 다음에 오도록 마지막
    schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": props,
                                         "required": list(props), "propertyOrdering": list(props)}}
    labels, details = {}, {}
    for s in range(0, len(frames), batch):
        chunk = frames[s:s + batch]
        parts = [f"These are samples from an endoscopic submucosal dissection (ESD) video, in time order. Each "
                 f"sample is a strip of 3 frames 1 s apart (previous | current | next), so you can see what the "
                 f"device is doing to the tissue.\n{PRE_EXISTING}\n\n"
                 f"For EACH sample, first check:\n{feats}\n\n"
                 f"Then classify the ESD phase. Phases:\n{desc}\n- {OTHER}: before marking starts, after the resection is finished, or outside the body\n\n{PHASE_RULES}\n"
                 f"Return one item per sample with its time label t exactly as given.",
                 "START", types.Part.from_bytes(data=start.read_bytes(), mime_type="image/jpeg")]
        for t, p in chunk:
            parts += [f"t={mmss(t)}", types.Part.from_bytes(data=p.read_bytes(), mime_type="image/jpeg")]
        out = generate(client, parts, schema)
        got = {o["t"]: o for o in out}
        for t, _ in chunk:
            o = got.get(mmss(t), {})
            labels[t], details[t] = o.get("phase", OTHER), o
        log(f"  프레임 {s + len(chunk)}/{len(frames)} 분류 ({MODEL})")
    return labels, details


def segment(times, labels, other_cost=0.5):
    """프레임 라벨 -> 단계 구간. 상태는 pre(0) -> 4단계(1..4) -> post(5) 로만 진행.
    단계 라벨이 상태와 다르면 1, 단계 상태 안의 other 는 other_cost. 상태 안에서 그 단계 라벨의 첫~끝 프레임이 구간."""
    keys = [k for k, _, _ in PHASES]
    ns, inf = len(keys) + 2, float("inf")

    def cost(lab, s):
        if lab == OTHER:
            return other_cost if 1 <= s <= len(keys) else 0.0
        return 0.0 if s >= 1 and s <= len(keys) and keys[s - 1] == lab else 1.0

    dp = [[inf] * ns for _ in times]
    bp = [[0] * ns for _ in times]
    for s in range(ns):
        dp[0][s] = cost(labels[times[0]], s) if s <= 1 else inf
    for i in range(1, len(times)):
        for s in range(ns):
            prev = min(range(s + 1), key=lambda q: dp[i - 1][q])
            dp[i][s], bp[i][s] = dp[i - 1][prev] + cost(labels[times[i]], s), prev
    s = min(range(ns), key=lambda q: dp[-1][q])
    states = [0] * len(times)
    for i in range(len(times) - 1, -1, -1):
        states[i] = s
        s = bp[i][s]
    step = times[1] - times[0] if len(times) > 1 else 0
    segs = {}
    for j, k in enumerate(keys, start=1):
        hit = [t for t, st in zip(times, states) if st == j and labels[t] == k]
        if hit:  # 프레임 시각 = 구간 가운데 -> 앞뒤로 반 칸씩
            segs[k] = (max(0.0, hit[0] - step / 2), hit[-1] + step / 2)
    return segs


# 시술 구간(첫 Marking 시작 ~ Dissection 끝) 안에서 각 단계가 차지하는 비율 (단계 사이 빈틈은 앞 단계에 포함).
# timestamp.md 의 수작업 기록 2건 평균: 11-39-41 = .075/.180/.203/.541, 11-18-39 = .089/.114/.148/.649
# (영상 전체 길이 기준은 시술 전후 화면 길이가 영상마다 달라 차이가 크다 -> 시술 구간 기준으로 쓴다)
PHASE_PRIOR = {"marking": 0.08, "injection": 0.15, "precut": 0.18, "dissection": 0.59}
PRIOR_SIGMA = 0.05  # 비율이 이만큼 벗어나면 prior_weight 만큼 벌점
ACTION_OF = {"point_burn": "marking", "injecting": "injection", "cutting_along_line": "precut",
             "cutting_beneath": "dissection"}


def segment_prior(times, labels, details, prior_weight=2.0, other_cost=0.5, trace_weight=0.5, search=8):
    """segment() + 단계 비율 사전정보. 구간은 연속 (pre | 4단계 | post), 경계 5개를 고른다.
    비용 = 프레임 라벨과 다른 칸 수 (동작이 보인 프레임 1, 흔적만으로 정한 프레임 trace_weight)
         + prior_weight * sum(((단계 길이 / 시술 구간 길이) - PHASE_PRIOR) / PRIOR_SIGMA)^2
    처음·끝 경계는 segment() 결과 주변 ±search 프레임에서 찾고, 가운데 경계 3개는 DP 로 정한다."""
    keys = [k for k, _, _ in PHASES]
    n = len(times)
    w = [1.0 if ACTION_OF.get(details[t].get("action")) == labels[t] else trace_weight for t in times]
    lab = [labels[t] for t in times]
    # miss[j][i] = 프레임 i 를 상태 j (0 pre, 1..4 단계, 5 post) 로 둘 때 비용
    cum = []  # cum[j][b] - cum[j][a] = 프레임 a..b-1 을 상태 j (0 pre, 1..4 단계, 5 post) 로 둘 때 비용
    for j in range(len(keys) + 2):
        c = [0.0]
        for i, l in enumerate(lab):
            if j in (0, len(keys) + 1):
                m = 0.0 if l == OTHER else w[i]
            else:
                m = other_cost * w[i] if l == OTHER else (0.0 if l == keys[j - 1] else w[i])
            c.append(c[-1] + m)
        cum.append(c)

    def inner(i0, i4):
        """[i0, i4) 를 4단계로 나누는 최소 비용과 경계"""
        span = i4 - i0
        f = {i0: 0.0}
        back = []
        for j, k in enumerate(keys, start=1):
            g, bk = {}, {}
            ends = range(i0 + 1, i4 + 1) if j < len(keys) else [i4]
            for e in ends:
                best = None
                for st, v in f.items():
                    if st >= e:
                        continue
                    c = v + cum[j][e] - cum[j][st] + prior_weight * (((e - st) / span - PHASE_PRIOR[k]) / PRIOR_SIGMA) ** 2
                    if best is None or c < best[0]:
                        best = (c, st)
                if best:
                    g[e], bk[e] = best
            f = g
            back.append(bk)
        cuts, e = [i4], i4
        for bk in reversed(back):
            e = bk[e]
            cuts.append(e)
        return f[i4], cuts[::-1]

    init = segment(times, labels, other_cost)
    step = times[1] - times[0] if n > 1 else 0
    first = min(i for i, t in enumerate(times) if t >= init[keys[0]][0]) if keys[0] in init else 0
    last = max(i for i, t in enumerate(times) if t <= init[keys[-1]][1]) + 1 if keys[-1] in init else n
    best = None
    for i0 in range(max(0, first - search), min(n - 4, first + search) + 1):
        for i4 in range(max(i0 + 4, last - search), min(n, last + search) + 1):
            c, cuts = inner(i0, i4)
            c += cum[0][i0] + cum[-1][n] - cum[-1][i4]
            if best is None or c < best[0]:
                best = (c, cuts)
    cuts = best[1]
    edge = [times[i] - step / 2 if i < n else times[-1] + step / 2 for i in cuts]
    return {k: (max(0.0, edge[j]), edge[j + 1]) for j, k in enumerate(keys)}


def refine_boundary(client, video, t0, before, after, half, step, tmp, start):
    """경계 근처 (t0 ± half) 를 step 초 간격으로 다시 보고, 뒤 단계의 흔적이 이미 생겼는지 (yes/no/unclear) 묻는다.
    -> unclear 는 빼고, 틀린 프레임 수가 가장 적은 지점 하나를 경계로 (흔적은 한번 생기면 남으므로 한 번만 바뀐다)."""
    a = max(0.0, t0 - half)
    d = Path(tmp) / f"b_{int(t0)}"
    d.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{a:.2f}", "-i", str(video), "-t", f"{2 * half:.2f}",
                    "-vf", f"fps=1/{step},crop=iw-300:ih:300:0,scale=640:-2", "-q:v", "4", str(d / "f_%04d.jpg")],
                   check=True)
    frames = [(a + step * (i + 0.5), p) for i, p in enumerate(sorted(d.glob("f_*.jpg")))]
    question = END_MILESTONE if after == OTHER else MILESTONES[after]
    desc = {k: dsc for k, _, dsc in PHASES}
    schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "frame": {"type": "INTEGER"}, "answer": {"type": "STRING", "enum": ["yes", "no", "unclear"]}},
        "required": ["frame", "answer"]}}
    ctx = "\n".join(f"- {k}: {desc[k]}" for k in (before, after) if k in desc)
    parts = [f"These are consecutive frames ({step:g} s apart) from an endoscopic submucosal dissection (ESD) video, "
             f"around the transition from '{before}' to '{after}'.\n{PRE_EXISTING}\n{ctx}\n\n"
             f"For EACH frame answer: {question}\n"
             f"Judge by what the device does to the tissue and the trace it leaves, not by the device type "
             f"(smoke/flash happen in several phases). Answer 'unclear' only if the area cannot be seen "
             f"(washed out, blurred, looking elsewhere).",
             "START", types.Part.from_bytes(data=start.read_bytes(), mime_type="image/jpeg")]
    for i, (_, pth) in enumerate(frames, 1):
        parts += [f"frame {i}", types.Part.from_bytes(data=pth.read_bytes(), mime_type="image/jpeg")]
    got = {o["frame"]: o["answer"] for o in generate(client, parts, schema)}
    ans = [got.get(i, "unclear") for i in range(1, len(frames) + 1)]
    errs = [sum(x == "yes" for x in ans[:c]) + sum(x == "no" for x in ans[c:]) for c in range(len(ans) + 1)]
    c = min(range(len(errs)), key=errs.__getitem__)
    t = frames[c][0] - step / 2 if c < len(frames) else frames[-1][0] + step / 2
    if c in (0, len(frames)):
        log(f"    경계 {before}->{after}: 창 끝에 걸림 (실제 경계가 ±{half:g}s 밖일 수 있음)")
    return t, "".join({"yes": "Y", "no": "n", "unclear": "?"}[x] for x in ans)


def detect_phases(client, video, out_dir, interval, batch, half=40, step=1, redo_coarse=False):
    # 대략 분류는 요청 수가 많으니 결과를 바로 저장하고, 다음 실행에서 재사용 (무료 tier 하루 요청 한도)
    cache = out_dir / "coarse_labels.json"
    if cache.exists() and not redo_coarse:
        rows = json.loads(cache.read_text())
        labels = {r["t"]: r["phase"] for r in rows}
        details = {r["t"]: r for r in rows}
        log(f"  대략 분류: 저장된 {cache.name} 사용 ({len(rows)} 프레임)")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            frames, start = extract_frames(video, interval, Path(tmp))
            log(f"  {len(frames)} 프레임 ({interval}s 간격, 1s 간격 3장 묶음) 추출")
            labels, details = classify_frames(client, frames, start, batch)
        cache.write_text(json.dumps([{**details[t], "t": t, "mmss": mmss(t), "phase": labels[t]} for t in labels], indent=1))
    times = sorted(labels)
    coarse = segment_prior(times, labels, details)
    log("  대략: " + " | ".join(f"{k} {mmss(a)}~{mmss(b)}" for k, (a, b) in coarse.items()))

    # 경계만 정밀하게: other|첫 단계, 단계|다음 단계, 마지막 단계|other. 단계 사이는 이어 붙인다.
    order = [k for k, _, _ in PHASES if k in coarse]
    pairs = [(OTHER, order[0], coarse[order[0]][0])]
    pairs += [(p, q, (coarse[p][1] + coarse[q][0]) / 2) for p, q in zip(order, order[1:])]
    pairs += [(order[-1], OTHER, coarse[order[-1]][1])]
    cuts, trace = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        start = start_frame(video, tmp)
        for before, after, t0 in pairs:
            t, tr = refine_boundary(client, video, t0, before, after, half, step, tmp, start)
            t = max(t, cuts[-1]) if cuts else t  # 순서 유지
            cuts.append(t)
            trace[f"{before}->{after}"] = {"coarse": mmss(t0), "refined": mmss(t), "frames": tr}
            log(f"  경계 {before}->{after}: {mmss(t0)} -> {mmss(t)}  {tr}")
    segs = {k: (cuts[i], cuts[i + 1]) for i, k in enumerate(order)}
    (out_dir / "boundaries.json").write_text(json.dumps(
        {"coarse": {k: [mmss(a), mmss(b)] for k, (a, b) in coarse.items()}, "boundaries": trace}, indent=1))
    (out_dir / "frame_labels.json").write_text(json.dumps(
        [{"t": round(t, 1), "mmss": mmss(t), **{k: v for k, v in details[t].items() if k != "t"}, "phase": labels[t]}
         for t in times], indent=1))
    lines = [f"# Data : {video.name}  (Gemini {MODEL}, frame {interval}s + 경계 ±{half}s 를 {step}s 간격으로 재확인)"]
    lines += [f"{i}. {name} : {mmss(segs[k][0])} ~ {mmss(segs[k][1])}" for i, (k, name, _) in enumerate(PHASES, 1) if k in segs]
    (out_dir / "timestamp.md").write_text("\n".join(lines) + "\n")
    (out_dir / "phases.json").write_text(json.dumps({k: list(v) for k, v in segs.items()}, indent=1))
    log("  " + " | ".join(lines[1:]))
    return segs


# ---------------------------------------------------------------- 2. 자르기

def cut_clips(video, segs, out_dir, stem):
    clips = {}
    for k, (a, b) in segs.items():
        dst = out_dir / f"{stem}_{k}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.2f}", "-i", str(video), "-t", f"{b - a:.2f}",
                        "-c", "copy", "-map", "0:v:0", "-map", "0:a?", "-movflags", "+faststart", str(dst)], check=True)
        clips[k] = dst
        log(f"  {dst.name}  {mmss(a)}~{mmss(b)}  {dst.stat().st_size / 1e6:.0f} MB")
    return clips


# ---------------------------------------------------------------- 3. 채점

def upload(client, clip):
    """720p/10fps 사본을 올리고 ACTIVE 가 될 때까지 기다린다."""
    with tempfile.TemporaryDirectory() as tmp:
        proxy = Path(tmp) / f"{clip.stem}_720p.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-vf", "scale=-2:720,fps=10", "-an",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", str(proxy)], check=True)
        f = client.files.upload(file=str(proxy), config={"mime_type": "video/mp4"})
    while f.state.name == "PROCESSING":
        time.sleep(5)
        f = client.files.get(name=f.name)
    if f.state.name != "ACTIVE":
        raise RuntimeError(f"{clip.name} 업로드 실패: {f.state}")
    return f


def score_phase(client, key, clip):
    name = dict((k, n) for k, n, _ in PHASES)[key]
    items = RUBRIC[key]
    rubric = "\n".join(f"{i}. {en} ({ko}) — max {m} points: {c}" for i, (en, ko, m, c) in enumerate(items, 1))
    schema = {"type": "OBJECT", "properties": {
        "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "criterion": {"type": "STRING"}, "score": {"type": "INTEGER"},
            "finding": {"type": "STRING", "description": "English, 1-2 sentences, what was observed"},
            "finding_ko": {"type": "STRING", "description": "Korean translation of finding"},
            "evidence": {"type": "STRING", "description": "clip time(s) m:ss supporting the score"}},
            "required": ["criterion", "score", "finding", "finding_ko", "evidence"]}},
        "summary": {"type": "STRING", "description": "English one-line summary of this phase for the report"},
        "summary_ko": {"type": "STRING"}}, "required": ["items", "summary", "summary_ko"]}
    outcomes = json.loads(TEMPLATE.read_text())["outcomes"]
    _, options = report_rules()
    if key == "dissection":  # 결과 지표는 박리 클립 (절제 완료 장면 포함) 에서 같이 판단
        schema["properties"]["outcomes"] = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"}, "status": {"type": "STRING", "enum": sorted({o for op in options for o in op})},
            "detail": {"type": "STRING", "description": "English, short, what was observed"}},
            "required": ["name", "status", "detail"]}}
        schema["required"].append("outcomes")
        rubric += "\n\nAlso judge these procedural outcomes, choosing status ONLY from the allowed values " \
                  "(use \"pending\" when it cannot be confirmed from the video, e.g. R0 needs pathology):\n" + \
                  "\n".join(f"- {o['name']}: {' / '.join(op)}" for o, op in zip(outcomes, options))
    f = upload(client, clip)
    try:
        prompt = (f"You are an expert endoscopist grading the {name} phase of an ESD procedure from this video clip "
                  f"(the clip contains only this phase). Score each criterion with an integer from 0 to its max, "
                  f"based only on what is visible. Give the observation behind each score and clip timestamps.\n"
                  f"Criteria:\n{rubric}\nReturn the items in this order with criterion names exactly as written.")
        out = generate(client, [f, prompt], schema)
    finally:
        client.files.delete(name=f.name)
    for it, (en, ko, m, _) in zip(out["items"], items):  # 이름·배점은 우리 기준표 값으로 고정
        it.update(criterion=en, criterion_ko=ko, max=m, score=max(0, min(m, int(it["score"]))))
    out["score"] = sum(it["score"] for it in out["items"])
    out["max"] = sum(m for *_, m, _ in items)
    if "outcomes" in out:  # 이름 순서는 보고서 양식, 선택지 밖 값은 pending
        got = {o["name"]: o for o in out["outcomes"]}
        out["outcomes"] = [{"name": o["name"], "status": got.get(o["name"], {}).get("status", "pending")
                            if got.get(o["name"], {}).get("status") in op else "pending",
                            "detail": got.get(o["name"], {}).get("detail", "")} for o, op in zip(outcomes, options)]
    return out


# ---------------------------------------------------------------- 4. 보고서

def case_info(video):
    """영상 이름 -> (날짜 또는 None, case_id). case_id 는 server.py 가 허용하는 문자([\\w.-])만 남긴다."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(.+)", Path(video).stem)
    date, cid = (m.group(1), m.group(2)) if m else (None, Path(video).stem)
    return date, re.sub(r"[^\w.-]", "_", cid)


def write_report(video, stem, segs, scores, out_dir):
    case = json.loads(TEMPLATE.read_text())
    date, case["case_id"] = case_info(video)
    case["date"] = date or case["date"]
    case["phases"] = []
    for k, name, _ in PHASES:
        if k not in segs:
            continue
        s = scores.get(k, {})
        case["phases"].append({"key": k, "name": name, "duration_sec": int(round(segs[k][1] - segs[k][0])),
                               "score": s.get("score", 0), "comment": s.get("summary", ""),
                               "start": mmss(segs[k][0]), "end": mmss(segs[k][1]), "items": s.get("items", [])})
    # 종합: report.html 규칙 그대로 (점수 0–100, 0.5 단위 / 등급은 gradeFor 구간 / 상위 %는 비교 집단이 없어 비움)
    bands, _ = report_rules()
    total = sum(p["score"] for p in case["phases"])
    pmax = 10 * len(PHASES)
    pct = round(200 * total / pmax) / 2
    case["overall"] = {"grade": next((g for b, g in bands if pct >= b), "F"), "score": pct,
                       "percentile_top": None, "process_score": total, "process_max": pmax}
    if "outcomes" in scores.get("dissection", {}):
        case["outcomes"] = scores["dissection"]["outcomes"]
    case["generated_by"] = f"{MODEL} · {time.strftime('%Y-%m-%d %H:%M')}"
    CASES.mkdir(parents=True, exist_ok=True)
    (CASES / f"{case['case_id']}.json").write_text(json.dumps(case, indent=2, ensure_ascii=False))
    index = sorted(p.stem for p in CASES.glob("*.json") if p.stem != "index")
    (CASES / "index.json").write_text(json.dumps(index))

    md = [f"# {video.name} — ESD 단계별 평가 ({MODEL})", "",
          "| 단계 | 구간 | 세부 항목 | 배점 | 득점 | 소견 | 근거 |", "|---|---|---|---|---|---|---|"]
    for p in case["phases"]:
        for i, it in enumerate(p["items"]):
            head = f"**{p['name']}** ({p['score']}/10)" if i == 0 else ""
            span = f"{p['start']}~{p['end']}" if i == 0 else ""
            md.append(f"| {head} | {span} | {it['criterion']} ({it['criterion_ko']}) | {it['max']} | **{it['score']}** | "
                      f"{it['finding_ko']} | {it['evidence']} |")
    md += ["", f"**공정 점수 {total} / {pmax}** → 종합 {pct:g} / 100, 등급 {case['overall']['grade']}", ""]
    md += [f"- **{p['name']}**: {scores.get(p['key'], {}).get('summary_ko', '')}" for p in case["phases"]]
    md += ["", "| 결과 지표 | 판정 | 내용 |", "|---|---|---|"] + [f"| {o['name']} | {o['status']} | {o['detail']} |" for o in case["outcomes"]]
    (out_dir / "report.md").write_text("\n".join(md) + "\n")
    log(f"  보고서: report/cases/{case['case_id']}.json  ({total}/{pmax})")


# ----------------------------------------------------------------

def run(client, video, args):
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", video.stem)
    out_dir = ROOT / f"{video.stem}_gemini"  # 수작업 결과(_phases/)와 분리
    out_dir.mkdir(exist_ok=True)
    log(f"=== {video.name} ({mmss(duration(video))})")

    pj = out_dir / "phases.json"
    if pj.exists() and not args.force and not args.redo_coarse:  # --redo-coarse 면 구간도 다시
        segs = {k: tuple(v) for k, v in json.loads(pj.read_text()).items()}
        log("  단계 구간: 기존 phases.json 사용")
    else:
        segs = detect_phases(client, video, out_dir, args.interval, args.batch, redo_coarse=args.redo_coarse)
    if "phases" == args.steps:
        return

    clips = {k: out_dir / f"{stem}_{k}.mp4" for k in segs}
    if args.force or not all(p.exists() for p in clips.values()):
        clips = cut_clips(video, segs, out_dir, stem)

    sj = out_dir / "scores.json"
    scores = json.loads(sj.read_text()) if sj.exists() and not args.force else {}
    for k, clip in clips.items():
        if k in scores:
            continue
        log(f"  채점: {k}")
        scores[k] = score_phase(client, k, clip)
        sj.write_text(json.dumps(scores, indent=1, ensure_ascii=False))  # 단계마다 저장 (중간에 끊겨도 이어서)
        log(f"    {scores[k]['score']}/{scores[k]['max']}  {scores[k]['summary']}")
    write_report(video, stem, segs, scores, out_dir)


def main():
    global MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="*", help="기본: 이 폴더의 *.mkv")
    ap.add_argument("--interval", type=float, default=5, help="단계 판단용 프레임 간격 (초)")
    ap.add_argument("--batch", type=int, default=55, help="요청 한 번에 넣는 프레임 수 (너무 크면 무료 tier 에서 503 이 잦다)")
    ap.add_argument("--steps", choices=["all", "phases"], default="all")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--no-fallback", action="store_true", help="다른 모델로 자동 전환하지 않음")
    ap.add_argument("--redo-coarse", action="store_true", help="저장된 대략 분류(coarse_labels.json)를 버리고 다시")
    args = ap.parse_args()
    MODEL = args.model
    if args.no_fallback:
        FALLBACK.clear()
    videos = [Path(v).resolve() for v in args.videos] or sorted(ROOT.glob("*.mkv"))
    if not videos:
        sys.exit("영상이 없습니다")
    load_env()
    client = genai.Client(http_options=types.HttpOptions(timeout=300_000))  # 요청당 5분 (응답 없이 멈추는 경우 대비)
    for v in videos:
        run(client, v, args)


if __name__ == "__main__":
    main()
