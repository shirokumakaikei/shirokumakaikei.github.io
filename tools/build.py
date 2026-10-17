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
AD_SERVICES = 3                    # 1記事に置けるサービス(type: service)の種類
AD_LIMIT = {"text": 3, "banner": 1}  # サービスの1記事あたりの合計
AD_PRODUCTS = 3                    # 商品リンク(type: product)は別枠で1記事3つまで(同じ商品は1回)
PRODUCT_LABEL = "記事で触れた商品"
PR_NOTE = '<p class="pr-note">この記事にはプロモーション(広告)を含みます。</p>'
# Amazon アソシエイトの表記(Amazon のリンクを含む記事の末尾。プライバシーポリシーにも同じ文)
AMAZON_NOTE = "Amazonのアソシエイトとして、当サイトは適格販売により収入を得ています。"
# 商品リンクに貼ってよい URL のドメイン(アフィリエイトのリンクだけ。商品ページの直リンクや他のサイトは不可)
SHOP_HOSTS = {
    "amazon_url": ("Amazon", ("amazon.co.jp", "amzn.to", "amzn.asia", "link.amazon")),
    "rakuten_url": ("楽天", ("hb.afl.rakuten.co.jp", "a.r10.to", "r10.to")),
}
SHOP_LINK_REL = "nofollow sponsored noopener"
# アフィリエイトリンクの判定(2026-10-17 オーナー決定)。PR 表記と Amazon の表記は、目印ではなく、できあがった本文の HTML に
# このドメインの URL があるかで機械的に決める。無ければ表記を出さない、あれば必ず出す(ステマ規制)。本社の office.py も同じ一覧
AFFILIATE_HOSTS = {
    "amazon": ("amazon.co.jp", "amazon.com", "amzn.to", "amzn.asia", "link.amazon"),
    "rakuten": ("rakuten.co.jp", "a.r10.to", "r10.to"),
    "asp": ("a8.net", "moshimo.com", "msmstatic.com", "valuecommerce.com", "valuecommerce.ne.jp", "accesstrade.net",
            "afi-b.com", "felmat.net", "rentracks.jp", "linksynergy.com", "tcs-asp.net"),
}
URL_HOST_RE = re.compile(r'(?:https?:)?//([A-Za-z0-9.-]+)')


def affiliate_kinds(html: str) -> set[str]:
    """本文の HTML にあるアフィリエイトリンクの種類(amazon / rakuten / asp)。無ければ空。"""
    kinds = set()
    for h in URL_HOST_RE.findall(html or ""):
        h = h.lower().rstrip(".")
        for k, hosts in AFFILIATE_HOSTS.items():
            if any(h == d or h.endswith("." + d) for d in hosts):
                kinds.add(k)
    return kinds
# 記事の写真(本社の docs/photo-requirements.md)。本文の目印 [[photo:p1]] を、assets/photos/<articles|lab>/<slug>/p1.webp の figure にする。
# alt と大きさは front matter の photos(本社の publish が書く)。画像はメタデータを消した WebP だけ
PHOTO_RE = re.compile(r"^\[\[photo:([pf]\d+)\]\]\s*$", re.M)  # p1 は写真、f1 は図解(本社の tools/figures.py が作った WebP)
PHOTO_MAX_BYTES = 400_000
# 写真ライブラリ(本社の tools/photo_library.py)で使い回した写真は、公開済みの画像をそのまま指す(front matter の photos の src)
PHOTO_SRC_RE = re.compile(r"^assets/photos/(?:articles|lab)/[a-z0-9-]+/[pf]\d+\.webp$")
WEBP_OK_CHUNKS = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF"}

# LINE 公式アカウントへの誘導(本社の docs/line-funnel.md)。content/line.yml の enabled が true で、媒体の友だち追加 URL があるときだけ、
# 全記事(md の記事・手書きの記事・しろくまラボ)の本文の後ろに、記事のテーマに合わせた枠を build が入れる。本文には書かせない
LINE_CONF = "content/line.yml"
LINE_THEMES = ("career", "dx", "shikaku", "it")
LINE_URL_RE = re.compile(r"^https://(?:lin\.ee|line\.me)/[A-Za-z0-9_@%/?=&.~-]+$")
LINE_LINK_RE = re.compile(r"https?://(?:lin\.ee|line\.me|page\.line\.me)/", re.I)
LINE_MARK = "<!-- line-cta -->"

AUTHOR_NOTE = (
    "税理士法人で約3年半、法人・個人の顧問を担当したあと、不動産業の事業会社で経理を担当しています。"
    "税理士試験は科目合格。事務所と事業会社の両方を経験した立場から、この記事を書いています。"
)
AUTHOR_DESC = "税理士法人を経て不動産業の事業会社で経理を担当。税理士試験は科目合格。"

# しろくまラボ(技術の記録)。content/lab/<slug>.md → lab/<slug>.html。ナビには出さず、トップとフッターから入る
LAB_NAME = "しろくまラボ"
LAB_LEAD = "会計とは別に、自宅のサーバーやネットワークなど、手元の機器で試したことを残している技術の記録です。"
LAB_AUTHOR_NOTE = (
    "税理士法人で約3年半、法人・個人の顧問を担当したあと、不動産業の事業会社で経理を担当しています。"
    "工学部(電気電子)出身で、自宅の機器で試したことをこのラボに残しています。"
)
LAB_DISCLAIMER = (
    "この記事は筆者の環境で試した記録です。手順や設定を参考にした結果生じたデータの消失、機器の故障、"
    "セキュリティ上の問題などについて、筆者は責任を負いません。作業の前にバックアップを取り、ご自身の判断で行ってください。"
    "内容は作業時期のものです。ソフトウェアの更新で手順や画面が変わることがあります。"
)

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
      <a href="{p}contact.html">お問い合わせ</a> ／
      <a href="{p}lab/index.html">しろくまラボ</a>
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


def load_line() -> dict:
    """content/line.yml(LINE 公式アカウントの誘導の設定)。無ければ空(誘導を出さない)。"""
    p = ROOT / LINE_CONF
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def check_line(conf: dict) -> list[str]:
    """LINE の誘導の設定。テーマ4つの文面はいつも必須。enabled のときは、サイトとラボの友だち追加 URL も必須。"""
    errs = []
    themes = conf.get("themes") or {}
    for t in LINE_THEMES:
        th = themes.get(t) or {}
        for k in ("tag", "title", "text"):
            if not str(th.get(k) or "").strip():
                errs.append(f"{LINE_CONF}: themes.{t}.{k} がありません")
        if th.get("gift_ready") and not str(th.get("gift") or "").strip():
            errs.append(f"{LINE_CONF}: themes.{t} は gift_ready なのに gift(特典の名前)がありません")
    for k, v in (conf.get("category_theme") or {}).items():
        if v not in LINE_THEMES:
            errs.append(f"{LINE_CONF}: category_theme の {k} のテーマ {v} は {'/'.join(LINE_THEMES)} のどれか")
    for k, u in (conf.get("add_urls") or {}).items():
        if u and not LINE_URL_RE.match(str(u)):
            errs.append(f"{LINE_CONF}: add_urls.{k} は https://lin.ee/... か https://line.me/... の友だち追加 URL")
    if conf.get("enabled"):
        urls = conf.get("add_urls") or {}
        for media in ("site", "lab"):
            if not urls.get(media):
                errs.append(f"{LINE_CONF}: enabled なのに add_urls.{media}(友だち追加 URL)がありません")
    text = yaml.safe_dump(conf, allow_unicode=True)
    for b in BANNED:
        if b in text:
            errs.append(f"{LINE_CONF}: 禁止文字 {b!r} が含まれています")
    return errs


def line_theme(a: dict, section: str = "articles") -> str:
    """記事の誘導のテーマ。front matter の line_theme > ラボは it > カテゴリ(content/line.yml の category_theme)> career。"""
    t = str(a.get("line_theme") or "")
    if t in LINE_THEMES:
        return t
    if section == "lab":
        return "it"
    return (load_line().get("category_theme") or {}).get(a.get("category"), "career")


def line_cta(theme: str, media: str = "site") -> str:
    """記事末尾の LINE の誘導の枠。設定が無い・止めている・URL が無いときは空。
    URL は媒体とテーマごと(add_urls の site-career など)、無ければ媒体ごと。LINE の「友だち追加経路」で流入元を数える。"""
    conf = load_line()
    if not conf.get("enabled"):
        return ""
    urls = conf.get("add_urls") or {}
    url = str(urls.get(f"{media}-{theme}") or urls.get(media) or "")
    th = (conf.get("themes") or {}).get(theme) or {}
    if not LINE_URL_RE.match(url) or not th.get("title"):
        return ""
    gift = (f'<p class="line-cta-gift">友だち追加の特典: {esc(str(th["gift"]))}</p>'
            if th.get("gift_ready") and th.get("gift") else "")
    note = esc(str(conf.get("note") or ""))
    return (f'{LINE_MARK}\n<aside class="line-cta" data-theme="{esc(theme)}">\n'
            f'<p class="line-cta-label">LINE で受け取る</p>\n'
            f'<p class="line-cta-title">{esc(str(th["title"]))}</p>\n'
            f'<p class="line-cta-text">{esc(str(th["text"]))}</p>\n{gift}\n'
            f'<a class="line-cta-btn" href="{esc(url)}" target="_blank" rel="noopener">LINE で友だち追加する</a>\n'
            + (f'<p class="line-cta-note">{note}</p>\n' if note else "") + '</aside>\n')


def add_line_to_legacy(html_text: str, theme: str) -> str:
    """手書きの記事(articles/*.html)の本文の最後(</main> の直前の </div> の前)に誘導を入れる。2回は入れない。"""
    cta = line_cta(theme)
    if not cta or LINE_MARK in html_text:
        return html_text
    new, n = re.subn(r"\n\s*</div>\s*\n</main>", lambda m: "\n" + cta + m.group(0), html_text, count=1)
    if n != 1:
        raise BuildError("手書きの記事に </div></main> が見つからず、LINE の誘導を入れられません")
    return new


def ad_type(ad: dict) -> str:
    """service(転職サービスなどの広告)か product(商品リンク)か。書いていなければ service。"""
    return "product" if str(ad.get("type", "service")).strip() == "product" else "service"


def shop_url_err(key: str, url: str) -> str:
    """Amazon / 楽天のリンクの形を確かめる。問題なければ空文字。"""
    from urllib.parse import urlsplit
    label, hosts = SHOP_HOSTS[key]
    u = urlsplit(str(url).strip())
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or re.search(r"[\s\"'<>]", str(url)):
        return f"{label}のリンクは https:// から始まる URL を1つだけ貼る"
    if not any(host == h or host.endswith("." + h) for h in hosts):
        return f"{label}のリンクのドメイン({host})が違う。使えるのは {' / '.join(hosts)}"
    return ""


def product_errs(pid: str, ad: dict) -> list[str]:
    """商品リンク(type: product)の登録内容。Amazon・楽天のリンク・コードのどれか1つは必須。写真は assets/products/ にある実物の写真。"""
    errs = []
    if not any(ad.get(k) for k in ("amazon_url", "rakuten_url", "code")):
        errs.append(f"商品 {pid}: Amazon のリンク・楽天のリンク・コードのどれかを登録する")
    for k in SHOP_HOSTS:
        if ad.get(k) and (e := shop_url_err(k, ad[k])):
            errs.append(f"商品 {pid}: {e}")
    photo = str(ad.get("photo") or "")
    if photo and (not re.fullmatch(r"assets/products/[a-z0-9-]+\.(?:jpg|jpeg|png|webp)", photo) or not (ROOT / photo).exists()):
        errs.append(f"商品 {pid}: 写真 {photo} が assets/products/ にない")
    return errs


def check_ads(name: str, body: str, strict: bool = True) -> list[str]:
    """広告の目印を検証する。広告コードの直書きは禁止(コードの書き間違い・捏造を防ぐ)。
    strict=False(公開済み記事のビルド)では、停止・削除した広告は表示しないだけでエラーにしない。
    サービス(最大3種類・文字リンク3・バナー1)と商品(別枠で最大3つ、同じ商品は1回)は別に数える。"""
    errs = []
    ads = load_ads(active_only=True)
    count = {"text": 0, "banner": 0}
    per = {}
    products: dict[str, int] = {}
    for line in body.splitlines():
        for m in AD_RE.finditer(line):
            if line.strip() != m.group(0):
                errs.append(f"{name}: 広告の目印 {m.group(0)} は1行に単独で置く")
            kind = m.group(2) or "text"
            if m.group(1) not in ads:
                if strict:
                    errs.append(f"{name}: 広告 {m.group(1)} は使えません(登録が無いか停止中)")
                continue
            if ad_type(ads[m.group(1)]) == "product":
                if kind == "banner":
                    errs.append(f"{name}: 商品 {m.group(1)} にバナーはない。[[ad:{m.group(1)}]] で置く")
                else:
                    errs += [f"{name}: {e}" for e in product_errs(m.group(1), ads[m.group(1)])]
                products[m.group(1)] = products.get(m.group(1), 0) + 1
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
    if sum(products.values()) > AD_PRODUCTS:
        errs.append(f"{name}: 商品リンクが{sum(products.values())}個。{AD_PRODUCTS}個まで")
    for n, c in products.items():
        if c > 1:
            errs.append(f"{name}: 商品 {n} が{c}回。同じ商品は1回まで")
    if re.search(r"a8\.net|a8mat=|<a\s[^>]*rel=\"?nofollow|msmaflink|moshimo", body, re.I):
        errs.append(f"{name}: 広告コードを本文に直接書かない。[[ad:...]] の目印を使う")
    return errs


def product_box(pid: str, ad: dict, prefix: str = "../") -> str:
    """商品ボックス。Amazon(主)・楽天(副)のボタンを build が作る。リンクが無ければ登録したコード(かんたんリンクなど)をそのまま出す。"""
    name = esc(str(ad.get("name") or pid))
    photo = str(ad.get("photo") or "")
    img = (f'<img class="ad-product-photo" src="{prefix}{esc(photo)}" alt="{name}(筆者が撮影した実物)" loading="lazy">'
           if photo and (ROOT / photo).exists() else "")
    cap = f'<p class="ad-product-caption">{esc(str(ad["caption"]))}</p>' if ad.get("caption") else ""
    btns = []
    for k, cls, text in (("amazon_url", "shop-amazon", "Amazon で見る"), ("rakuten_url", "shop-rakuten", "楽天で見る")):
        if ad.get(k) and not shop_url_err(k, ad[k]):
            btns.append(f'<a class="shop-btn {cls}" href="{esc(str(ad[k]).strip())}" target="_blank" rel="{SHOP_LINK_REL}">{text}</a>')
    if btns:
        links = f'<div class="ad-product-shops">{"".join(btns)}</div>'
    elif ad.get("code"):
        links = str(ad["code"]).strip()
    else:
        return ""
    head = f'<div class="ad-product-head">{img}<div><p class="ad-product-name">{name}</p>{cap}</div></div>' if btns else ""
    return f'\n<div class="ad-product">\n<p class="ad-product-label">{PRODUCT_LABEL}</p>\n{head}\n{links}\n</div>\n'


def expand_ads(body_md: str) -> tuple[str, bool]:
    """目印を広告コードに置き換える。停止・削除した広告の目印は消す。"""
    ads = load_ads(active_only=True)
    used = False

    def rep(m: re.Match) -> str:
        nonlocal used
        kind = m.group(2) or "text"
        ad = ads.get(m.group(1)) or {}
        if ad and ad_type(ad) == "product":  # 商品はボックスで出す
            box = "" if kind == "banner" else product_box(m.group(1), ad)
            if not box:
                print(f"注意: 商品 {m.group(0)} はリンクが無いので表示しません", file=sys.stderr)
                return ""
            used = True
            return box
        code = ad.get(kind)
        if not code:
            print(f"注意: 広告 {m.group(0)} は停止・削除済みなので表示しません", file=sys.stderr)
            return ""
        used = True
        return f'\n<div class="ad-{kind}">\n{code.strip()}\n</div>\n'

    return AD_RE.sub(rep, body_md), used


def webp_meta_err(data: bytes) -> str:
    """WebP にメタデータ(EXIF・XMP・ICC)が入っていないか。入っていれば理由を返す。"""
    import struct
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return "WebP ではない"
    i = 12
    while i + 8 <= len(data):
        tag, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        if tag not in WEBP_OK_CHUNKS:
            return f"メタデータらしきチャンク {tag!r} がある"
        if tag == b"VP8X" and data[i + 8] & 0x2C:
            return "ICC・EXIF・XMP のフラグが立っている"
        i += 8 + size + (size & 1)
    return ""


def photo_errs(a: dict, section: str) -> list[str]:
    """本文の写真の目印と、front matter の photos、画像ファイルが合っているか。"""
    errs = []
    name = a["slug"]
    ph = a.get("photos") or {}
    if ph and not isinstance(ph, dict):
        return [f"{name}: photos は p1: {{alt: ..., width: ..., height: ...}} の形"]
    found = PHOTO_RE.findall(a.get("body_md", ""))
    for pid in found:
        e = ph.get(pid)
        if not isinstance(e, dict) or not str(e.get("alt") or "").strip():
            errs.append(f"{name}: 写真 {pid} の alt が front matter の photos に無い")
            continue
        if not all(isinstance(e.get(k), int) and e[k] > 0 for k in ("width", "height")):
            errs.append(f"{name}: 写真 {pid} の width・height が無い")
        if e.get("src") and not PHOTO_SRC_RE.match(str(e["src"])):
            errs.append(f"{name}: 写真 {pid} の src は assets/photos/<articles|lab>/<slug>/<pN>.webp の形")
            continue
        f = ROOT / (str(e["src"]) if e.get("src") else f"assets/photos/{section}/{name}/{pid}.webp")
        if not f.exists():
            errs.append(f"{name}: 写真 {pid} の画像 {f.relative_to(ROOT)} が無い")
            continue
        data = f.read_bytes()
        if len(data) > PHOTO_MAX_BYTES:
            errs.append(f"{name}: 写真 {pid} が {len(data):,} バイト。{PHOTO_MAX_BYTES:,} バイトまで")
        if (why := webp_meta_err(data)):
            errs.append(f"{name}: 写真 {pid}: {why}(メタデータを消した WebP だけを置く)")
    for pid in ph:
        if pid not in found:
            errs.append(f"{name}: front matter の写真 {pid} の目印 [[photo:{pid}]] が本文に無い")
    return errs


def expand_photos(body_md: str, a: dict, section: str, prefix: str = "../") -> str:
    """目印 [[photo:p1]] を figure(img に alt・幅・高さ・遅延読み込み)にする。"""
    ph = a.get("photos") or {}

    def rep(m: re.Match) -> str:
        e = ph.get(m.group(1)) or {}
        src = prefix + (str(e["src"]) if e.get("src") and PHOTO_SRC_RE.match(str(e["src"]))
                        else f"assets/photos/{section}/{a['slug']}/{m.group(1)}.webp")
        alt = esc(str(e.get("alt", "")))
        cls = "article-photo article-figure" if m.group(1).startswith("f") else "article-photo"  # 図解は枠線なし(図の中に枠がある)
        return (f'<figure class="{cls}"><img src="{src}" alt="{alt}" width="{int(e.get("width") or 0)}" '
                f'height="{int(e.get("height") or 0)}" loading="lazy" decoding="async"></figure>')
    return PHOTO_RE.sub(rep, body_md)


def secret_errs(a: dict) -> list[str]:
    """IP アドレス・トークン・メールアドレスらしき文字列(tools/secret_scan.py)。記事には載せない。"""
    import secret_scan
    text = "\n".join(str(a.get(k, "")) for k in ["title", "description", "summary", "worked_on", "body_md"]) \
        + "\n" + "\n".join(map(str, a.get("environment") or [])) \
        + "\n" + "\n".join(str((v or {}).get("alt", "")) for v in (a.get("photos") or {}).values() if isinstance(v, dict))
    return [f"{a['slug']}: {kind}「{secret_scan.mask(hit)}」を載せない" for _, kind, hit in secret_scan.scan_text(text)]


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
    if LINE_LINK_RE.search(body):
        errs.append(f"{name}: LINE への誘導はテンプレートが入れるので、本文に LINE のリンクを書かない")
    if a.get("line_theme") and a["line_theme"] not in LINE_THEMES:
        errs.append(f"{name}: line_theme は {'/'.join(LINE_THEMES)} のどれか")
    h2 = a.get("body_md", "").count("\n## ") + a.get("body_md", "").startswith("## ")
    if not (3 <= h2 <= 7):
        errs.append(f"{name}: 見出し(##)が{h2}個。4〜6個にする")
    errs += secret_errs(a)
    errs += photo_errs(a, "articles")
    return errs


def validate_lab(a: dict) -> list[str]:
    """しろくまラボの記事。作業時期と動作確認した環境が必須。広告は使った機材の商品リンクだけ。"""
    errs = []
    name = a["slug"]
    if not SLUG_RE.match(name):
        errs.append(f"lab/{name}: slugは半角英小文字・数字・ハイフンのみ")
    for k in ["title", "description", "summary", "worked_on", "environment", "date"]:
        if not a.get(k):
            errs.append(f"lab/{name}: front matter に {k} がありません")
    env = a.get("environment")
    if env and not (isinstance(env, list) and all(isinstance(x, str) and x.strip() for x in env)):
        errs.append(f"lab/{name}: environment は「- Raspberry Pi 4(8GB)」のような箇条書き(文字列のリスト)")
    if a.get("date"):
        try:
            a["date"] = date.fromisoformat(str(a["date"]))
        except ValueError:
            errs.append(f"lab/{name}: date は YYYY-MM-DD")
    if len(a.get("title", "")) > 48:
        errs.append(f"lab/{name}: title が長すぎます(48字以内)")
    if not (50 <= len(a.get("description", "")) <= 160):
        errs.append(f"lab/{name}: description は50〜160字")
    body = a.get("body_md", "")
    joined = " ".join(str(a.get(k, "")) for k in ["title", "description", "summary", "worked_on", "body_md"])
    for b in BANNED:
        if b in joined:
            errs.append(f"lab/{name}: 禁止文字 {b!r} が含まれています")
    # 広告は「使った機材」の商品リンク(type: product)だけ。転職サービスなどの広告は置かない
    ads = load_ads(active_only=True)
    for m in AD_RE.finditer(body):
        if m.group(1) in ads and ad_type(ads[m.group(1)]) != "product":
            errs.append(f"lab/{name}: しろくまラボに置けるのは使った機材の商品リンクだけ({m.group(1)} はサービスの広告)")
    errs += [e.replace(f"{name}: ", f"lab/{name}: ", 1) for e in check_ads(name, body, strict=False)]
    if LINE_LINK_RE.search(body):
        errs.append(f"lab/{name}: LINE への誘導はテンプレートが入れるので、本文に LINE のリンクを書かない")
    if "<script" in body.lower():
        errs.append(f"lab/{name}: 本文に<script>は使えません")
    plain = len(re.sub(r"[#*\[\]()\-\s`]", "", body))
    if not (1500 <= plain <= 10000):
        errs.append(f"lab/{name}: 本文が{plain}字。3,000〜6,000字を目安に収める")
    h2 = body.count("\n## ") + body.startswith("## ")
    if not (3 <= h2 <= 10):
        errs.append(f"lab/{name}: 見出し(##)が{h2}個。7〜8個にする(記事の型)")
    errs += [e.replace(f"{name}: ", f"lab/{name}: ", 1) for e in secret_errs(a) + photo_errs(a, "lab")]
    return errs


def load_lab() -> list[dict]:
    """しろくまラボの記事(content/lab/*.md)。公開日が来たものだけ、新しい順。"""
    md = [parse_md(p) for p in sorted((ROOT / "content/lab").glob("*.md"))]
    errs = [e for a in md for e in validate_lab(a)]
    if errs:
        raise BuildError("\n".join(errs))
    today = date.today()
    published = [a for a in md if a["date"] <= today and not a.get("draft")]
    published.sort(key=lambda a: (a["date"], a["slug"]), reverse=True)
    return published


def load_all() -> list[dict]:
    legacy = yaml.safe_load((ROOT / "content/legacy.yml").read_text(encoding="utf-8"))["articles"]
    for a in legacy:
        a["legacy"] = True
        if not (ROOT / "articles" / f"{a['slug']}.html").exists():
            raise BuildError(f"legacy記事 {a['slug']}.html が articles/ にありません")
    md = [parse_md(p) for p in sorted((ROOT / "content/articles").glob("*.md"))]
    errs = [e for a in md for e in validate(a)]
    errs += [e for pid, ad in load_ads(active_only=True).items() if ad_type(ad) == "product" for e in product_errs(pid, ad)]
    errs += check_line(load_line())
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


def list_titles(exclude: str = "", section: str = "articles") -> list[tuple[str, str]]:
    """検証せずに (slug, title) を集める。生成途中の壊れた下書きがあっても落ちない。
    section="lab" ならしろくまラボ(content/lab)の記事。"""
    if section == "lab":
        out = []
    else:
        legacy = yaml.safe_load((ROOT / "content/legacy.yml").read_text(encoding="utf-8"))["articles"]
        out = [(a["slug"], a["title"]) for a in legacy]
    for p in sorted((ROOT / "content" / section).glob("*.md")):
        if p.stem == exclude:
            continue
        try:
            out.append((p.stem, str(parse_md(p).get("title", p.stem))))
        except (BuildError, yaml.YAMLError):
            pass
    return out


# ---------- レンダリング ----------

def render_article(a: dict, all_slugs: set[str]) -> str:
    body_md, _ = expand_ads(expand_photos(a["body_md"], a, "articles"))
    body = markdown.markdown(body_md, extensions=["tables", "sane_lists"])
    aff = affiliate_kinds(body)  # PR 表記と Amazon の表記は、本文に実際にあるリンクで決める
    has_ad, amazon = bool(aff), "amazon" in aff
    for target in re.findall(r'href="([a-z0-9-]+)\.html"', body):
        if target not in all_slugs:
            raise BuildError(f"{a['slug']}: 内部リンク先 {target}.html が存在しません")
    url = f"{BASE}/articles/{a['slug']}.html"
    og = a.get("og_description") or a["description"]
    # OGP 画像(本社の publish が assets/og/<slug>.png に置く)。無い記事は文字だけのカード
    has_img = (ROOT / "assets" / "og" / f"{a['slug']}.png").exists()
    img = f"{BASE}/assets/og/{a['slug']}.png"
    og_img = (f'<meta property="og:image" content="{img}">\n<meta property="og:image:width" content="1200">\n'
              f'<meta property="og:image:height" content="628">\n<meta name="twitter:image" content="{img}">\n') if has_img else ""
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
{og_img}<meta name="twitter:card" content="{'summary_large_image' if has_img else 'summary'}">
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
    {line_cta(line_theme(a))}
    {f'<p class="amazon-note">{AMAZON_NOTE}</p>' if amazon else ''}
  </div>
</main>

{FOOTER.format(p="../", year=date.today().year)}

</body>
</html>
"""


def lab_head(title: str, desc: str, url: str, og_type: str = "article", slug: str = "") -> str:
    """しろくまラボのページの head。slug を渡すと、本社の publish が置いた OGP 画像(assets/og/lab/<slug>.png)があれば出す。"""
    t = esc(title)
    has_img = bool(slug) and (ROOT / "assets" / "og" / "lab" / f"{slug}.png").exists()
    img = f"{BASE}/assets/og/lab/{slug}.png"
    og_img = (f'<meta property="og:image" content="{img}">\n<meta property="og:image:width" content="1200">\n'
              f'<meta property="og:image:height" content="628">\n<meta name="twitter:image" content="{img}">\n') if has_img else ""
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="{og_type}">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{url}">
<meta property="og:site_name" content="しろくま会計">
<meta property="og:locale" content="ja_JP">
{og_img}<meta name="twitter:card" content="{'summary_large_image' if has_img else 'summary'}">
<link rel="stylesheet" href="../assets/style.css">"""


def render_lab_article(a: dict, lab_slugs: set[str], article_slugs: set[str]) -> str:
    body_md, _ = expand_ads(expand_photos(a["body_md"], a, "lab"))  # 使った機材(商品リンク)だけ
    body = markdown.markdown(body_md, extensions=["tables", "sane_lists", "fenced_code"])
    aff = affiliate_kinds(body)  # PR 表記と Amazon の表記は、本文に実際にあるリンクで決める
    has_ad, amazon = bool(aff), "amazon" in aff
    for target in re.findall(r'href="([a-z0-9-]+)\.html"', body):
        if target not in lab_slugs and target != "index":
            raise BuildError(f"lab/{a['slug']}: 内部リンク先 {target}.html がしろくまラボにありません")
    for target in re.findall(r'href="\.\./articles/([a-z0-9-]+)\.html"', body):
        if target not in article_slugs and target != "index":
            raise BuildError(f"lab/{a['slug']}: 内部リンク先 ../articles/{target}.html が存在しません")
    url = f"{BASE}/lab/{a['slug']}.html"
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "TechArticle",
                "headline": a["title"],
                "description": a["description"],
                "datePublished": a["date"].isoformat(),
                "dateModified": str(a.get("updated") or a["date"]),
                "author": {"@type": "Person", "name": "しろくま", "url": f"{BASE}/about.html"},
                "publisher": {"@type": "Organization", "name": "しろくま会計", "url": f"{BASE}/"},
                "mainEntityOfPage": {"@type": "WebPage", "@id": url},
                "inLanguage": "ja",
                "proficiencyLevel": "Beginner",
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "ホーム", "item": f"{BASE}/"},
                    {"@type": "ListItem", "position": 2, "name": LAB_NAME, "item": f"{BASE}/lab/index.html"},
                    {"@type": "ListItem", "position": 3, "name": a["title"]},
                ],
            },
        ],
    }
    t = esc(a["title"])
    env = "".join(f"<li>{esc(str(x))}</li>" for x in a["environment"])
    return f"""{lab_head(f"{a['title']} | {LAB_NAME} | しろくま会計", a["description"], url, slug=a["slug"])}
<script type="application/ld+json">
{json.dumps(ld, ensure_ascii=False, indent=2)}
</script>
</head>
<body>

{HEADER.format(p="../")}

<section class="hero">
  <div class="wrap">
    <nav class="breadcrumb" aria-label="パンくず"><a href="../index.html">ホーム</a> ／ <a href="index.html">{LAB_NAME}</a> ／ <span>{t}</span></nav>
    <h1>{t}</h1>
  </div>
</section>

<main>
  <div class="wrap">
    {PR_NOTE if has_ad else ""}
    <div class="note lab-env">
      <p><strong>作業時期</strong> {esc(str(a["worked_on"]))}</p>
      <p><strong>動作確認した環境</strong></p>
      <ul>{env}</ul>
    </div>

{body}
    {line_cta("it", "lab")}
    {f'<p class="amazon-note">{AMAZON_NOTE}</p>' if amazon else ''}

    <div class="note lab-disclaimer">
      <p><strong>免責</strong></p>
      <p>{LAB_DISCLAIMER}</p>
    </div>
    <div class="note">
      <p><strong>この記事を書いた人</strong></p>
      <p>{LAB_AUTHOR_NOTE}</p>
    </div>
  </div>
</main>

{FOOTER.format(p="../", year=date.today().year)}

</body>
</html>
"""


def render_lab_index(lab: list[dict]) -> str:
    url = f"{BASE}/lab/index.html"
    lis = "\n".join(
        f"""      <li>
        <a href="{a['slug']}.html">{esc(a['title'])}</a><br>
        {esc(a['summary'])}<br>
        <span class="lab-meta">作業時期: {esc(str(a['worked_on']))}</span>
      </li>""" for a in lab)
    body = f"    <ul>\n{lis}\n    </ul>" if lab else "    <p>記事は準備中です。</p>"
    return f"""{lab_head(f"{LAB_NAME} | しろくま会計", LAB_LEAD, url, "website")}
</head>
<body>

{HEADER.format(p="../")}

<section class="hero">
  <div class="wrap">
    <nav class="breadcrumb" aria-label="パンくず"><a href="../index.html">ホーム</a> ／ <span>{LAB_NAME}</span></nav>
    <h1>{LAB_NAME}</h1>
    <p class="lead">{LAB_LEAD}</p>
  </div>
</section>

<main>
  <div class="wrap">
    <p>どの記事も、作業した時期と、動作を確かめた機器・ソフトのバージョンを書いています。手順を試すときは、ご自身の環境との違いに気をつけてください。会計の記事は<a href="../articles/index.html">記事一覧</a>からどうぞ。</p>

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


def render_sitemap(articles: list[dict], lab: list[dict] | None = None) -> str:
    rows = [
        (f"{BASE}/", "weekly", "1.0", None),
        (f"{BASE}/articles/index.html", "weekly", "0.9", None),
    ]
    for a in articles:
        lm = a["date"].isoformat() if not a["legacy"] else None
        rows.append((f"{BASE}/articles/{a['slug']}.html", "monthly", "0.8", lm))
    rows.append((f"{BASE}/lab/index.html", "monthly", "0.5", None))
    for a in lab or []:
        rows.append((f"{BASE}/lab/{a['slug']}.html", "yearly", "0.5", a["date"].isoformat()))
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
    lab = load_lab()
    lab_slugs = {a["slug"] for a in lab}
    for a in lab:
        rendered[f"lab/{a['slug']}.html"] = render_lab_article(a, lab_slugs, slugs)
    rendered["lab/index.html"] = render_lab_index(lab)
    rendered["sitemap.xml"] = render_sitemap(articles, lab)

    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT, out, ignore=lambda d, names: [n for n in names if Path(d) == ROOT and n in EXCLUDE])
    for a in articles:  # 手書きの記事にも LINE の誘導を入れる(リポジトリの元ファイルは書き換えない)
        if a["legacy"]:
            rendered[f"articles/{a['slug']}.html"] = add_line_to_legacy(
                (ROOT / "articles" / f"{a['slug']}.html").read_text(encoding="utf-8"), line_theme(a))
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
                else:
                    add_line_to_legacy((ROOT / "articles" / f"{a['slug']}.html").read_text(encoding="utf-8"), line_theme(a))
            lab = load_lab()
            for a in lab:
                render_lab_article(a, {x["slug"] for x in lab}, slugs)
            print(f"OK: {len(articles)}記事、{LAB_NAME} {len(lab)}記事")
        else:
            articles = build(Path(args.out))
            print(f"built {len(articles)} articles -> {args.out}")
    except BuildError as e:
        print(f"BUILD ERROR\n{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
