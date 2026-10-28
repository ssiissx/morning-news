#!/usr/bin/env python3
"""아침 뉴스 모음 만들기.

scripts/feeds.json 에 적힌 국내 방송사 RSS에서 기사를 모아, 같은 사건을 다룬 기사끼리 묶고
여러 곳이 보도한 순서로 섹션별로 정리해 index.html 을 만든다.

같은 사건인지는 두 가지로 본다.
- 제목의 뜻: 한국어 문장 모델(MODEL_NAME)로 제목을 숫자 벡터로 바꿔 비교한다.
  "투표용지 공개검증"과 "투표지 247만 장 검증"처럼 표현이 달라도 같은 사건이면 가깝게 나온다.
- 제목의 글자: 두 글자씩 끊은 조각이 얼마나 겹치는지. 모델을 못 쓰면 이것만으로 묶는다.

    pip install -r scripts/requirements.txt   # 없어도 돌아가지만 묶는 품질이 떨어진다
    python scripts/build_news.py
"""
import concurrent.futures as cf
import email.utils
import html
import json
import math
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없어서 고정 오프셋으로 충분하다
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG = HERE / "feeds.json"
OUT = ROOT  # 저장소 맨 위에 index.html, archive/, feeds.opml 을 만든다
ARCHIVE_DAYS = 30
MIN_OK_FEEDS = 5  # 이보다 적게 받아지면 어제 페이지를 덮어쓰지 않는다
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
WEEKDAYS = "월화수목금토일"

MODEL_NAME = "jhgan/ko-sroberta-multitask"  # 한국어 문장 뜻 비교 모델(약 0.45GB, 처음 한 번 받아 둔다)

# 묶는 기준. 뜻 유사도(코사인, 1에 가까울수록 같은 뜻)와 글자 겹침(0~1)을 함께 본다.
# 2026-10-28 기사로 맞춘 값: 같은 사건 제목끼리는 뜻 0.56~0.68, 같은 인물·분야의 다른 사건은 0.28~0.49였다.
COS_HIGH = 0.75   # 뜻이 이만큼 가까우면 글자가 달라도 같은 사건
COS_MID = 0.58    # 이 정도면 글자도 LEX_MID 이상 겹쳐야 같은 사건
LEX_MID = 0.15
COS_LOW = 0.50    # 글자가 LEX_HIGH 이상 겹쳐도 뜻이 이보다 멀면 다른 사건 ("오늘 아침" 같은 상투어 대비)
LEX_HIGH = 0.40
COS_JOIN = 0.52   # 묶음 앞쪽 기사들과의 평균 뜻 유사도가 이 이상이어야 들어간다 (꼬리 물기로 번지는 것 방지)
LEX_ONLY = 0.45   # 모델 없이 글자 겹침만으로 묶을 때의 기준
MIN_COMMON = 3    # 글자쌍이 이보다 적게 겹치면 글자 겹침은 0으로 본다
HEAD = 6          # 묶음에서 비교 대상으로 삼는 앞쪽 기사 수
# 기사 2건 이상인 묶음끼리 평균 뜻이 이만큼 가까우면 한 사건의 다른 측면으로 보고 합친다
# ("구룡마을 진화" + "잿더미 된 구룡마을"). 2026-10-28 기사로 보면 0.66~0.70은 거의 같은 사건의 측면이었고,
# 다른 사건이 처음 섞인 쌍이 0.64였다.
MERGE_COS = 0.67


# ---------- 가져오기 ----------

def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(el, *names):
    for name in names:
        for ch in el:
            if local(ch.tag) == name and ch.text and ch.text.strip():
                return ch.text.strip()
    return ""


def link_of(el):
    for ch in el:
        if local(ch.tag) != "link":
            continue
        href = ch.get("href")
        if href and ch.get("rel", "alternate") == "alternate":
            return href.strip()
        if ch.text and ch.text.strip():
            return ch.text.strip()
    for ch in el:
        if local(ch.tag) in ("guid", "id") and (ch.text or "").strip().startswith("http"):
            return ch.text.strip()
    return ""


def parse_date(s):
    if not s:
        return None
    s = re.sub(r"^([A-Za-z]{3}),(\S)", r"\1, \2", s.strip())
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=KST)
    return d


TAG_RE = re.compile(r"<[^>]+>")


def clean_text(s):
    s = html.unescape(html.unescape(s or ""))
    s = TAG_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


BYLINE_RE = re.compile(r"^[(\[【][^)\]】]{1,24}=[^)\]】]{1,24}[)\]】]\s*(?:[가-힣·,\s]{2,24}?(?:기자|특파원|통신원)\s*=?\s*)?")
ANCHOR_RE = re.compile(r"^(?:【\s*앵커\s*멘트\s*】|\[\s*앵커\s*\]|<\s*앵커\s*>)\s*")


def clean_summary(s, title):
    s = ANCHOR_RE.sub("", BYLINE_RE.sub("", clean_text(s)).strip())
    # SBS는 사진 설명이 본문에 띄어쓰기 없이 붙어 와서("▲ 자료화면금융권을…") 끊을 곳을 알 수 없다
    if s.startswith("▲"):
        return ""
    if len(s) < 25 or s == title:
        return ""
    if len(s) <= 170:
        return s
    cut = s[:170]
    ends = [m.end() for m in re.finditer(r"(다\.|\.\s|。|\?\s|!\s)", cut)]
    if ends and ends[-1] > 60:
        return cut[:ends[-1]].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",·") + "…"


def strip_outlet(title, outlet):
    for sep in (" - ", " | "):
        if title.endswith(sep + outlet):
            return title[: -len(sep + outlet)].strip()
    m = re.match(r"^(.*\S)\s+[-|]\s+[^-|]{1,25}$", title)
    return m.group(1) if m else title


def fetch_url(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read(8_000_000)
    return ET.fromstring(body)


def extract(root, feed, categories, exclude):
    """한 출처의 XML에서 기사 목록을 뽑는다.
    섹션은 피드에 적힌 것, 없으면 기사에 붙은 분류(categories 표)로 정하고, 그것도 없으면 None."""
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = clean_text(child_text(el, "title"))
        link = link_of(el)
        if not title or not link.startswith(("http://", "https://")):
            continue
        summary = child_text(el, "description", "summary", "content")
        if feed.get("kind") == "gnews":
            src = next((ch for ch in el if local(ch.tag) == "source"), None)
            title = strip_outlet(title, (src.text or "").strip() if src is not None else feed["name"])
            summary = ""  # 구글 뉴스 설명은 관련 기사 링크 목록이라 쓰지 않는다
        if exclude and exclude.search(title):
            continue
        section = feed.get("section")
        if not section:
            cats = [clean_text(ch.text) for ch in el if local(ch.tag) == "category" and ch.text]
            section = next((categories[c] for c in cats if c in categories), None)
        items.append({
            "title": title,
            "link": link,
            "summary": clean_summary(summary, title),
            "date": parse_date(child_text(el, "pubDate", "published", "updated", "date")),
            "outlet": feed["name"],
            "section": section,
        })
    return items


# ---------- 묶기 ----------

# 제목에서 사건과 상관없는 부분: [단독]·[속보] 같은 말머리, (종합2보), 날짜
TAGS_RE = re.compile(r"\[[^\]]{1,14}\]|【[^】]{1,14}】|<[^>]{1,10}>|\((?:종합|\d+보|속보|상보)[^)]{0,6}\)")
DATE_RE = re.compile(r"\d{4}년|\d{1,2}월\s?\d{1,2}일|\(\d{1,2}일[·\s]?[월화수목금토일]?\)")
PUNCT_RE = re.compile(r"[\"'“”‘’「」『』<>《》〈〉…·,.!?:;()\[\]{}|/\\~\-–—_=+*&^%$#@]")
# 방송사마다 한자 약칭을 쓰기도 하고 안 쓰기도 해서("李대통령" / "이 대통령") 한글로 맞춘다
HANJA = {"李": "이", "尹": "윤", "文": "문", "朴": "박", "金": "김", "美": "미국", "中": "중국", "日": "일본",
         "北": "북한", "韓": "한국", "英": "영국", "獨": "독일", "佛": "프랑스", "露": "러시아", "與": "여당",
         "野": "야당", "檢": "검찰", "軍": "군", "前": "전", "現": "현", "對": "대", "靑": "청와대", "株": "주식"}


def norm_title(t):
    t = TAGS_RE.sub(" ", t)
    t = DATE_RE.sub(" ", t)
    t = "".join(HANJA.get(ch, ch) for ch in t)
    t = PUNCT_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def features(title):
    s = norm_title(title).replace(" ", "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def embed(texts):
    """제목들을 뜻 벡터로 바꾼다. 모델을 쓸 수 없으면 None을 돌려주고 글자 비교로만 묶는다."""
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(MODEL_NAME, device="cpu")
        return model.encode(texts, batch_size=64, normalize_embeddings=True,
                            convert_to_numpy=True, show_progress_bar=False)
    except Exception as e:  # 설치가 안 됐거나 모델을 못 받은 날에도 페이지는 만든다
        print(f"뜻 비교 모델을 쓰지 못해 글자 비교로만 묶습니다: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        return None


def cluster(items, emb, now):
    feats = [features(it["title"]) for it in items]
    df = Counter(f for fs in feats for f in fs)
    n = len(items)
    idf = {f: math.log((n + 1) / (c + 0.5)) for f, c in df.items()}
    weight = lambda fs: sum(idf.get(f, 0.0) for f in fs)
    w = [weight(fs) or 1e-9 for fs in feats]
    cos = emb @ emb.T if emb is not None else None

    def lex(i, j):
        common = feats[i] & feats[j]
        return weight(common) / min(w[i], w[j]) if len(common) >= MIN_COMMON else 0.0

    def same(i, j):
        l = lex(i, j)
        if cos is None:
            return l >= LEX_ONLY
        c = float(cos[i, j])
        return c >= COS_HIGH or (c >= COS_MID and l >= LEX_MID) or (c >= COS_LOW and l >= LEX_HIGH)

    clusters = []      # 묶음마다 기사 번호 목록 (최신 기사가 앞)
    home = {}          # 묶음 앞쪽 HEAD개 기사 번호 -> 묶음 번호
    index = {}         # 글자쌍 -> 그 글자쌍이 있는 앞쪽 기사 번호들

    def enter(i, ci):
        clusters[ci].append(i)
        if len(clusters[ci]) <= HEAD:
            home[i] = ci
            for f in feats[i]:
                index.setdefault(f, []).append(i)

    for i in sorted(range(n), key=lambda k: items[k]["date"] or now, reverse=True):
        shared = Counter(j for f in feats[i] for j in index.get(f, ()))
        cand = {j for j, k in shared.items() if k >= MIN_COMMON}
        if cos is not None and home:
            heads = list(home)
            row = cos[i, heads]
            cand.update(heads[k] for k in range(len(heads)) if row[k] >= COS_LOW)
        best, best_s = None, -1.0
        for ci in {home[j] for j in cand if same(i, j)}:
            head = clusters[ci][:HEAD]
            if cos is not None:
                s = sum(float(cos[i, j]) for j in head) / len(head)
            else:
                s = max(lex(i, j) for j in head)
            if s > best_s:
                best, best_s = ci, s
        if best is not None and (cos is None or best_s >= COS_JOIN):
            enter(i, best)
        else:
            clusters.append([])
            enter(i, len(clusters) - 1)

    if cos is not None:
        clusters = merge_facets(clusters, emb, items, now)

    out = []
    for idx in clusters:
        members = [items[i] for i in idx]
        out.append({
            "idx": idx,
            "members": members,
            "outlets": list(dict.fromkeys(m["outlet"] for m in members)),
            "latest": max((m["date"] for m in members if m["date"]), default=None),
        })
    return out


def merge_facets(clusters, emb, items, now):
    """기사 하나하나로는 기준에 못 미쳤지만 묶음 전체의 뜻이 아주 가까운 것들을 합친다.
    가장 가까운 쌍부터 하나씩 합치고, 합칠 때마다 중심을 다시 계산한다."""
    import numpy as np
    groups = [idx for idx in clusters if len(idx) >= 2]
    rest = [idx for idx in clusters if len(idx) < 2]

    def centroid(idx):
        v = emb[idx].mean(axis=0)
        return v / (np.linalg.norm(v) or 1.0)

    cents = [centroid(g) for g in groups]
    while len(groups) > 1:
        m = np.stack(cents)
        sims = m @ m.T
        np.fill_diagonal(sims, -1.0)
        a, b = divmod(int(sims.argmax()), len(groups))
        if sims[a, b] < MERGE_COS:
            break
        merged = sorted(groups[a] + groups[b], key=lambda k: items[k]["date"] or now, reverse=True)
        for k in sorted((a, b), reverse=True):
            del groups[k], cents[k]
        groups.append(merged)
        cents.append(centroid(merged))
    return groups + rest


def assign_sections(clusters, items, emb, order):
    """묶음의 섹션은 기사들에 붙은 섹션의 다수결로 정한다. 날씨 피드에 실린 사건은 날씨로 보낸다.
    섹션이 붙은 기사가 하나도 없으면(구글 뉴스 등으로만 받은 기사) 뜻이 가장 가까운 기사 7개의 섹션을 따른다.
    이때 날씨는 고르지 않는다 — "내일 오후 최종 조정" 같은 제목이 날씨 예보와 가깝게 나오기 때문."""
    labeled = [i for i, it in enumerate(items) if it["section"] and it["section"] != "weather"]
    lab_emb = emb[labeled] if emb is not None and labeled else None
    for c in clusters:
        votes = Counter(m["section"] for m in c["members"] if m["section"])
        if "weather" in votes:
            c["section"] = "weather"
            continue
        if not votes and lab_emb is not None:
            centroid = emb[c["idx"]].mean(axis=0)
            sims = lab_emb @ centroid
            near = sorted(range(len(labeled)), key=lambda k: -sims[k])[:7]
            votes = Counter(items[labeled[k]]["section"] for k in near if sims[k] >= 0.45)
        c["section"] = (max(votes, key=lambda s: (votes[s], -order.index(s) if s in order else -99))
                        if votes else None)


# 제목 앞뒤의 프로그램 말머리([자막뉴스], [여담야담], [뉴스와이드] 등). [단독]·[속보]는 정보라서 남긴다.
PROGRAM_TAG_RE = re.compile(r"^\s*\[(?!단독|속보)[^\]]{1,14}\]\s*|\s*\[(?!단독|속보)[^\]]{1,14}\]\s*$")


def display_title(t):
    while True:
        cut = PROGRAM_TAG_RE.sub("", t)
        if cut == t or not cut.strip():  # 말머리만 있는 제목이면 그대로 둔다
            return t
        t = cut


def pick_rep(c, prefer):
    """대표 기사: 요약이 있고, 프로그램 말머리가 없는 기사를 선호 순서대로 고른다."""
    members = c["members"]
    ranked = sorted(members, key=lambda m: (not m["summary"], display_title(m["title"]) != m["title"],
                                            prefer.index(m["outlet"]) if m["outlet"] in prefer else len(prefer)))
    return ranked[0]


# ---------- 화면 ----------

def esc(s):
    return html.escape(s or "", quote=True)


def fmt_time(d):
    if not d:
        return ""
    d = d.astimezone(KST)
    ampm = "오전" if d.hour < 12 else "오후"
    h = d.hour % 12 or 12
    return f"{ampm} {h}:{d.minute:02d}"


def fmt_day(d):
    return f"{d.month}월 {d.day}일 {WEEKDAYS[d.weekday()]}요일"


def fmt_rel(d, now):
    if not d:
        return ""
    mins = int((now - d).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)}분 전"
    if mins < 24 * 60:
        return f"{mins // 60}시간 전"
    return f"{mins // (24 * 60)}일 전"


def render_story(c, prefer, now):
    rep = pick_rep(c, prefer)
    summary = rep["summary"] or next((m["summary"] for m in c["members"] if m["summary"]), "")
    outlets = c["outlets"]
    shown = " · ".join(esc(o) for o in outlets[:3])
    more = f" 외 {len(outlets) - 3}곳" if len(outlets) > 3 else ""
    count = f'<span class="count">{len(outlets)}곳 보도</span>' if len(outlets) > 1 else ""
    when = fmt_rel(c["latest"], now)
    parts = [
        '<article class="story">',
        f'<h3><a href="{esc(rep["link"])}" rel="noopener">{esc(display_title(rep["title"]))}</a></h3>',
    ]
    if summary:
        parts.append(f'<p class="lead">{esc(summary)}</p>')
    parts.append(f'<p class="meta">{count}<span>{shown}{more}</span>'
                 + (f'<span class="when">{when}</span>' if when else "") + "</p>")
    others, seen = [], {rep["outlet"]}
    for m in c["members"]:
        if m["outlet"] in seen:
            continue
        seen.add(m["outlet"])
        others.append(f'<li><span class="o">{esc(m["outlet"])}</span>'
                      f'<a href="{esc(m["link"])}" rel="noopener">{esc(display_title(m["title"]))}</a></li>')
    if others:
        parts.append(f'<details><summary>다른 곳은 이렇게 썼어요 ({len(others)})</summary>'
                     f'<ul>{"".join(others)}</ul></details>')
    parts.append("</article>")
    return "\n".join(parts)


CSS = """
:root{--bg:#F4F6F9;--surface:#FFFFFF;--ink:#16202B;--soft:#566373;--line:#DCE2E9;
--accent:#2A55C9;--badge-bg:#E4ECFF;--badge-ink:#1E3F99;
--sans:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR","Hiragino Sans",system-ui,sans-serif;}
@media (prefers-color-scheme: dark){:root{--bg:#0E131A;--surface:#151C25;--ink:#E5EAF0;--soft:#9AA6B4;
--line:#26303C;--accent:#93B2FF;--badge-bg:#1B2742;--badge-ink:#B9CCFF;color-scheme:dark}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:16px;line-height:1.55}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:680px;margin:0 auto;padding-left:max(16px,env(safe-area-inset-left,0px));
padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:28px 64px}
header .day{margin:0;color:var(--soft);font-size:.9rem;font-weight:600}
h1{margin:4px 0 6px;font-size:2.1rem;line-height:1.15;letter-spacing:-.02em}
header .stat{margin:0;color:var(--soft);font-size:.85rem}
nav.jump{position:sticky;top:0;z-index:5;display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;
margin:14px -16px 0;padding:10px 16px;background:var(--bg);border-bottom:1px solid var(--line)}
nav.jump::-webkit-scrollbar{display:none}
nav.jump a{flex:none;padding:6px 12px;border:1px solid var(--line);border-radius:999px;background:var(--surface);
font-size:.875rem;text-decoration:none;color:var(--ink)}
nav.jump a[aria-current]{background:var(--ink);border-color:var(--ink);color:var(--bg);font-weight:600}
section{margin-top:34px;scroll-margin-top:64px}
.tabs section{display:none;margin-top:22px}
.tabs section.on{display:block}
.next{margin:20px 0 0;text-align:right}
.next a{display:inline-block;padding:8px 14px;border:1px solid var(--line);border-radius:999px;background:var(--surface);
color:var(--accent);font-size:.875rem;font-weight:600;text-decoration:none}
h2{margin:0 0 4px;font-size:1.35rem;letter-spacing:-.01em}
.story{padding:14px 0;border-bottom:1px solid var(--line)}
.story:last-of-type{border-bottom:0}
.story h3{margin:0;font-size:1.06rem;line-height:1.4;font-weight:650}
.story h3 a{text-decoration:none}
.story h3 a:hover{text-decoration:underline}
.lead{margin:6px 0 0;color:var(--soft);font-size:.93rem}
.meta{margin:8px 0 0;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center;color:var(--soft);font-size:.8rem}
.count{padding:1px 8px;border-radius:6px;background:var(--badge-bg);color:var(--badge-ink);font-weight:600;font-variant-numeric:tabular-nums}
.when{margin-left:auto;font-variant-numeric:tabular-nums}
details{margin-top:8px;font-size:.88rem}
summary{cursor:pointer;color:var(--accent);font-weight:600;font-size:.82rem}
details ul{list-style:none;margin:8px 0 0;padding:0 0 0 12px;border-left:2px solid var(--line);display:grid;gap:6px}
details .o{display:inline-block;min-width:5.5em;margin-right:6px;color:var(--soft);font-size:.8rem}
footer{margin-top:48px;padding-top:18px;border-top:1px solid var(--line);color:var(--soft);font-size:.82rem;display:grid;gap:12px}
footer h2{font-size:1rem;color:var(--ink)}
.days{display:flex;flex-wrap:wrap;gap:6px}
.days a{padding:4px 10px;border:1px solid var(--line);border-radius:8px;background:var(--surface);text-decoration:none;color:var(--ink);font-size:.82rem}
.days a[aria-current]{border-color:var(--accent);color:var(--accent);font-weight:600}
footer p{margin:0}
.bad{color:var(--soft);text-decoration:line-through}
"""


# 섹션을 탭처럼 하나씩 보여 준다. 위쪽 버튼 줄은 화면 맨 위에 붙어 있어서, 스포츠를 다 읽고도
# 올라가지 않고 다른 섹션으로 갈 수 있다. 주소 끝(#politics)에 섹션이 남아 뒤로 가기도 된다.
# 스크립트가 돌지 않는 환경에서는 예전처럼 모든 섹션이 이어서 보인다.
TABS_JS = """
(function(){
  var links = document.querySelectorAll('nav.jump a');
  var secs = document.querySelectorAll('.wrap > section');
  var head = document.querySelector('.wrap > header');
  if (!links.length || !secs.length) return;
  document.body.classList.add('tabs');
  function show(id, jump){
    var hit = null;
    for (var i = 0; i < secs.length; i++) if (secs[i].id === id) hit = secs[i];
    if (!hit) { hit = secs[0]; jump = false; }
    for (var k = 0; k < secs.length; k++) secs[k].classList.toggle('on', secs[k] === hit);
    for (var j = 0; j < links.length; j++){
      var a = links[j];
      if (a.getAttribute('href') === '#' + hit.id){
        a.setAttribute('aria-current', 'page');
        a.parentNode.scrollLeft = a.offsetLeft - (a.parentNode.clientWidth - a.offsetWidth) / 2;
      } else a.removeAttribute('aria-current');
    }
    // 아래로 내려가 있었으면 새 섹션의 첫 기사가 보이도록 버튼 줄 바로 아래로 올린다
    var top = head.getBoundingClientRect().bottom + window.pageYOffset;
    if (jump || window.pageYOffset > top) window.scrollTo(0, Math.min(window.pageYOffset, top));
  }
  window.addEventListener('hashchange', function(){ show(location.hash.slice(1), true); });
  show(location.hash.slice(1), false);
})();
"""


# 홈 화면에 저장한 앱(특히 아이폰)은 닫지 않고 뒤에 켜 두었다가 어제 화면을 그대로 다시 보여 주고,
# 새로고침 버튼도 없다. 그래서 오늘 페이지는 30분 넘게 지나 다시 화면에 나오면 새로 받아 온다.
# 보던 섹션(#sports)은 주소에 남아 있어서 새로 받아도 그 섹션이 열린다.
REFRESH_JS = """
(function(){
  var loaded = Date.now();
  function check(){
    if (document.visibilityState === 'visible' && Date.now() - loaded > 30 * 60 * 1000) location.reload();
  }
  document.addEventListener('visibilitychange', check);
  window.addEventListener('pageshow', function(e){ if (e.persisted) check(); });
  window.addEventListener('focus', check);
})();
"""


def render_page(now, sections_html, nav, stats, sources_line, days, current, prefix, method_note):
    day_links = []
    for d in days:
        href = f"{prefix}archive/{d}.html" if prefix != "../" else f"{d}.html"
        dt = datetime.fromisoformat(d)
        label = f"{dt.month}/{dt.day} ({WEEKDAYS[dt.weekday()]})"
        cur = ' aria-current="page"' if d == current else ""
        day_links.append(f'<a href="{href}"{cur}>{label}</a>')
    home = "./" if prefix != "../" else "../"
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#F4F6F9" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0E131A" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-title" content="아침 뉴스">
<link rel="manifest" href="{home}manifest.webmanifest">
<link rel="icon" type="image/png" href="{home}icon-192.png">
<link rel="apple-touch-icon" href="{home}icon-180.png">
<title>아침 뉴스 · {now.month}월 {now.day}일</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
<p class="day">{fmt_day(now)}</p>
<h1>아침 뉴스</h1>
<p class="stat">{fmt_time(now)} 기준 · 방송사 {stats['outlets']}곳의 기사 {stats['items']:,}건을 같은 사건끼리 묶었어요</p>
</header>
<nav class="jump" aria-label="섹션">{nav}</nav>
{sections_html}
<footer>
<h2>지난 아침 뉴스</h2>
<div class="days"><a href="{home}">오늘</a>{''.join(day_links)}</div>
<p>‘N곳 보도’는 같은 사건을 다룬 방송사 수예요. 섹션마다 많이 보도된 순서로 놓았어요. {method_note}</p>
<p>요약은 각 방송사가 RSS에 실은 첫 문장이에요. KBS·MBC·YTN·채널A는 공개 RSS가 없어 구글 뉴스 검색으로 받아서, 그 기사만 있는 사건은 요약이 없어요.</p>
<p>출처: {sources_line}</p>
<p>RSS 앱으로도 보려면 <a href="{home}feeds.opml">출처 목록(OPML)</a>을 가져가세요.</p>
</footer>
</div>
<script>{TABS_JS}{REFRESH_JS if prefix == "" else ""}</script>
</body>
</html>
"""


def write_opml(config, path):
    titles = {s["id"]: s["title"] for s in config["sections"]}
    seen, rows = set(), []
    for f in config["feeds"]:
        if f["url"] in seen:
            continue
        seen.add(f["url"])
        label = f["name"] + (f" · {titles.get(f['section'], f['section'])}" if f.get("section") else "")
        rows.append(f'<outline type="rss" text="{esc(label)}" title="{esc(label)}" xmlUrl="{esc(f["url"])}"/>')
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0"><head><title>아침 뉴스 출처</title></head><body>\n'
                    + "\n".join(rows) + "\n</body></opml>\n", encoding="utf-8")


# ---------- 실행 ----------

def gather(config, now):
    """모든 출처를 받아 기간 안의 기사를 모은다. 같은 기사가 여러 피드에 있으면 하나로 합친다."""
    categories = config.get("categories", {})
    exclude = re.compile(config["exclude"]) if config.get("exclude") else None
    feeds = config["feeds"]
    urls = list(dict.fromkeys(f["url"] for f in feeds))  # 같은 주소는 한 번만 받는다
    fetched, errors = {}, {}
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fetch_url, u): u for u in urls}
        for fut in cf.as_completed(futs):
            u = futs[fut]
            try:
                fetched[u] = fut.result()
            except Exception as e:  # 한 곳이 실패해도 나머지로 만든다
                errors[u] = f"{type(e).__name__}: {str(e)[:80]}"
    # 같은 방송사 주소를 한꺼번에 요청하면 응답이 끊기는 곳이 있어서(2026-10-28 JTBC 8곳 모두 시간 초과)
    # 실패한 주소는 하나씩 한 번 더 받아 본다
    for u in list(errors):
        try:
            fetched[u] = fetch_url(u)
            del errors[u]
        except Exception as e:
            errors[u] = f"{type(e).__name__}: {str(e)[:80]}"

    ok, bad = [], []
    cutoff = now - timedelta(hours=config.get("hours", 24))
    items, by_link, by_title = [], {}, {}
    for f in feeds:
        if f["url"] not in fetched:
            print(f"  FAIL {f['name']:<8} {errors[f['url']]}")
            bad.append(f["name"])
            continue
        got = extract(fetched[f["url"]], f, categories, exclude)
        print(f"  ok   {f['name']:<8} {len(got):>4}  {f.get('section') or '-':<9} {f['url'][:60]}")
        ok.append(f["name"])
        for it in got:
            if it["date"] and (it["date"] < cutoff or it["date"] > now + timedelta(hours=2)):
                continue
            # 같은 기사(같은 주소, 또는 같은 방송사의 같은 제목)는 하나로 두고 섹션·요약만 채운다
            tkey = (it["outlet"], norm_title(it["title"]).replace(" ", ""))
            prev = by_link.get(it["link"]) or by_title.get(tkey)
            if prev:
                prev["section"] = prev["section"] or it["section"]
                prev["summary"] = prev["summary"] or it["summary"]
                continue
            by_link[it["link"]] = by_title[tkey] = it
            items.append(it)
    ok = list(dict.fromkeys(ok))
    bad = [n for n in dict.fromkeys(bad) if n not in ok]
    return items, ok, bad, len(fetched)


def main():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    prefer = config.get("prefer", [])
    now = datetime.now(KST)

    items, ok, bad, n_fetched = gather(config, now)
    if n_fetched < MIN_OK_FEEDS:
        print(f"받아진 출처가 {n_fetched}곳뿐이라 페이지를 바꾸지 않습니다.", file=sys.stderr)
        return 1

    emb = embed([norm_title(it["title"]) for it in items]) if items else None
    clusters = cluster(items, emb, now)
    order = [s["id"] for s in config["sections"] if s["id"] != "top"]
    assign_sections(clusters, items, emb, order)
    floor = now - timedelta(days=2)
    clusters.sort(key=lambda c: (len(c["outlets"]), c["latest"] or floor), reverse=True)

    # 섹션은 feeds.json 순서대로 채운다. 주요 뉴스(맨 앞)에 나온 사건은 아래 섹션에서 다시 나오지 않는다.
    used, filled = set(), []
    for sec in config["sections"]:
        if sec["id"] == "top":
            allowed = set(sec.get("from", order))
            least = sec.get("min_outlets", 2)
            pool = [c for c in clusters if c["section"] in allowed and len(c["outlets"]) >= least]
        else:
            pool = [c for c in clusters if c["section"] == sec["id"]]
        picked = [c for c in pool if id(c) not in used][:sec["limit"]]
        used.update(id(c) for c in picked)
        if picked:
            filled.append((sec, picked))

    sections_html, nav = [], []
    for k, (sec, picked) in enumerate(filled):
        nav.append(f'<a href="#{sec["id"]}">{esc(sec["title"])}</a>')
        # 섹션 끝에서 바로 다음 섹션으로 넘어가는 버튼 (마지막 섹션은 맨 앞으로)
        nxt = filled[(k + 1) % len(filled)][0]
        label = "다음" if k + 1 < len(filled) else "처음으로"
        sections_html.append(f'<section id="{sec["id"]}" aria-labelledby="h-{sec["id"]}">'
                             f'<h2 id="h-{sec["id"]}">{esc(sec["title"])}</h2>'
                             + "\n".join(render_story(c, prefer, now) for c in picked)
                             + f'<p class="next"><a href="#{nxt["id"]}">{label}: {esc(nxt["title"])} ›</a></p>'
                             + "</section>")

    sources_line = " · ".join(esc(n) for n in ok)
    if bad:
        sources_line += " · 오늘 못 받은 곳: " + " · ".join(f'<span class="bad">{esc(n)}</span>' for n in bad)
    method_note = ("같은 사건인지는 제목의 뜻과 글자를 함께 비교해서 정해요." if emb is not None
                   else "오늘은 뜻 비교 모델을 쓰지 못해 제목 글자만 비교해서 묶었어요.")

    archive = OUT / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    today = now.date().isoformat()
    keep_after = (now - timedelta(days=ARCHIVE_DAYS)).date().isoformat()
    for p in archive.glob("*.html"):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem < keep_after:
            p.unlink()
    days = sorted({p.stem for p in archive.glob("*.html")} | {today}, reverse=True)[:14]

    stats = {"outlets": len({it["outlet"] for it in items}), "items": len(items)}
    body = "\n".join(sections_html)
    (OUT / "index.html").write_text(
        render_page(now, body, "".join(nav), stats, sources_line, days, today, "", method_note), encoding="utf-8")
    (archive / f"{today}.html").write_text(
        render_page(now, body, "".join(nav), stats, sources_line, days, today, "../", method_note), encoding="utf-8")
    write_opml(config, OUT / "feeds.opml")
    multi = sum(1 for c in clusters if len(c["outlets"]) > 1)
    print(f"완료: 방송사 {stats['outlets']}곳, 기사 {stats['items']}건 → 사건 {len(clusters)}개"
          f"(여러 곳 보도 {multi}개) → {OUT / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
