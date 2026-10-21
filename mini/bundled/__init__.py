"""Bundled document tools (PDF, docx, xlsx): seed tools in the same git-backed format as user tools.

One directory per tool, as the creator would write it: tool.py, test_tool.py and manifest.json (the spec; install()
fills in the version and the rest). They use the standard library only (docx and xlsx are zip + XML; PDF text is read
with zlib and a small parser), so they run in the stock sandbox image without packages or network.

Registry.seed_bundled() installs them on first run, and again as a new version when their files change. Their names
are reserved: Registry.install() refuses a tool of that name unless its manifest carries the digest of the bundled
files, and the creator refuses to build or repair one.
"""
import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def names():
    return sorted(n for n in os.listdir(HERE) if os.path.isfile(os.path.join(HERE, n, "manifest.json")))


def directory(name):
    return os.path.join(HERE, name)


def digest(name):
    """Fingerprint of a bundled tool's files: what a manifest must carry to claim a reserved name."""
    h = hashlib.sha256()
    d = directory(name)
    for f in sorted(x for x in os.listdir(d) if x != "__pycache__"):
        h.update(f.encode() + b"\0" + open(os.path.join(d, f), "rb").read() + b"\0")
    return h.hexdigest()[:16]


def manifest(name):
    with open(os.path.join(directory(name), "manifest.json")) as f:
        m = json.load(f)
    m["files"] = sorted(x for x in os.listdir(directory(name)) if x not in ("manifest.json", "__pycache__"))
    m["bundled"] = digest(name)
    return m
