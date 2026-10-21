"""Fetch a URL from an approved host."""
import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from ._files import target
from .files_read import MAX_READ
from ..harness.sandbox import PolicyError

NAME = "web_fetch"
MAX_BYTES = 2 * 1024 * 1024
DESCRIPTION = ("Fetch a web page or file over http(s). The host must be approved ([web] hosts in the config, or the "
               "user approves it for this task). Pages come back as text, in parts: offset is the character to start "
               "at. save_as: also save the response, unchanged, to that path in the user's folder.")
SCHEMA = {"type": "object", "required": ["url"], "properties": {
    "url": {"type": "string"}, "offset": {"type": "integer"}, "save_as": {"type": "string"}}}
COVERS = ("fetch url", "download url", "fetch web page", "download file from url", "get url", "open url",
          "fetch page", "http get")


def approved(a, host):
    """True when the host is in [web] hosts, was approved earlier in this task, or the user approves it now."""
    hosts = {h.lower() for h in a.cfg.get("web", {}).get("hosts") or []}
    if host in hosts or host in a.web_hosts:
        return True
    d = a.gate.request("permissions", f"Fetch from {host}?", {"host": host, "why": "the agent wants to read a page "
                       "from it (web_fetch); approved hosts stay approved for this task"})
    if d.approved:
        a.web_hosts.add(host)
    return d.approved


def as_text(body, ctype):
    m = re.search(r"charset=([\w-]+)", ctype or "")
    try:
        text = body.decode(m.group(1) if m else "utf-8", errors="replace")
    except LookupError:                           # an encoding Python does not know
        text = body.decode("utf-8", errors="replace")
    if "html" in (ctype or "") or text.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
        text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)<(br|/p|/div|/li|/h\d|/tr)\b[^>]*>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        text = "\n".join(" ".join(ln.split()) for ln in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def run(call, args):
    a, url = call.agent, args["url"].strip()
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "url must be an http or https address."
    if not approved(a, u.hostname.lower()):
        return f"{u.hostname} is not approved; the page was not fetched. Do without it, or tell the user."
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mini"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body, ctype = r.read(MAX_BYTES + 1), r.headers.get("Content-Type", "")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"Fetch failed: {e}"
    cut = len(body) > MAX_BYTES
    body = body[:MAX_BYTES]
    a.log.emit("info", text=f"web_fetch {url}: {len(body)} bytes, {ctype}")
    note = ""
    if args.get("save_as"):
        try:
            full, rel = target(a, args["save_as"])

            def write():
                os.makedirs(os.path.dirname(full), exist_ok=True)
                open(full, "wb").write(body)
                return [rel]
            a._change([rel], write)
            note = f"\nSaved to {rel} ({len(body)} bytes{', cut at the size limit' if cut else ''})."
        except (OSError, PolicyError) as e:
            note = f"\nNot saved: {e}"
    if not ctype.startswith(("text/", "application/json", "application/xml")) and "html" not in ctype:
        return f"{ctype or 'unknown type'}, {len(body)} bytes: not text." + note
    text, start = as_text(body, ctype), max(0, args.get("offset") or 0)
    end = start + MAX_READ
    more = f"\n[... characters {start}-{min(end, len(text))} of {len(text)}; web_fetch with offset={end} for the rest]" \
        if end < len(text) else ""
    return text[start:end] + more + note
