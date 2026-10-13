"""公開してはいけない文字列(IP アドレス・トークン・メールアドレスなど)が入っていないかを調べる。

    python tools/secret_scan.py            # リポジトリの全ファイル(git ls-files)
    python tools/secret_scan.py a.md b.md  # 指定したファイルだけ

見つかったら場所を出して終了コード 1。PR ごとと main への push で .github/workflows/secret-scan.yml が動かす。
build.py も記事(content/**/*.md)をこれで調べ、引っかかった記事は公開しない。
本社リポジトリの office.py も下書きの検証でこれを使う(公開リポジトリを site/ に置いたとき)。

例として書いてよいもの:
- IP アドレスは 192.168.x.x のように x で伏せる。数字で書くなら文書用のアドレス(192.0.2.x / 198.51.100.x / 203.0.113.x)
- メールアドレスは example.com / example.jp / example.org のもの
誤検知で、どうしても載せたい文字列は tools/secret_scan_allow.txt に1行ずつ書く(公開サイトには出ない)。
「ファイルのパス 文字列」と書くと、そのファイルだけで許す(お問い合わせページのメールアドレスなど)。
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW_FILE = Path(__file__).resolve().parent / "secret_scan_allow.txt"
TEXT_EXT = {".md", ".html", ".htm", ".yml", ".yaml", ".txt", ".js", ".mjs", ".css", ".json", ".xml", ".py", ".sh", ".toml", ".csv"}

IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]*\d)(?:/\d{1,2})?")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
TOKEN_RES = [
    ("GitHub のトークン", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("Anthropic / OpenAI の API キー", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}")),
    ("Slack のトークン", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Slack の Webhook URL", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("AWS のアクセスキー", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google の API キー", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Tailscale の認証キー", re.compile(r"\btskey-[A-Za-z0-9-]{10,}")),
    ("Tailscale のマシン名(tailnet)", re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b", re.I)),
    ("秘密鍵", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
]
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
OK_IPS = {"0.0.0.0", "127.0.0.1", "255.255.255.255"}
OK_EMAIL_DOMAINS = re.compile(r"@(?:[\w-]+\.)*(?:example\.(?:com|jp|org|net)|users\.noreply\.github\.com)$", re.I)


def allowlist(path: str = "") -> set[str]:
    """許す文字列。path を渡すと、そのファイルだけで許すもの(「パス 文字列」の行)も含める。"""
    if not ALLOW_FILE.exists():
        return set()
    out = set()
    for ln in ALLOW_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split(None, 1)
        if len(parts) == 1:
            out.add(parts[0])
        elif path and parts[0] == path:
            out.add(parts[1].strip())
    return out


def _ip_ok(s: str) -> bool:
    addr = s.split("/")[0]
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True  # 999.1.1.1 のような IP でない数字の並び
    if addr in OK_IPS or str(ip).startswith("255.255."):  # サブネットマスク
        return True
    if "/" in s:  # 10.0.0.0/8 のような広い範囲の書き方は、家の機器やネットワークを指さないので許す(/24 などは伏せる)
        try:
            return ipaddress.ip_network(s, strict=True).prefixlen <= 16
        except ValueError:
            return False
    return any(ip in n for n in DOC_NETS)


def scan_text(text: str, allow: set[str] | None = None) -> list[tuple[int, str, str]]:
    """(行番号, 種類, 見つかった文字列) の一覧。"""
    allow = allowlist() if allow is None else allow
    hits = []
    for no, line in enumerate(text.splitlines(), 1):
        for m in IPV4_RE.finditer(line):
            if not _ip_ok(m.group(0)) and m.group(0) not in allow:
                hits.append((no, "IP アドレス(192.168.x.x のように伏せる)", m.group(0)))
        for m in EMAIL_RE.finditer(line):
            if not OK_EMAIL_DOMAINS.search(m.group(0)) and m.group(0) not in allow:
                hits.append((no, "メールアドレス(例は example.com にする)", m.group(0)))
        for kind, rx in TOKEN_RES:
            for m in rx.finditer(line):
                if m.group(0) not in allow:
                    hits.append((no, kind, m.group(0)))
    return hits


def mask(s: str) -> str:
    """ログに秘密をそのまま出さない。先頭だけ見せる。"""
    return s if len(s) <= 8 else s[:6] + "…"


def repo_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / p for p in out.splitlines()]


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] if argv else repo_files()
    found = 0
    for p in files:
        if p.suffix.lower() not in TEXT_EXT or not p.is_file() or p.resolve() == ALLOW_FILE:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = p.resolve().relative_to(ROOT) if p.resolve().is_relative_to(ROOT) else p
        for no, kind, s in scan_text(text, allowlist(str(rel))):
            print(f"{rel}:{no}: {kind}: {mask(s)}")
            found += 1
    if found:
        print(f"\n公開してはいけない文字列らしきものが {found} 件あります。伏せるか消してください。"
              "誤検知なら tools/secret_scan_allow.txt に足す。", file=sys.stderr)
        return 1
    print("OK: 秘密情報らしき文字列はありません")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
