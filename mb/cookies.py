"""Чтение куки mangabuff: строка Cookie-заголовка или файл cookies.txt (Netscape)."""

from __future__ import annotations

from pathlib import Path

DOMAIN_HINT = "mangabuff"


def parse_netscape_cookies(text: str, domain_hint: str = DOMAIN_HINT) -> str:
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        line = line.rstrip("\n")
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            parts = line.split()
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, _expires, name, value = parts[:7]
        if domain_hint and domain_hint not in domain:
            continue
        pairs[name] = value
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def load_cookie(raw: str = "", cookie_file: str | Path = "") -> str:
    raw = (raw or "").strip()
    if raw and "=" not in raw and Path(raw).expanduser().exists():
        cookie_file, raw = raw, ""
    if raw:
        return raw
    if cookie_file:
        path = Path(cookie_file).expanduser()
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            if "\t" not in text and "Netscape" not in text and "=" in text:
                return text.strip()
            return parse_netscape_cookies(text)
    return ""
