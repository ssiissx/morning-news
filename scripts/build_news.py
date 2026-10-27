#!/usr/bin/env python3
"""아침 뉴스 모음 만들기.

scripts/feeds.json 에 적힌 RSS 출처에서 기사를 모아, 같은 사건을 다룬 기사끼리 묶고
여러 곳이 보도한 순서로 정리해 index.html 을 만든다.
파이썬 표준 라이브러리만 쓰므로 따로 설치할 것이 없다.

    python scripts/build_news.py
"""
import concurrent.futures as cf
import email.utils
import html
import json
import math
import re
import sys
import urllib.parse
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
GN_PARAMS = {"ko": "hl=ko&gl=KR&ceid=KR:ko", "ja": "hl=ja&gl=JP&ceid=JP:ja",
             "en": "hl=en-US&gl=US&ceid=US:en"}
LANG_LABEL = {"en": "해외 언론 · 영어", "ja": "일본 언론 · 일본어"}
WEEKDAYS = "월화수목금토일"

# 묶을 때 쓰는 기준. 제목의 글자쌍(한·일)이나 단어(영어)가 얼마나 겹치는지로 판단한다.
MIN_COMMON = {"ko": 3, "ja": 3, "en": 2}
THRESHOLD = {"ko": 0.45, "ja": 0.45, "en": 0.5}
STOP_EN = set("""the and for with from that this into over after amid about says said will
have has are was were but not its his her their they what when where who why how new more
than out off can could would should may just been also news live latest update""".split())


# ---------- 가져오기 ----------

def feed_url(feed):
    if feed.get("query"):
        q = urllib.parse.quote(feed["query"])
        return f"https://news.google.com/rss/search?q={q}&{GN_PARAMS[feed['lang']]}"
    return feed["url"]


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


def clean_summary(s, title):
    s = BYLINE_RE.sub("", clean_text(s)).strip()
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


def extract(root, feed):
    """한 출처의 XML에서 기사 목록을 뽑는다. match/exclude 는 제목 정규식 필터."""
    match = re.compile(feed["match"]) if feed.get("match") else None
    exclude = re.compile(feed["exclude"]) if feed.get("exclude") else None
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = clean_text(child_text(el, "title"))
        link = link_of(el)
        if not title or not link.startswith(("http://", "https://")):
            continue
        outlet = feed["name"]
        summary = child_text(el, "description", "summary", "content")
        if feed.get("kind") == "gnews":
            src = next((ch for ch in el if local(ch.tag) == "source"), None)
            if src is not None and (src.text or "").strip():
                outlet = src.text.strip()
            title = strip_outlet(title, outlet)
            summary = ""  # 구글 뉴스 설명은 관련 기사 링크 목록이라 쓰지 않는다
        if (match and not match.search(title)) or (exclude and exclude.search(title)):
            continue
        items.append({
            "title": title,
            "link": link,
            "summary": clean_summary(summary, title),
            "date": parse_date(child_text(el, "pubDate", "published", "updated", "date")),
            "outlet": outlet,
            "lang": feed["lang"],
        })
    return items


# ---------- 묶기 ----------

def norm_title(t):
    t = re.sub(r"^\s*(?:\[[^\]]{1,14}\]|【[^】]{1,14}】|<[^>]{1,10}>)\s*", "", t)
    t = re.sub(r"^\s*(?:\[[^\]]{1,14}\]|【[^】]{1,14}】)\s*", "", t)
    t = re.sub(r"[\"'“”‘’「」『』<>《》〈〉…·,.!?:;()\[\]{}|/\\~\-–—_=+*&^%$#@]", " ", t)
    return t.lower()


def features(title, lang):
    t = norm_title(title)
    if lang in ("ko", "ja"):
        s = re.sub(r"\s+", "", t)
        return {s[i:i + 2] for i in range(len(s) - 1)}
    return {w for w in re.findall(r"[a-z0-9']{3,}", t) if w not in STOP_EN}


def cluster(items, lang, now):
    for it in items:
        it["feat"] = features(it["title"], lang)
    df = Counter(f for it in items for f in it["feat"])
    n = len(items)
    idf = {f: math.log((n + 1) / (c + 0.5)) for f, c in df.items()}
    weight = lambda fs: sum(idf.get(f, 0.0) for f in fs)
    for it in items:
        it["w"] = weight(it["feat"]) or 1e-9

    clusters, index = [], {}
    for it in sorted(items, key=lambda x: x["date"] or now, reverse=True):
        shared = Counter()
        for f in it["feat"]:
            for ci in index.get(f, ()):
                shared[ci] += 1
        best, best_s = None, 0.0
        for ci, cnt in shared.items():
            if cnt < MIN_COMMON[lang]:
                continue
            for m in clusters[ci]["members"][:4]:
                common = it["feat"] & m["feat"]
                s = weight(common) / min(it["w"], m["w"])
                if s > best_s:
                    best, best_s = ci, s
        if best is not None and best_s >= THRESHOLD[lang]:
            c = clusters[best]
            c["members"].append(it)
            if len(c["members"]) <= 4:
                for f in it["feat"]:
                    index.setdefault(f, set()).add(best)
        else:
            clusters.append({"members": [it], "lang": lang})
            for f in it["feat"]:
                index.setdefault(f, set()).add(len(clusters) - 1)
    for c in clusters:
        c["outlets"] = list(dict.fromkeys(m["outlet"] for m in c["members"]))
        c["latest"] = max((m["date"] for m in c["members"] if m["date"]), default=None)
    return clusters


def similar(a, b, lang):
    common = a & b
    if len(common) < MIN_COMMON[lang]:
        return False
    return len(common) / max(1, min(len(a), len(b))) >= 0.6


def pick_rep(c, prefer):
    members = c["members"]
    for name in prefer:
        for m in members:
            if m["outlet"] == name:
                return m
    with_summary = [m for m in members if m["summary"]]
    return with_summary[0] if with_summary else members[0]


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
        f'<h3><a href="{esc(rep["link"])}" rel="noopener">{esc(rep["title"])}</a></h3>',
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
                      f'<a href="{esc(m["link"])}" rel="noopener">{esc(m["title"])}</a></li>')
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
nav.jump{display:flex;gap:6px;overflow-x:auto;margin:18px 0 6px;padding-bottom:4px;scrollbar-width:none}
nav.jump a{flex:none;padding:6px 12px;border:1px solid var(--line);border-radius:999px;background:var(--surface);
font-size:.875rem;text-decoration:none;color:var(--ink)}
section{margin-top:34px}
h2{margin:0 0 4px;font-size:1.35rem;letter-spacing:-.01em}
h2 + .sub{margin:0 0 6px;color:var(--soft);font-size:.85rem}
h4{margin:22px 0 2px;color:var(--soft);font-size:.8rem;font-weight:600;letter-spacing:.04em}
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
.empty{color:var(--soft);font-size:.9rem}
footer{margin-top:48px;padding-top:18px;border-top:1px solid var(--line);color:var(--soft);font-size:.82rem;display:grid;gap:12px}
footer h2{font-size:1rem;color:var(--ink)}
.days{display:flex;flex-wrap:wrap;gap:6px}
.days a{padding:4px 10px;border:1px solid var(--line);border-radius:8px;background:var(--surface);text-decoration:none;color:var(--ink);font-size:.82rem}
.days a[aria-current]{border-color:var(--accent);color:var(--accent);font-weight:600}
footer p{margin:0}
.bad{color:var(--soft);text-decoration:line-through}
"""


def render_page(now, sections_html, nav, stats, sources_line, days, current, prefix):
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
<p class="stat">{fmt_time(now)} 기준 · 언론사 {stats['outlets']}곳의 기사 {stats['items']:,}건을 같은 사건끼리 묶었어요</p>
</header>
<nav class="jump" aria-label="섹션">{nav}</nav>
{sections_html}
<footer>
<h2>지난 아침 뉴스</h2>
<div class="days"><a href="{home}">오늘</a>{''.join(day_links)}</div>
<p>‘N곳 보도’는 같은 사건을 다룬 언론사 수예요. 많이 보도된 순서로 놓았고, 요약은 각 언론사가 RSS에 실은 첫 문장이에요.</p>
<p>출처: {sources_line}</p>
<p>RSS 앱으로도 보려면 <a href="{home}feeds.opml">출처 목록(OPML)</a>을 가져가세요.</p>
</footer>
</div>
</body>
</html>
"""


def write_opml(config, path):
    seen, rows = set(), []
    for sec in config["sections"]:
        for f in sec["feeds"]:
            url = feed_url(f)
            if url in seen:
                continue
            seen.add(url)
            label = f["name"] + (f" · {f['query']}" if f.get("query") else "")
            rows.append(f'<outline type="rss" text="{esc(label)}" title="{esc(label)}" xmlUrl="{esc(url)}"/>')
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0"><head><title>아침 뉴스 출처</title></head><body>\n'
                    + "\n".join(rows) + "\n</body></opml>\n", encoding="utf-8")


# ---------- 실행 ----------

def main():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    prefer = config.get("prefer", [])
    now = datetime.now(KST)

    feeds = [(si, f) for si, sec in enumerate(config["sections"]) for f in sec["feeds"]]
    urls = list(dict.fromkeys(feed_url(f) for _, f in feeds))  # 같은 주소는 한 번만 받는다
    fetched, errors = {}, {}
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fetch_url, u): u for u in urls}
        for fut in cf.as_completed(futs):
            u = futs[fut]
            try:
                fetched[u] = fut.result()
            except Exception as e:  # 한 곳이 실패해도 나머지로 만든다
                errors[u] = f"{type(e).__name__}: {str(e)[:80]}"

    results, failed = {}, {}
    for fi, (_, f) in enumerate(feeds):
        u = feed_url(f)
        if u in fetched:
            results[fi] = extract(fetched[u], f)
            print(f"  ok   {f['name']:<16} {len(results[fi]):>4}  {u[:70]}")
        else:
            failed[fi] = errors[u]
            print(f"  FAIL {f['name']:<16} {failed[fi]}")

    if len(fetched) < MIN_OK_FEEDS:
        print(f"받아진 출처가 {len(fetched)}곳뿐이라 페이지를 바꾸지 않습니다.", file=sys.stderr)
        return 1

    total_items = len({it["link"] for v in results.values() for it in v})
    shown = []  # 앞 섹션에 이미 나온 사건은 뒤 섹션에서 빼기 위해
    sections_html, nav = [], []
    for si, sec in enumerate(config["sections"]):
        cutoff = now - timedelta(hours=sec.get("hours", 30))
        by_lang, seen_links = {}, set()
        for fi, (s2, f) in enumerate(feeds):
            if s2 != si or fi not in results:
                continue
            for it in results[fi]:
                key = it["link"]
                if key in seen_links or (it["date"] and it["date"] < cutoff) or (it["date"] and it["date"] > now + timedelta(hours=2)):
                    continue
                seen_links.add(key)
                by_lang.setdefault(it["lang"], []).append(it)

        blocks = []
        for lang, limit in sec["limits"].items():
            items = by_lang.get(lang, [])
            if not items:
                continue
            clusters = cluster([dict(it) for it in items], lang, now)
            if sec.get("sort") == "recent":
                clusters.sort(key=lambda c: c["latest"] or cutoff, reverse=True)
            else:
                clusters.sort(key=lambda c: (len(c["outlets"]), c["latest"] or cutoff), reverse=True)
            picked = []
            for c in clusters:
                feats = [m["feat"] for m in c["members"][:6]]
                if any(l2 == lang and any(similar(a, b, lang) for a in feats for b in f2s)
                       for l2, f2s in shown):
                    continue
                picked.append(c)
                shown.append((lang, feats))
                if len(picked) >= limit:
                    break
            if not picked:
                continue
            head = f'<h4>{LANG_LABEL[lang]}</h4>' if lang in LANG_LABEL else ""
            blocks.append(head + "\n".join(render_story(c, prefer, now) for c in picked))

        if not blocks:
            continue
        nav.append(f'<a href="#{sec["id"]}">{esc(sec["title"])}</a>')
        sections_html.append(f'<section id="{sec["id"]}" aria-labelledby="h-{sec["id"]}">'
                             f'<h2 id="h-{sec["id"]}">{esc(sec["title"])}</h2>' + "\n".join(blocks) + "</section>")

    names = list(dict.fromkeys(f["name"] for fi, (_, f) in enumerate(feeds) if fi in results))
    bad = list(dict.fromkeys(f["name"] for fi, (_, f) in enumerate(feeds) if fi in failed and f["name"] not in names))
    sources_line = " · ".join(esc(n) for n in names)
    if bad:
        sources_line += " · 오늘 못 받은 곳: " + " · ".join(f'<span class="bad">{esc(n)}</span>' for n in bad)

    archive = OUT / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    today = now.date().isoformat()
    keep_after = (now - timedelta(days=ARCHIVE_DAYS)).date().isoformat()
    for p in archive.glob("*.html"):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem < keep_after:
            p.unlink()
    days = sorted({p.stem for p in archive.glob("*.html")} | {today}, reverse=True)[:14]

    outlets = {it["outlet"] for v in results.values() for it in v}
    stats = {"outlets": len(outlets), "items": total_items}
    body = "\n".join(sections_html)
    (OUT / "index.html").write_text(
        render_page(now, body, "".join(nav), stats, sources_line, days, today, ""), encoding="utf-8")
    (archive / f"{today}.html").write_text(
        render_page(now, body, "".join(nav), stats, sources_line, days, today, "../"), encoding="utf-8")
    write_opml(config, OUT / "feeds.opml")
    print(f"완료: 출처 {len(names)}곳(언론사 {len(outlets)}곳), 기사 {total_items}건 → {OUT / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
