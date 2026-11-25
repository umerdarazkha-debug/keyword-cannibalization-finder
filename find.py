#!/usr/bin/env python3
"""Keyword cannibalization finder.

Reads a Search Console export that has the query and the page on the same row,
finds queries where two or more of your pages share the impressions, and writes
one offline HTML report plus two CSV files.

    python find.py export.csv --out report/ --brand "acme, acme shop"

Standard library only. Python 3.9+.
"""
import argparse
import csv
import datetime
import html
import io
import json
import re
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

VERSION = "1.0.0"

ALIASES = {
    "query": ["query", "queries", "topqueries", "keyword", "keywords", "searchquery",
              "searchqueries", "searchterm", "searchterms"],
    "page": ["page", "pages", "toppages", "landingpage", "landingpages", "url", "urls",
             "address", "pageurl", "landingpageurl"],
    "clicks": ["clicks", "click", "urlclicks", "totalclicks"],
    "impressions": ["impressions", "impression", "impr", "urlimpressions", "totalimpressions"],
    "position": ["position", "averageposition", "avgposition", "avgpos", "averagepos",
                 "avgposition", "pos"],
}
REQUIRED = ["query", "page", "clicks", "impressions"]


class InputError(Exception):
    pass


# ---------- reading the export ----------

def decode(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def sniff_delimiter(text):
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    counts = {d: first.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def slug(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def map_columns(header, overrides):
    """Returns {field: index}. Overrides are exact header names from the file."""
    found = {}
    lowered = [h.strip().lower() for h in header]
    for field, wanted in overrides.items():
        if not wanted:
            continue
        if wanted.strip().lower() not in lowered:
            raise InputError(
                'Column "%s" is not in the file. Columns found: %s' % (wanted, ", ".join(header)))
        found[field] = lowered.index(wanted.strip().lower())
    slugs = [slug(h) for h in header]
    for field, names in ALIASES.items():
        if field in found:
            continue
        for name in names:
            if name in slugs:
                found[field] = slugs.index(name)
                break
    missing = [f for f in REQUIRED if f not in found]
    if missing:
        flags = " ".join("--%s-col" % m for m in missing)
        hint = ""
        if "query" in missing or "page" in missing:
            hint = ("\nThe file needs the query and the page on the same row. The standard "
                    "Search Console download (Queries.csv, Pages.csv) does not have that. "
                    "See the README for three ways to get it.")
        raise InputError(
            "Could not find a column for: %s.\nColumns found: %s\nName them with %s.%s"
            % (", ".join(missing), ", ".join(header) or "(none)", flags, hint))
    return found


def to_number(value):
    """'1,234' -> 1234.0, '12,3' -> 12.3, '1.234,5' -> 1234.5, '4.2%' -> 4.2. None if empty."""
    s = re.sub(r"[\s\u00a0\u202f%]", "", value or "")
    if not s or s in ("-", "--", "null", "NULL", "None", "nan", "NaN"):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def to_count(value):
    """Clicks and impressions are whole numbers, so a lone separator is a thousands mark."""
    s = re.sub(r"[\s\u00a0\u202f]", "", value or "")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        return int(re.sub(r"[.,]", "", s))
    n = to_number(value)
    return int(round(n)) if n is not None and n >= 0 else 0


def page_key(url, strip_params=False, keep_fragments=False):
    """One key for http/https, a trailing slash, upper-case hosts and #fragments."""
    u = url.strip()
    try:
        p = urlsplit(u)
    except ValueError:
        return u.lower()
    if not p.netloc:
        key = u if keep_fragments else u.split("#")[0]
        return key.rstrip("/") or "/"
    path = p.path.rstrip("/") or "/"
    query = "" if strip_params else p.query
    frag = p.fragment if keep_fragments else ""
    return urlunsplit(("", p.netloc.lower(), path, query, frag))


def read_rows(path, overrides=None):
    """Returns rows of (query, page, clicks, impressions, position) and some counts.
    Rows where Search Console hides the query come back with an empty query: they are
    never a candidate, but their impressions belong to the page's total."""
    raw = Path(path).read_bytes()
    text = decode(raw)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=sniff_delimiter(text))
    header = None
    for row in reader:
        if any(c.strip() for c in row):
            header = [c.strip() for c in row]
            break
    if header is None:
        raise InputError("The file is empty.")
    cols = map_columns(header, overrides or {})
    need = max(cols.values())
    rows, stats = [], {"rows": 0, "blank_query": 0, "bad_rows": 0,
                       "has_position": "position" in cols}
    for row in reader:
        if not any(c.strip() for c in row):
            continue
        stats["rows"] += 1
        if len(row) <= need:
            stats["bad_rows"] += 1
            continue
        query = " ".join(row[cols["query"]].split()).lower()
        page = row[cols["page"]].strip()
        if not page:
            stats["bad_rows"] += 1
            continue
        if not query or query in ("(not set)", "(other)", "null"):
            stats["blank_query"] += 1
            query = ""
        pos = to_number(row[cols["position"]]) if "position" in cols else None
        rows.append((query, page, to_count(row[cols["clicks"]]),
                     to_count(row[cols["impressions"]]), pos))
    return rows, stats


# ---------- finding the candidates ----------

def analyse(rows, opts):
    brand = [b.strip().lower() for b in (opts.get("brand") or "").split(",") if b.strip()]
    exclude = re.compile(opts["exclude"], re.I) if opts.get("exclude") else None
    include = re.compile(opts["include"], re.I) if opts.get("include") else None

    cell = defaultdict(lambda: [0, 0, 0.0, 0])      # (query, key) -> clicks, impr, pos*w, w
    variants = defaultdict(lambda: defaultdict(int))  # key -> {url as written: impressions}
    page_total = defaultdict(int)                     # key -> impressions over the whole file
    skipped = {"brand": set(), "filtered_rows": 0}

    for query, page, clicks, impr, pos in rows:
        if (exclude and exclude.search(page)) or (include and not include.search(page)):
            skipped["filtered_rows"] += 1
            continue
        key = page_key(page, opts.get("strip_params"), opts.get("keep_fragments"))
        variants[key][page.split("#")[0] if not opts.get("keep_fragments") else page] += max(impr, 1)
        page_total[key] += impr
        if not query:
            continue
        if any(b in query for b in brand):
            skipped["brand"].add(query)
            continue
        c = cell[(query, key)]
        c[0] += clicks
        c[1] += impr
        if pos is not None and pos > 0:
            w = max(impr, 1)
            c[2] += pos * w
            c[3] += w

    display = {k: max(v, key=v.get) for k, v in variants.items()}
    by_query = defaultdict(list)
    for (query, key), (clicks, impr, pw, w) in cell.items():
        by_query[query].append({"key": key, "clicks": clicks, "impressions": impr,
                                "position": round(pw / w, 1) if w else None})

    candidates = []
    for query, pages in by_query.items():
        total_impr = sum(p["impressions"] for p in pages)
        if len(pages) < 2 or total_impr < opts["min_impressions"]:
            continue
        rivals = [p for p in pages
                  if p["impressions"] >= opts["min_page_impressions"]
                  and p["impressions"] / total_impr >= opts["min_share"]]
        if len(rivals) < 2:
            continue
        r_clicks = sum(p["clicks"] for p in rivals)
        r_impr = sum(p["impressions"] for p in rivals)
        basis = "clicks" if r_clicks >= opts["click_basis"] else "impressions"
        other = "impressions" if basis == "clicks" else "clicks"
        rivals.sort(key=lambda p: (-p[basis], -p[other],
                                   p["position"] if p["position"] is not None else 999, p["key"]))
        denom = r_clicks if basis == "clicks" else r_impr
        for p in rivals:
            p["share"] = round(p[basis] / denom, 4) if denom else 0.0
            p["url"] = display[p["key"]]
        second = max(p["share"] for p in rivals[1:])
        severity = ("high" if second >= opts["high"] else
                    "medium" if second >= opts["medium"] else "low")
        positions = [p["position"] for p in rivals[:2]]
        note = ""
        if all(x is not None for x in positions):
            if max(positions) <= 10:
                note = ("Both pages average in the top 10. That can be a double listing, "
                        "which is fine. Check the live results before you change anything.")
            elif min(positions) > 10:
                note = "No page averages in the top 10 for this query."
        candidates.append({
            "query": query, "severity": severity, "basis": basis, "second_share": second,
            "clicks": sum(p["clicks"] for p in pages), "impressions": total_impr,
            "other_impressions": r_impr - rivals[0]["impressions"],
            "pages": [{k: p[k] for k in ("url", "key", "clicks", "impressions", "position", "share")}
                      for p in rivals],
            "note": note,
        })
    rank = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: (rank[c["severity"]], -c["other_impressions"], c["query"]))

    pairs = build_pairs(candidates, page_total, display, opts)
    summary = {
        "queries_checked": len(by_query),
        "candidates": len(candidates),
        "high": sum(c["severity"] == "high" for c in candidates),
        "medium": sum(c["severity"] == "medium" for c in candidates),
        "low": sum(c["severity"] == "low" for c in candidates),
        "pairs": len(pairs),
        "clicks": sum(c["clicks"] for c in candidates),
        "impressions": sum(c["impressions"] for c in candidates),
        "brand_queries_skipped": len(skipped["brand"]),
        "filtered_rows": skipped["filtered_rows"],
    }
    return candidates, pairs, summary


def build_pairs(candidates, page_total, display, opts):
    agg = {}
    for c in candidates:
        for a, b in combinations(c["pages"][:4], 2):
            ka, kb = sorted((a["key"], b["key"]))
            g = agg.setdefault((ka, kb), {"queries": [], "stat": {ka: [0, 0, 0.0, 0], kb: [0, 0, 0.0, 0]}})
            g["queries"].append((c["impressions"], c["query"], c["severity"]))
            for p in (a, b):
                s = g["stat"][p["key"]]
                s[0] += p["clicks"]
                s[1] += p["impressions"]
                if p["position"] is not None:
                    s[2] += p["position"] * max(p["impressions"], 1)
                    s[3] += max(p["impressions"], 1)
    pairs = []
    for (ka, kb), g in agg.items():
        # same idea as for one query: with too few clicks to trust, impressions decide
        i = 0 if g["stat"][ka][0] + g["stat"][kb][0] >= opts["click_basis"] else 1
        strong, weak = sorted((ka, kb), key=lambda k: (-g["stat"][k][i], -g["stat"][k][1 - i], k))

        def side(k):
            s = g["stat"][k]
            return {"url": display[k], "clicks": s[0], "impressions": s[1],
                    "position": round(s[2] / s[3], 1) if s[3] else None}
        n = len(g["queries"])
        weak_total = page_total.get(weak, 0)
        overlap = round(g["stat"][weak][1] / weak_total, 4) if weak_total else 0.0
        if n >= opts["same_topic_queries"] and overlap >= opts["same_topic_overlap"]:
            kind = "Same topic"
            step = ("Most of the weaker page's visibility is on queries the stronger page already "
                    "covers. If both answer the same question, merge the weaker page into the "
                    "stronger one and 301 redirect it.")
        elif n >= opts["same_topic_queries"]:
            kind = "Partly overlapping"
            step = ("Keep both. Give each page its own main query in the title and H1, and link "
                    "from the weaker page to the stronger one using the shared query as anchor text.")
        else:
            kind = "One-off overlap"
            step = ("A link from the weaker page to the stronger one, with the query as anchor "
                    "text, is usually enough.")
        top = sorted(g["queries"], reverse=True)
        sev = min((q[2] for q in g["queries"]), key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])
        pairs.append({
            "strong": side(strong), "weak": side(weak), "shared_queries": n,
            "shared_impressions": g["stat"][ka][1] + g["stat"][kb][1],
            "shared_clicks": g["stat"][ka][0] + g["stat"][kb][0],
            "weak_overlap": overlap, "kind": kind, "next_step": step, "severity": sev,
            "top_queries": [q[1] for q in top[:8]],
        })
    pairs.sort(key=lambda p: (-p["shared_queries"], -p["shared_impressions"], p["strong"]["url"]))
    return pairs


# ---------- writing the outputs ----------

def write_csvs(out, candidates, pairs):
    with open(out / "queries.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["query", "severity", "query_clicks", "query_impressions", "rank_in_query",
                    "page", "page_clicks", "page_impressions", "page_avg_position",
                    "share", "share_of", "note"])
        for c in candidates:
            for i, p in enumerate(c["pages"], 1):
                w.writerow([c["query"], c["severity"], c["clicks"], c["impressions"], i,
                            p["url"], p["clicks"], p["impressions"],
                            "" if p["position"] is None else p["position"],
                            round(p["share"] * 100, 1), c["basis"], c["note"]])
    with open(out / "pairs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stronger_page", "weaker_page", "shared_queries", "shared_impressions",
                    "shared_clicks", "stronger_clicks", "weaker_clicks", "stronger_avg_position",
                    "weaker_avg_position", "weaker_page_overlap_pct", "type", "next_step",
                    "top_shared_queries"])
        for p in pairs:
            w.writerow([p["strong"]["url"], p["weak"]["url"], p["shared_queries"],
                        p["shared_impressions"], p["shared_clicks"], p["strong"]["clicks"],
                        p["weak"]["clicks"],
                        "" if p["strong"]["position"] is None else p["strong"]["position"],
                        "" if p["weak"]["position"] is None else p["weak"]["position"],
                        round(p["weak_overlap"] * 100, 1), p["kind"], p["next_step"],
                        " | ".join(p["top_queries"])])


def safe_json(data):
    """JSON that cannot close the script tag it sits in."""
    return (json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def write_html(out, source_name, candidates, pairs, summary, stats, opts, today=None):
    cap = opts["max_rows"]
    data = {
        "source": source_name,
        "generated": (today or datetime.date.today()).isoformat(),
        "summary": summary, "stats": stats,
        "settings": {k: opts[k] for k in ("min_impressions", "min_page_impressions", "min_share",
                                          "high", "medium", "click_basis", "same_topic_queries",
                                          "same_topic_overlap")},
        "brand": opts.get("brand") or "",
        "queries": candidates[:cap], "pairs": pairs[:cap],
        "queries_total": len(candidates), "pairs_total": len(pairs),
    }
    page = TEMPLATE.replace("__TITLE__", html.escape("Keyword cannibalization: " + source_name))
    page = page.replace("__DATA__", safe_json(data))
    (out / "cannibalization.html").write_text(page, encoding="utf-8")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>__TITLE__</title>
<style>
:root { --bg:#f6f7f9; --panel:#ffffff; --fg:#17202b; --mute:#5b6675; --line:#e2e6eb; --accent:#0f766e; --chip:#eef1f4; --bad:#b42318; --warn:#9a5b00; --good:#1f7a4c; --bar:#cfd6de;
  --font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; color-scheme:light }
@media (prefers-color-scheme: dark) { :root { --bg:#0f1318; --panel:#161b22; --fg:#e6ebf1; --mute:#9aa5b4; --line:#272f39; --accent:#2cc9b4; --chip:#1f2630; --bad:#f97066; --warn:#f2b54a; --good:#4ade80; --bar:#33404d; color-scheme:dark } }
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 var(--font) }
.wrap { max-width:1120px; margin:0 auto; padding:28px 16px 64px }
h1 { font-size:26px; line-height:1.2; margin:0 0 6px; letter-spacing:-.01em }
.sub { color:var(--mute); margin:0 0 22px; overflow-wrap:anywhere }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:16px }
.tile { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px }
.tile b { display:block; font-size:26px; line-height:1.1; font-variant-numeric:tabular-nums }
.tile span { color:var(--mute); font-size:13px }
.box { background:var(--panel); border:1px solid var(--line); border-left:3px solid var(--accent); border-radius:10px; padding:14px 16px; margin-bottom:18px }
.box p { margin:0 0 6px } .box p:last-child { margin:0 }
.box ul { margin:6px 0 0; padding-left:20px; color:var(--mute) }
.tabs { display:flex; gap:6px; flex-wrap:wrap; margin:22px 0 12px; border-bottom:1px solid var(--line) }
.tabs button { font:inherit; font-weight:600; color:var(--mute); background:none; border:0; border-bottom:2px solid transparent; padding:10px 12px; cursor:pointer }
.tabs button[aria-selected="true"] { color:var(--fg); border-bottom-color:var(--accent) }
.tools { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px }
.tools input, .tools select { font:inherit; color:var(--fg); background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:8px 10px; min-width:0 }
.tools input { flex:1 1 220px }
.count { color:var(--mute); font-size:13px; margin:0 0 10px }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:10px }
.head { display:flex; gap:8px 12px; flex-wrap:wrap; align-items:baseline; margin-bottom:8px }
.head strong { font-size:16px; overflow-wrap:anywhere }
.meta { color:var(--mute); font-size:13px; font-variant-numeric:tabular-nums }
.sev { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; padding:2px 8px; border-radius:99px; background:var(--chip) }
.sev.high { color:var(--bad) } .sev.medium { color:var(--warn) } .sev.low { color:var(--mute) }
.row { display:grid; grid-template-columns:84px minmax(0,1fr) auto; gap:4px 12px; align-items:center; padding:7px 0; border-top:1px solid var(--line) }
.tag { font-size:12px; font-weight:700; color:var(--mute) }
.tag.keep { color:var(--good) }
.url { font:13px/1.4 var(--mono); overflow-wrap:anywhere }
.url a { color:inherit; text-decoration:none } .url a:hover { text-decoration:underline }
.nums { color:var(--mute); font-size:13px; font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap }
.bar { grid-column:2 / 4; height:5px; background:var(--chip); border-radius:99px; overflow:hidden }
.bar i { display:block; height:100%; background:var(--bar) } .bar i.first { background:var(--accent) }
.step { margin:10px 0 0 } .step b { color:var(--accent) }
.note { margin:10px 0 0; color:var(--mute); font-size:13px }
.chips { display:flex; gap:6px; flex-wrap:wrap; margin-top:10px }
.chips span { background:var(--chip); border-radius:99px; padding:2px 10px; font-size:13px; overflow-wrap:anywhere }
.more { font:inherit; font-weight:600; color:var(--accent); background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:9px 14px; cursor:pointer }
.empty { color:var(--mute); padding:24px 0 }
footer { color:var(--mute); font-size:13px; margin-top:28px; overflow-wrap:anywhere }
[hidden] { display:none !important }
@media (max-width:600px) { .row { grid-template-columns:minmax(0,1fr) } .nums { text-align:left; white-space:normal } .bar { grid-column:1 } }
</style>
</head>
<body>
<div class="wrap">
  <h1>Keyword cannibalization candidates</h1>
  <p class="sub" id="sub"></p>
  <div class="tiles" id="tiles"></div>
  <div class="box">
    <p><strong>These are candidates, not verdicts.</strong> The export shows that two pages got impressions for the same query. It cannot show why.</p>
    <ul>
      <li>Two of your pages listed on the same results page is a double listing, not a problem.</li>
      <li>Search intent is not in the data. A guide and a product page can both deserve to rank.</li>
      <li>Language folders and brand searches produce false hits. Filter them out when you run the script.</li>
    </ul>
  </div>
  <div class="tabs" role="tablist">
    <button role="tab" id="tab-pairs" aria-selected="true">Page pairs</button>
    <button role="tab" id="tab-queries" aria-selected="false">Queries</button>
  </div>
  <div class="tools">
    <input id="search" type="search" placeholder="Filter by query or URL" aria-label="Filter by query or URL">
    <select id="sev" aria-label="Severity">
      <option value="">All severities</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
    </select>
  </div>
  <p class="count" id="count"></p>
  <div id="list"></div>
  <button class="more" id="more" hidden>Show more</button>
  <footer id="foot"></footer>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  var D = JSON.parse(document.getElementById('data').textContent);
  var S = D.summary, tab = 'pairs', shown = 50;
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function n(x) { return Number(x).toLocaleString('en-US'); }
  function pct(x) { return Math.round(x * 100) + '%'; }
  function pos(x) { return x == null ? '' : ' · avg position ' + x.toFixed(1); }
  function link(u) {
    var s = el('span', 'url');
    if (/^https?:\/\//i.test(u)) { var a = el('a', null, u); a.href = u; a.rel = 'noopener noreferrer'; a.target = '_blank'; s.appendChild(a); }
    else s.textContent = u;
    return s;
  }
  document.getElementById('sub').textContent = D.source + ' · generated ' + D.generated;
  [[S.queries_checked, 'queries checked'], [S.candidates, 'candidate queries'], [S.high, 'high severity'],
   [S.pairs, 'page pairs'], [S.clicks, 'clicks on candidate queries']].forEach(function (t) {
    var d = el('div', 'tile'); d.appendChild(el('b', null, n(t[0]))); d.appendChild(el('span', null, t[1]));
    document.getElementById('tiles').appendChild(d);
  });
  function pageRow(tagText, tagCls, p, share, first) {
    var r = el('div', 'row');
    r.appendChild(el('span', 'tag ' + (tagCls || ''), tagText));
    r.appendChild(link(p.url));
    r.appendChild(el('span', 'nums', n(p.clicks) + ' clicks · ' + n(p.impressions) + ' impr.' + pos(p.position)));
    if (share != null) { var b = el('div', 'bar'), i = el('i', first ? 'first' : ''); i.style.width = Math.max(2, Math.round(share * 100)) + '%'; b.appendChild(i); r.appendChild(b); }
    return r;
  }
  function pairCard(p) {
    var c = el('div', 'card'), h = el('div', 'head');
    h.appendChild(el('strong', null, p.kind));
    h.appendChild(el('span', 'sev ' + p.severity, p.severity));
    h.appendChild(el('span', 'meta', n(p.shared_queries) + (p.shared_queries === 1 ? ' shared query' : ' shared queries') + ' · ' + n(p.shared_impressions) + ' impressions · ' + n(p.shared_clicks) + ' clicks'));
    c.appendChild(h);
    c.appendChild(pageRow('Stronger', 'keep', p.strong));
    c.appendChild(pageRow('Weaker', '', p.weak));
    c.appendChild(el('p', 'note', pct(p.weak_overlap) + " of the weaker page's impressions in this export are on these shared queries."));
    var s = el('p', 'step'); s.appendChild(el('b', null, 'Suggested next step: ')); s.appendChild(document.createTextNode(p.next_step)); c.appendChild(s);
    var ch = el('div', 'chips'); p.top_queries.forEach(function (q) { ch.appendChild(el('span', null, q)); }); c.appendChild(ch);
    return c;
  }
  function queryCard(q) {
    var c = el('div', 'card'), h = el('div', 'head');
    h.appendChild(el('strong', null, q.query));
    h.appendChild(el('span', 'sev ' + q.severity, q.severity));
    h.appendChild(el('span', 'meta', n(q.impressions) + ' impressions · ' + n(q.clicks) + ' clicks · second page takes ' + pct(q.second_share) + ' of ' + q.basis));
    c.appendChild(h);
    q.pages.forEach(function (p, i) { c.appendChild(pageRow(pct(p.share), i === 0 ? 'keep' : '', p, p.share, i === 0)); });
    if (q.note) c.appendChild(el('p', 'note', q.note));
    return c;
  }
  function hay(x) {
    return tab === 'pairs' ? (x.strong.url + ' ' + x.weak.url + ' ' + x.top_queries.join(' ')).toLowerCase()
      : (x.query + ' ' + x.pages.map(function (p) { return p.url; }).join(' ')).toLowerCase();
  }
  function render() {
    var term = document.getElementById('search').value.trim().toLowerCase(), sev = document.getElementById('sev').value;
    var all = tab === 'pairs' ? D.pairs : D.queries, total = tab === 'pairs' ? D.pairs_total : D.queries_total;
    var rows = all.filter(function (x) { return (!sev || x.severity === sev) && (!term || hay(x).indexOf(term) !== -1); });
    var list = document.getElementById('list'); list.textContent = '';
    rows.slice(0, shown).forEach(function (x) { list.appendChild(tab === 'pairs' ? pairCard(x) : queryCard(x)); });
    if (!rows.length) list.appendChild(el('p', 'empty', !total ? 'No candidates found with the current settings.' : total > all.length ? 'Nothing matches in the rows this page holds. Filter the CSV files to search all of them.' : 'Nothing matches this filter.'));
    var label = tab === 'pairs' ? 'page pairs' : 'queries';
    var txt = 'Showing ' + n(Math.min(shown, rows.length)) + ' of ' + n(rows.length) + ' ' + label + '.';
    if (total > all.length) txt += ' This page holds the first ' + n(all.length) + ' of ' + n(total) + '. The CSV files have all of them.';
    document.getElementById('count').textContent = txt;
    document.getElementById('more').hidden = rows.length <= shown;
  }
  function pick(t) {
    tab = t; shown = 50;
    document.getElementById('tab-pairs').setAttribute('aria-selected', t === 'pairs');
    document.getElementById('tab-queries').setAttribute('aria-selected', t === 'queries');
    render();
  }
  document.getElementById('tab-pairs').onclick = function () { pick('pairs'); };
  document.getElementById('tab-queries').onclick = function () { pick('queries'); };
  document.getElementById('search').oninput = function () { shown = 50; render(); };
  document.getElementById('sev').onchange = function () { shown = 50; render(); };
  document.getElementById('more').onclick = function () { shown += 50; render(); };
  var st = D.settings, f = [];
  f.push(n(D.stats.rows) + ' rows read');
  if (D.stats.blank_query) f.push(n(D.stats.blank_query) + ' rows with a hidden query, counted in page totals only');
  if (D.stats.bad_rows) f.push(n(D.stats.bad_rows) + ' incomplete rows skipped');
  if (S.filtered_rows) f.push(n(S.filtered_rows) + ' rows removed by the page filter');
  if (S.brand_queries_skipped) f.push(n(S.brand_queries_skipped) + ' brand queries skipped');
  if (!D.stats.has_position) f.push('no position column in the file');
  f.push('settings: query needs ' + n(st.min_impressions) + '+ impressions, a page needs ' + n(st.min_page_impressions) + '+ impressions and ' + pct(st.min_share) + '+ of the query, high from ' + pct(st.high) + ', medium from ' + pct(st.medium));
  document.getElementById('foot').textContent = f.join(' · ') + '.';
  render();
})();
</script>
</body>
</html>
"""


# ---------- command line ----------

def share(value):
    v = float(value)
    if v > 1:
        v /= 100.0
    if not 0 <= v <= 1:
        raise argparse.ArgumentTypeError("use a share between 0 and 1, or a percentage up to 100")
    return v


def build_parser():
    p = argparse.ArgumentParser(
        description="Find queries where two or more of your pages compete, from a Search Console "
                    "export that has the query and the page on the same row.")
    p.add_argument("export", help="CSV file with query, page, clicks and impressions columns")
    p.add_argument("--out", default="report", help="output folder (default: report)")
    p.add_argument("--brand", default="", help='comma-separated brand terms; queries containing one are skipped')
    p.add_argument("--exclude", default="", help="regex; pages that match are ignored, e.g. \"/fr/|/de/\"")
    p.add_argument("--include", default="", help="regex; only pages that match are read")
    p.add_argument("--min-impressions", type=int, default=50, help="a query needs this many impressions in total (default: 50)")
    p.add_argument("--min-page-impressions", type=int, default=10, help="a page needs this many impressions on the query to count (default: 10)")
    p.add_argument("--min-share", type=share, default=0.10, help="a page needs this share of the query's impressions to count (default: 0.10)")
    p.add_argument("--high", type=share, default=0.30, help="second page's share from which a query is high severity (default: 0.30)")
    p.add_argument("--medium", type=share, default=0.15, help="second page's share from which a query is medium severity (default: 0.15)")
    p.add_argument("--click-basis", type=int, default=10, help="with this many clicks on the query, shares use clicks; below it, impressions (default: 10)")
    p.add_argument("--same-topic-queries", type=int, default=3, help="shared queries from which a pair is more than a one-off (default: 3)")
    p.add_argument("--same-topic-overlap", type=share, default=0.50, help="share of the weaker page's impressions on shared queries from which a pair is called the same topic (default: 0.50)")
    p.add_argument("--strip-params", action="store_true", help="treat URLs that differ only by ?parameters as one page")
    p.add_argument("--keep-fragments", action="store_true", help="treat page#section URLs as separate pages")
    p.add_argument("--max-rows", type=int, default=500, help="queries and pairs embedded in the HTML; the CSV files always have all (default: 500)")
    p.add_argument("--query-col", default="", help="exact header of the query column, if it is not detected")
    p.add_argument("--page-col", default="", help="exact header of the page column")
    p.add_argument("--clicks-col", default="", help="exact header of the clicks column")
    p.add_argument("--impressions-col", default="", help="exact header of the impressions column")
    p.add_argument("--position-col", default="", help="exact header of the average position column")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    opts = vars(args).copy()
    src = Path(args.export)
    try:
        if not src.is_file():
            raise InputError("File not found: %s" % src.name)
        for flag in ("exclude", "include"):
            try:
                re.compile(opts[flag])
            except re.error as e:
                raise InputError("--%s is not a valid regex: %s" % (flag, e))
        rows, stats = read_rows(src, {f: opts[f + "_col"] for f in ALIASES})
        if not any(r[0] for r in rows):
            raise InputError("No usable rows in %s (%d rows read, %d with a hidden query)."
                             % (src.name, stats["rows"], stats["blank_query"]))
    except InputError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 2
    candidates, pairs, summary = analyse(rows, opts)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csvs(out, candidates, pairs)
    write_html(out, src.name, candidates, pairs, summary, stats, opts)
    print("%s rows read, %s queries checked" % (format(stats["rows"], ","), format(summary["queries_checked"], ",")))
    print("%s candidate queries (%d high, %d medium, %d low) across %s page pairs"
          % (format(summary["candidates"], ","), summary["high"], summary["medium"], summary["low"],
             format(summary["pairs"], ",")))
    if stats["blank_query"]:
        print("%s rows with a hidden query (counted in page totals only)" % format(stats["blank_query"], ","))
    if summary["brand_queries_skipped"]:
        print("%s brand queries skipped" % format(summary["brand_queries_skipped"], ","))
    print("Report: %s" % (out / "cannibalization.html"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
