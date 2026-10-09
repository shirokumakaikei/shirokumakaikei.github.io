"""しろくま会計 ビルド。

content/articles/*.md と content/legacy.yml から
  - articles/<slug>.html(mdの記事)
  - articles/index.html(記事一覧)
  - index.html の新着欄
  - sitemap.xml
を生成し、出力先ディレクトリ(既定: _site)へ書き出す。リポジトリ本体は書き換えない。

使い方:
  python tools/build.py            # _site に出力
  python tools/build.py --check    # 検証だけ(PRのCI用)
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import markdown
import yaml

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://shirokumakaikei.github.io"
CATEGORIES = ["キャリア・転職", "試験・学習", "実務"]
SECTION = {"キャリア・転職": "キャリア", "試験・学習": "試験・学習", "実務": "実務"}
EXCLUDE = {".git", ".github", "content", "tools", "slack", "team", "_site", "README-pipeline.md"}
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
BANNED = ["—", "―"]  # エムダッシュ類
AD_RE = re.compile(r"\[\[ad:([a-z0-9-]+)(?::(banner))?\]\]")
AD_SERVICES = 3                    # 1記事に置けるサービスの種類
AD_LIMIT = {"text": 3, "banner": 1}  # 1記事あたりの合計
PR_NOTE = '<p class="pr-note">この記事にはプロモーション(広告)を含みます。</p>'

AUTHOR_NOTE = (
    "税理士法人で約3年半、法人・個人の顧問を担当したあと、不動産業の事業会社で経理を担当しています。"
    "税理士試験は科目合格。事務所と事業会社の両方を経験した立場から、この記事を書いています。"
)
AUTHOR_DESC = "税理士法人を経て不動産業の事業会社で経理を担当。税理士試験は科目合格。"

HEADER = """<header class="site-header">
  <div class="wrap">
    <a class="brand" href="{p}index.html">しろくま会計<span>SHIROKUMA KAIKEI</span></a>
    <nav class="site-nav">
      <a href="{p}articles/index.html">記事一覧</a>
      <a href="{p}about.html">運営者情報</a>
      <a href="{p}contact.html">お問い合わせ</a>
    </nav>
  </div>
</header>"""

FOOTER = """<footer class="site-footer">
  <div class="wrap">
    <span>&copy; {year} しろくま会計</span>
    <span>
      <a href="{p}about.html">運営者情報</a> ／
      <a href="{p}privacy.html">プライバシーポリシー</a> ／
      <a href="{p}contact.html">お問い合わせ</a>
    </span>
  </div>
</footer>"""


class BuildError(Exception):
    pass


def esc(s: str) -> str:
    return html.escape(s, quote=True)


# ---------- 読み込み ----------

def parse_md(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise BuildError(f"{path.name}: front matter(---で囲む)がありません")
    meta = yaml.safe_load(m.group(1)) or {}
    meta["body_md"] = m.group(2).strip()
    meta["slug"] = path.stem
    meta["legacy"] = False
    return meta


def load_ads(active_only: bool = False) -> dict:
    """content/affiliates.yml の広告。active: false は停止中(公開済み記事からも消える)。"""
    p = ROOT / "content/affiliates.yml"
    if not p.exists():
        return {}
    ads = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("ads") or {}
    if active_only:
        ads = {k: v for k, v in ads.items() if v.get("active", True)}
    return ads


def check_ads(name: str, body: str, strict: bool = True) -> list[str]:
    """広告の目印を検証する。広告コードの直書きは禁止(コードの書き間違い・捏造を防ぐ)。
    strict=False(公開済み記事のビルド)では、停止・削除した広告は表示しないだけでエラーにしない。"""
    errs = []
    ads = load_ads(active_only=True)
    count = {"text": 0, "banner": 0}
    per = {}
    for line in body.splitlines():
        for m in AD_RE.finditer(line):
            if line.strip() != m.group(0):
                errs.append(f"{name}: 広告の目印 {m.group(0)} は1行に単独で置く")
            kind = m.group(2) or "text"
            if m.group(1) not in ads:
                if strict:
                    errs.append(f"{name}: 広告 {m.group(1)} は使えません(登録が無いか停止中)")
                continue
            if not ads[m.group(1)].get(kind):
                errs.append(f"{name}: 広告 {m.group(1)} には{'バナー' if kind == 'banner' else '文字リンク'}がありません")
            count[kind] += 1
            per[(m.group(1), kind)] = per.get((m.group(1), kind), 0) + 1
    names = {n for n, _ in per}
    if len(names) > AD_SERVICES:
        errs.append(f"{name}: 広告のサービスが{len(names)}種類。{AD_SERVICES}種類まで")
    for k, n in AD_LIMIT.items():
        if count[k] > n:
            errs.append(f"{name}: 広告({'バナー' if k == 'banner' else '文字リンク'})が{count[k]}個。{n}個まで")
    for (n, k), c in per.items():
        if k == "text" and c > 1:
            errs.append(f"{name}: {n} の文字リンクが{c}回。同じサービスは1回まで")
    if re.search(r"a8\.net|a8mat=|<a\s[^>]*rel=\"?nofollow", body):
        errs.append(f"{name}: 広告コードを本文に直接書かない。[[ad:...]] の目印を使う")
    return errs


def expand_ads(body_md: str) -> tuple[str, bool]:
    """目印を広告コードに置き換える。停止・削除した広告の目印は消す。"""
    ads = load_ads(active_only=True)
    used = False

    def rep(m: re.Match) -> str:
        nonlocal used
        kind = m.group(2) or "text"
        code = (ads.get(m.group(1)) or {}).get(kind)
        if not code:
            print(f"注意: 広告 {m.group(0)} は停止・削除済みなので表示しません", file=sys.stderr)
            return ""
        used = True
        return f'\n<div class="ad-{kind}">\n{code.strip()}\n</div>\n'

    return AD_RE.sub(rep, body_md), used


def validate(a: dict) -> list[str]:
    errs = []
    name = a["slug"]
    if not SLUG_RE.match(name):
        errs.append(f"{name}: slugは半角英小文字・数字・ハイフンのみ")
    for k in ["title", "description", "summary", "category", "date"]:
        if not a.get(k):
            errs.append(f"{name}: front matter に {k} がありません")
    if a.get("category") and a["category"] not in CATEGORIES:
        errs.append(f"{name}: category は {CATEGORIES} のどれか")
    if a.get("date"):
        try:
            a["date"] = date.fromisoformat(str(a["date"]))
        except ValueError:
            errs.append(f"{name}: date は YYYY-MM-DD")
    if len(a.get("title", "")) > 48:
        errs.append(f"{name}: title が長すぎます(48字以内)")
    if not (50 <= len(a.get("description", "")) <= 160):
        errs.append(f"{name}: description は50〜160字")
    joined = " ".join(str(a.get(k, "")) for k in ["title", "description", "summary", "body_md"])
    for b in BANNED:
        if b in joined:
            errs.append(f"{name}: 禁止文字 {b!r} が含まれています")
    body = a.get("body_md", "")
    if "この記事を書いた人" in body:
        errs.append(f"{name}: 「この記事を書いた人」はテンプレートが自動で入れるので本文に書かない")
    if re.search(r"\[[^\]]*\.html\]\(", body):
        errs.append(f"{name}: リンクの表示文字がファイル名になっている。記事タイトルか自然な言い回しにする")
    plain = len(re.sub(r"[#*\[\]()\-\s]|\(.*?\.html\)", "", AD_RE.sub("", body)))
    if not (2000 <= plain <= 6000):
        errs.append(f"{name}: 本文が{plain}字。2,500〜4,000字を目安に収める")
    errs += check_ads(name, body, strict=False)
    if "<script" in body.lower():
        errs.append(f"{name}: 本文に<script>は使えません")
    h2 = a.get("body_md", "").count("\n## ") + a.get("body_md", "").startswith("## ")
    if not (3 <= h2 <= 7):
        errs.append(f"{name}: 見出し(##)が{h2}個。4〜6個にする")
    return errs


def load_all() -> list[dict]:
    legacy = yaml.safe_load((ROOT / "content/legacy.yml").read_text(encoding="utf-8"))["articles"]
    for a in legacy:
        a["legacy"] = True
        if not (ROOT / "articles" / f"{a['slug']}.html").exists():
            raise BuildError(f"legacy記事 {a['slug']}.html が articles/ にありません")
    md = [parse_md(p) for p in sorted((ROOT / "content/articles").glob("*.md"))]
    errs = [e for a in md for e in validate(a)]
    slugs = [a["slug"] for a in legacy + md]
    dup = {s for s in slugs if slugs.count(s) > 1}
    if dup:
        errs.append(f"slug重複: {sorted(dup)}")
    if errs:
        raise BuildError("\n".join(errs))
    today = date.today()
    published = [a for a in md if a["date"] <= today and not a.get("draft")]
    published.sort(key=lambda a: (a["date"], a["slug"]), reverse=True)
    return published + legacy  # 新しい順


def list_titles(exclude: str = "") -> list[tuple[str, str]]:
    """検証せずに (slug, title) を集める。生成途中の壊れた下書きがあっても落ちない。"""
    legacy = yaml.safe_load((ROOT / "content/legacy.yml").read_text(encoding="utf-8"))["articles"]
    out = [(a["slug"], a["title"]) for a in legacy]
    for p in sorted((ROOT / "content/articles").glob("*.md")):
        if p.stem == exclude:
            continue
        try:
            out.append((p.stem, str(parse_md(p).get("title", p.stem))))
        except (BuildError, yaml.YAMLError):
            pass
    return out


# ---------- レンダリング ----------

def render_article(a: dict, all_slugs: set[str]) -> str:
    body_md, has_ad = expand_ads(a["body_md"])
    body = markdown.markdown(body_md, extensions=["tables", "sane_lists"])
    for target in re.findall(r'href="([a-z0-9-]+)\.html"', body):
        if target not in all_slugs:
            raise BuildError(f"{a['slug']}: 内部リンク先 {target}.html が存在しません")
    url = f"{BASE}/articles/{a['slug']}.html"
    og = a.get("og_description") or a["description"]
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Article",
                "headline": a["title"],
                "description": og,
                "datePublished": a["date"].isoformat(),
                "dateModified": str(a.get("updated") or a["date"]),
                "author": {"@type": "Person", "name": "しろくま", "description": AUTHOR_DESC,
                           "url": f"{BASE}/about.html"},
                "publisher": {"@type": "Organization", "name": "しろくま会計", "url": f"{BASE}/"},
                "mainEntityOfPage": {"@type": "WebPage", "@id": url},
                "inLanguage": "ja",
                "articleSection": SECTION[a["category"]],
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "ホーム", "item": f"{BASE}/"},
                    {"@type": "ListItem", "position": 2, "name": "記事一覧", "item": f"{BASE}/articles/index.html"},
                    {"@type": "ListItem", "position": 3, "name": a["title"]},
                ],
            },
        ],
    }
    t = esc(a["title"])
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t} | しろくま会計</title>
<meta name="description" content="{esc(a['description'])}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="article">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{esc(og)}">
<meta property="og:url" content="{url}">
<meta property="og:site_name" content="しろくま会計">
<meta property="og:locale" content="ja_JP">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{t}">
<meta name="twitter:description" content="{esc(og)}">
<link rel="stylesheet" href="../assets/style.css">
<script type="application/ld+json">
{json.dumps(ld, ensure_ascii=False, indent=2)}
</script>
</head>
<body>

{HEADER.format(p="../")}

<section class="hero">
  <div class="wrap">
    <nav class="breadcrumb" aria-label="パンくず"><a href="../index.html">ホーム</a> ／ <a href="../articles/index.html">記事一覧</a> ／ <span>{t}</span></nav>
    <h1>{t}</h1>
  </div>
</section>

<main>
  <div class="wrap">
    {PR_NOTE if has_ad else ""}
    <div class="note">
      <p><strong>この記事を書いた人</strong></p>
      <p>{AUTHOR_NOTE}</p>
    </div>

{body}
  </div>
</main>

{FOOTER.format(p="../", year=date.today().year)}

</body>
</html>
"""


def render_index(articles: list[dict]) -> str:
    sections = []
    for cat in CATEGORIES:
        items = [a for a in articles if a["category"] == cat]
        if not items:
            continue
        lis = "\n".join(
            f"""      <li>
        <a href="{a['slug']}.html">{esc(a['title'])}</a><br>
        {esc(a['summary'])}
      </li>""" for a in items)
        sections.append(f"    <h2>{cat}</h2>\n    <ul>\n{lis}\n    </ul>")
    body = "\n\n".join(sections)
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>記事一覧 | しろくま会計</title>
<meta name="description" content="しろくま会計の記事一覧。税理士事務所と事業会社経理の両方を経験した立場から、会計業界の転職・キャリア、税理士試験、経理実務について書いています。">
<link rel="canonical" href="{BASE}/articles/index.html">
<meta property="og:type" content="website">
<meta property="og:title" content="記事一覧 | しろくま会計">
<meta property="og:description" content="会計業界の転職・キャリア、税理士試験、経理実務についての記事一覧です。">
<meta property="og:url" content="{BASE}/articles/index.html">
<meta property="og:site_name" content="しろくま会計">
<meta property="og:locale" content="ja_JP">
<link rel="stylesheet" href="../assets/style.css">
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "CollectionPage",
  "name": "記事一覧",
  "url": "{BASE}/articles/index.html",
  "publisher": {{"@type": "Organization", "name": "しろくま会計", "url": "{BASE}/"}},
  "inLanguage": "ja"
}}
</script>
</head>
<body>

{HEADER.format(p="../")}

<section class="hero">
  <div class="wrap">
    <h1>記事一覧</h1>
    <p class="lead">税理士試験の科目別の学習、税理士事務所と事業会社経理の実務、会計業界でのキャリアについて書いています。</p>
  </div>
</section>

<main>
  <div class="wrap">

{body}

  </div>
</main>

{FOOTER.format(p="../", year=date.today().year)}

</body>
</html>
"""


def render_home(src: str, articles: list[dict]) -> str:
    lis = "\n".join(
        f'      <li><a href="articles/{a["slug"]}.html">{esc(a["title"])}</a></li>' for a in articles[:6])
    block = (f"<h2>新着記事</h2>\n    <ul>\n{lis}\n    </ul>\n"
             f'    <p><a href="articles/index.html">記事一覧をすべて見る（全{len(articles)}記事）</a></p>')
    new, n = re.subn(r"<h2>新着記事</h2>.*?</p>", block, src, count=1, flags=re.S)
    if n != 1:
        raise BuildError("index.html に新着記事ブロックが見つかりません")
    return new


def render_sitemap(articles: list[dict]) -> str:
    rows = [
        (f"{BASE}/", "weekly", "1.0", None),
        (f"{BASE}/articles/index.html", "weekly", "0.9", None),
    ]
    for a in articles:
        lm = a["date"].isoformat() if not a["legacy"] else None
        rows.append((f"{BASE}/articles/{a['slug']}.html", "monthly", "0.8", lm))
    rows += [(f"{BASE}/about.html", "yearly", "0.5", None),
             (f"{BASE}/contact.html", "yearly", "0.3", None),
             (f"{BASE}/privacy.html", "yearly", "0.2", None)]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, cf, pr, lm in rows:
        lmx = f"<lastmod>{lm}</lastmod>" if lm else ""
        out.append(f"  <url><loc>{loc}</loc>{lmx}<changefreq>{cf}</changefreq><priority>{pr}</priority></url>")
    out.append("</urlset>")
    return "\n".join(out) + "\n"


# ---------- メイン ----------

def build(out: Path) -> list[dict]:
    articles = load_all()
    slugs = {a["slug"] for a in articles}
    rendered = {f"articles/{a['slug']}.html": render_article(a, slugs) for a in articles if not a["legacy"]}
    rendered["articles/index.html"] = render_index(articles)
    rendered["index.html"] = render_home((ROOT / "index.html").read_text(encoding="utf-8"), articles)
    rendered["sitemap.xml"] = render_sitemap(articles)

    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT, out, ignore=lambda d, names: [n for n in names if Path(d) == ROOT and n in EXCLUDE])
    for rel, content in rendered.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (out / ".nojekyll").write_text("")
    return articles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "_site"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    try:
        if args.check:
            articles = load_all()
            slugs = {a["slug"] for a in articles}
            for a in articles:
                if not a["legacy"]:
                    render_article(a, slugs)
            print(f"OK: {len(articles)}記事")
        else:
            articles = build(Path(args.out))
            print(f"built {len(articles)} articles -> {args.out}")
    except BuildError as e:
        print(f"BUILD ERROR\n{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
