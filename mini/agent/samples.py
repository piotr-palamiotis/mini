"""The task's real input files, for building a tool: a short preview for the spec step and small samples the
tests read, so a tool is designed and tested on the data it will get instead of data the model typed from memory."""
import os
import re
import struct
import zipfile

MAX_FILES = 6
MAX_FILE = 64 * 1024        # a file this size or smaller is copied whole
MAX_TOTAL = 256 * 1024      # all samples of one tool together
LINE_FILES = (".csv", ".tsv", ".txt", ".log", ".jsonl", ".ndjson")   # a longer one keeps its first lines
KEEP_LINES = 40
PREVIEW_LINES = 12
PREVIEW_CHARS = 1200


def is_text(path):
    with open(path, "rb") as f:
        head = f.read(4096)
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        return e.start > len(head) - 4       # a multi-byte character cut at the end of the read is still text
    return True


def _inputs(agent):
    """At most MAX_FILES inputs, one of each kind (folder and extension) first: six configs from one folder must
    not crowd out the schema they are checked against."""
    names = sorted(getattr(agent, "task_inputs", None) or ())
    kind = lambda p: (os.path.dirname(p), os.path.splitext(p)[1].lower())
    first, rest, kinds = [], [], set()
    for p in names:
        (rest if kind(p) in kinds else first).append(p)
        kinds.add(kind(p))
    return sorted((first + rest)[:MAX_FILES])


def _exif_dates(tiff):
    """DateTimeOriginal (date taken) and DateTime from a TIFF/EXIF block, as {name: value}."""
    if tiff[:2] not in (b"II", b"MM"):
        return {}
    e = "<" if tiff[:2] == b"II" else ">"

    def ifd(off):
        tags = {}
        if off + 2 > len(tiff):
            return tags
        for i in range(struct.unpack_from(e + "H", tiff, off)[0]):
            at = off + 2 + 12 * i
            if at + 12 > len(tiff):
                break
            tag, typ, count, val = struct.unpack_from(e + "HHII", tiff, at)
            if typ == 2 and count > 4:
                tags[tag] = tiff[val:val + count].rstrip(b"\0").decode("ascii", "replace")
            elif typ == 4 or typ == 13:
                tags[tag] = val
        return tags
    top = ifd(struct.unpack_from(e + "I", tiff, 4)[0])
    sub = ifd(top[0x8769]) if isinstance(top.get(0x8769), int) else {}
    out = {}
    if sub.get(0x9003):
        out["DateTimeOriginal"] = sub[0x9003]
    if top.get(0x0132):
        out["DateTime"] = top[0x0132]
    return out


def _jpeg(data):
    size, dates, i = None, {}, 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        mark, n = data[i + 1], struct.unpack_from(">H", data, i + 2)[0]
        if mark == 0xE1 and data[i + 4:i + 10] == b"Exif\0\0":
            dates = _exif_dates(data[i + 10:i + 2 + n])
        if 0xC0 <= mark <= 0xCF and mark not in (0xC4, 0xC8, 0xCC) and i + 9 <= len(data):
            h, w = struct.unpack_from(">HH", data, i + 5)
            size = (w, h)
            break
        i += 2 + n
    return size, dates


def describe(full):
    """What a binary file is, read with the standard library: an image's format, size and EXIF dates; an archive's
    entries (a workbook's sheets). "" when it is none of these."""
    try:
        with open(full, "rb") as f:
            data = f.read(1 << 20)
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack_from(">II", data, 16)
            return f"PNG image, {w}x{h}"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack_from("<HH", data, 6)
            return f"GIF image, {w}x{h}"
        if data[:2] == b"\xff\xd8":
            size, dates = _jpeg(data)
            if not size:
                return "JPEG image"
            ex = "; ".join(f"EXIF {k} {v}" for k, v in dates.items()) or "no EXIF date"
            return f"JPEG image, {size[0]}x{size[1]}, {ex}"
        if data[:4] == b"PK\x03\x04" and zipfile.is_zipfile(full):
            with zipfile.ZipFile(full) as z:
                names = z.namelist()
                if "xl/workbook.xml" in names:
                    sheets = re.findall(r'<sheet\b[^>]*\bname="([^"]*)"', z.read("xl/workbook.xml").decode("utf-8", "replace"))
                    return f"Excel workbook, sheets: {', '.join(sheets)}"
            shown = ", ".join(names[:30]) + (f", ... ({len(names)} entries)" if len(names) > 30 else "")
            return f"zip archive holding: {shown}"
    except (OSError, struct.error, zipfile.BadZipFile, KeyError, ValueError):
        pass
    return ""


def head(folder, rel):
    """The first lines of a file in the folder (text), or its size and what it is (binary); "" when it is not a
    file."""
    full = os.path.join(folder, rel)
    if not os.path.isfile(full):
        return ""
    size = os.path.getsize(full)
    if not is_text(full):
        what = describe(full)
        return f"{rel} ({size} bytes, binary" + (f": {what})" if what else ")")
    with open(full, encoding="utf-8", errors="replace") as f:
        lines = [ln for _, ln in zip(range(PREVIEW_LINES), f)]
    text = "".join(lines)[:PREVIEW_CHARS]
    more = " (first lines)" if size > len(text.encode()) else ""
    return f"{rel} ({size} bytes){more}:\n```\n{text.rstrip()}\n```"


def preview(agent):
    """The first lines of each input file (text), or its size and type (binary)."""
    out = [h for h in (head(agent.folder, rel) for rel in _inputs(agent)) if h]
    if not out:
        return ""
    return ("The task's input files as they are (design the inputs, columns and fields from these, not from a "
            "guess):\n\n" + "\n\n".join(out))


def copy(agent, files_dir):
    """Copy small samples of the input files into files_dir/samples/<path>. Returns [(path, bytes)] copied."""
    done, total = [], 0
    for rel in _inputs(agent):
        full = os.path.join(agent.folder, rel)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        dest = os.path.join(files_dir, "samples", rel)
        if os.path.exists(dest):
            continue
        size = os.path.getsize(full)
        if size > MAX_FILE:
            if not (rel.lower().endswith(LINE_FILES) and is_text(full)):
                continue
            with open(full, "rb") as f:
                data = b"".join(ln for _, ln in zip(range(KEEP_LINES), f))
        else:
            with open(full, "rb") as f:
                data = f.read()
        if total + len(data) > MAX_TOTAL:
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        total += len(data)
        done.append((rel, len(data)))
    return done


def listing(files_dir):
    """[(path, bytes)] of the samples in a tool's files."""
    root = os.path.join(files_dir, "samples")
    out = []
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            out.append((os.path.relpath(p, root), os.path.getsize(p)))
    return sorted(out)


def retyped(files_dir, test_text, window=32, need=3):
    """Samples test_text holds a copy of: `need` or more stretches of `window` characters from the start of the file
    appear in it. One stretch is not enough, since an XML declaration or a CSV header is shared by any made-up input."""
    out = []
    for p, _ in listing(files_dir):
        full = os.path.join(files_dir, "samples", p)
        if not is_text(full):
            continue
        text = open(full, encoding="utf-8", errors="replace").read(800)
        hits = sum(1 for i in range(0, max(len(text) - window, 0) + 1, window // 2) if text[i:i + window] in test_text)
        if hits >= need:
            out.append(p)
    return out


SHOW_CHARS = 4000            # sample text shown whole to the test step, all files together


def tests_note(files_dir):
    got = listing(files_dir)
    if not got:
        return ""
    names = ", ".join(f"samples/{p}" for p, _ in got)
    shown, left = [], SHOW_CHARS
    for p, n in got:
        full = os.path.join(files_dir, "samples", p)
        if n <= left and is_text(full):
            shown.append(f"samples/{p}:\n```\n{open(full, encoding='utf-8', errors='replace').read().rstrip()}\n```")
            left -= n
    body = ("\n\n" + "\n\n".join(shown)) if shown else ""
    return (f" Real samples of the task's input files are next to tool.py: {names} (a long line-based file keeps its "
            f"first {KEEP_LINES} lines). When this tool reads files like these, the normal-use test reads them, e.g. "
            f"`os.path.join(os.path.dirname(__file__), \"samples\", ...)`, copies what it needs into tmp_path, and "
            f"asserts the values they hold: the count per file and concrete values from each file (a name, a "
            f"title, a number). `len(x) > 0` or `\"key\" in result` alone tests nothing. Do not type sample data "
            f"of your own when a real sample exists.{body}")
