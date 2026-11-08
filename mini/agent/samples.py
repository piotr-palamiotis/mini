"""The task's real input files, for building a tool: a short preview for the spec step and small samples the
tests read, so a tool is designed and tested on the data it will get instead of data the model typed from memory."""
import os

MAX_FILES = 6
MAX_FILE = 64 * 1024        # a file this size or smaller is copied whole
MAX_TOTAL = 256 * 1024      # all samples of one tool together
LINE_FILES = (".csv", ".tsv", ".txt", ".log", ".jsonl", ".ndjson")   # a longer one keeps its first lines
KEEP_LINES = 40
PREVIEW_LINES = 12
PREVIEW_CHARS = 1200


def _is_text(path):
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
    return sorted(getattr(agent, "task_inputs", None) or ())[:MAX_FILES]


def preview(agent):
    """The first lines of each input file (text), or its size and type (binary)."""
    out = []
    for rel in _inputs(agent):
        full = os.path.join(agent.folder, rel)
        if not os.path.isfile(full):
            continue
        size = os.path.getsize(full)
        if not _is_text(full):
            out.append(f"{rel} ({size} bytes, binary)")
            continue
        with open(full, encoding="utf-8", errors="replace") as f:
            lines = [ln for _, ln in zip(range(PREVIEW_LINES), f)]
        text = "".join(lines)[:PREVIEW_CHARS]
        more = " (first lines)" if size > len(text.encode()) else ""
        out.append(f"{rel} ({size} bytes){more}:\n```\n{text.rstrip()}\n```")
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
            if not (rel.lower().endswith(LINE_FILES) and _is_text(full)):
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


def tests_note(files_dir):
    got = listing(files_dir)
    if not got:
        return ""
    names = ", ".join(f"samples/{p}" for p, _ in got)
    return (f" Real samples of the task's input files are next to tool.py: {names} (a long line-based file keeps its "
            f"first {KEEP_LINES} lines). The normal-use test reads them, e.g. `os.path.join(os.path.dirname(__file__), "
            f"\"samples\", ...)`, copies what it needs into tmp_path and checks the tool's result on them. Do not type "
            f"sample data of your own when a real sample exists.")
