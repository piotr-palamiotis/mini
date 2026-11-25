"""Slash commands: one registry for the core commands and every registry or bundled tool (docs/redesign.md 4b).

Each command is a name, a description and an input schema. Core commands (/model, /new, /attach, /status, /undo,
/registry, /config, /tools, /help) run in the UI: MiniApp.cmd_<name>(args). Tools are shown in kebab-case
(/send-email for send_email) and run in the harness (harness/toolrun.slash), in their ui.mode: form or ask. The agent
neither sees nor runs commands.

parse() turns the prompt text into (command, arguments, problem): `--field value` flags from the schema (kebab or
snake case, `--flag` / `--no-flag` for booleans, several words for an array, JSON for an object) and, for a core
command that has one, positional words (`/model rtx3070`, `/attach notes.txt`, `/undo RUN FILE...`).

complete() is the autocomplete when `/` is the first character: command names with descriptions filtered as you
type, then `--field` flags and enum values of the chosen command, then file paths relative to the project folder
for file arguments and /attach.
"""
import fnmatch
import json
import os
import shlex

from ..harness import toolui

SKIP_DIRS = {".git", ".mini", "__pycache__", "node_modules", ".venv"}
MAX_PATHS = 40


def kebab(name):
    return name.replace("_", "-")


class Command:
    """name (kebab, without the slash), description, schema (JSON Schema object of its arguments); tool: the
    registry name for a tool command (None for a core one); ui: its ui hints; positional: the property bare words
    fill (core commands)."""

    def __init__(self, name, description, schema=None, tool=None, ui=None, positional=None, source="core", args=None):
        self.name, self.description, self.tool, self.positional, self.source = name, description, tool, positional, source
        self.args = args                           # usage of the arguments, when the generated one says too little
        self.schema = schema if isinstance(schema, dict) else {}
        self.ui = ui if isinstance(ui, dict) else {}

    @property
    def props(self):
        p = self.schema.get("properties")
        return p if isinstance(p, dict) else {}

    @property
    def mode(self):
        """form or ask for a tool; None for a core command (it runs at once)."""
        return toolui.mode({"ui": self.ui, "input_schema": self.schema}) if self.tool else None

    def hint(self, field):
        h = (self.ui.get("fields") or {}).get(field) if isinstance(self.ui.get("fields"), dict) else None
        return h if isinstance(h, dict) else {}

    def file_field(self, field):
        """(is a file argument, filter globs) for one property."""
        p = self.props.get(field) if isinstance(self.props.get(field), dict) else {}
        if p.get("type") == "array":
            item = p.get("items") if isinstance(p.get("items"), dict) else {}
            ok = toolui.is_file(field[:-1] if field.endswith("s") else field, item, self.hint(field))
        else:
            ok = toolui.is_file(field, p, self.hint(field))
        return ok, toolui.filters(self.hint(field))

    def line(self, args):
        """The command as one line with these arguments, secrets masked (the stream, the history, the journal)."""
        words = [f"/{self.name}"]
        for k, v in args.items():
            p = self.props.get(k) if isinstance(self.props.get(k), dict) else {}
            if toolui.is_secret(p):
                shown = "***"
            elif isinstance(v, bool):
                words.append(f"--{'' if v else 'no-'}{kebab(k)}")
                continue
            elif isinstance(v, list):
                shown = " ".join(_quote(str(x)) for x in v)
            else:
                v = " ".join((v if isinstance(v, str) else json.dumps(v)).split())
                shown = _quote(v if len(v) <= 60 else v[:59] + "…")
            words.append(f"--{kebab(k)} {shown}")
        return " ".join(words)

    def usage(self):
        if self.args:
            return f"/{self.name} {self.args}"
        words = [f"/{self.name}"]
        req = set(self.schema.get("required") or [])
        for k, p in self.props.items():
            p = p if isinstance(p, dict) else {}
            val = "|".join(map(str, p["enum"])) if p.get("enum") else k.upper()
            arg = val if k == self.positional else f"--{kebab(k)}" + ("" if p.get("type") == "boolean" else f" {val}")
            words.append(arg if k in req else f"[{arg}]")
        return " ".join(words)


CORE = [
    Command("model", "the model and hardware mini runs on: a preset (none: list them); switching restarts llama-server",
            {"properties": {"preset": {"type": "string", "description": "a preset name"}}}, positional="preset"),
    Command("models", "the models folder and the downloads running in the background"),
    Command("download", "find GGUF models on Hugging Face (WORDS) or download one (OWNER/NAME [QUANT]) in the background",
            {"properties": {"repo": {"type": "string", "description": "search words, or OWNER/NAME"},
                            "quant": {"type": "string", "description": "which quantization, e.g. Q4_K_M (none: asks)"},
                            "name": {"type": "string", "description": "the model's name in the folder"},
                            "mmproj": {"type": "boolean", "description": "also the vision projector"}}},
            positional="repo"),
    Command("preset-new", "the preset maker: pick a model from the folder and the GPU(s) to run it on"),
    Command("new", "open another session (a tab with its own conversation and model; they run in parallel)",
            {"properties": {"name": {"type": "string"}}}, positional="name"),
    Command("clear", "forget this session's conversation so far (each task is told about the earlier ones)"),
    Command("session", "switch to a session by number or name (none: list them)",
            {"properties": {"target": {"type": "string"}}}, positional="target"),
    Command("rename", "rename this session",
            {"required": ["name"], "properties": {"name": {"type": "string"}}}, positional="name"),
    Command("close", "close this session (or the one named); a running one cannot be closed",
            {"properties": {"target": {"type": "string"}}}, positional="target"),
    Command("attach", "attach a file to the next task",
            {"required": ["path"], "properties": {"path": {"type": "string", "format": "file"}}}, positional="path"),
    Command("status", "check the model server and the model"),
    Command("undo", "put back what the last task (or RUN) changed, or only those files",
            {"properties": {"target": {"type": "array", "items": {"type": "string", "format": "file"},
                                       "description": "a run id and/or files"}}}, positional="target",
            args="[RUN] [FILE...]"),
    Command("registry", "open the registry browser"),
    Command("config", "open the settings"),
    Command("tools", "list core capabilities and registry tools"),
    Command("help", "list the commands"),
    Command("exit", "quit mini"),
]


class Commands:
    """The command registry: the core commands, then one per accepted tool."""

    def __init__(self, tools=(), presets=()):
        self.cmds = {c.name: c for c in CORE}
        if presets:                                # /model completes and checks the configured preset names
            m = self.cmds["model"]
            self.cmds["model"] = Command(m.name, m.description, {"properties": {"preset": {
                "type": "string", "enum": list(presets), "description": "a preset name"}}}, positional="preset")
        for m in tools:
            name = kebab(m["name"])
            if name in self.cmds:                  # a core command keeps its name; the tool stays reachable
                name = "tool-" + name
            self.cmds[name] = Command(name, m.get("description", ""), m.get("input_schema"), tool=m["name"],
                                      ui=m.get("ui"), source="bundled" if m.get("bundled") else "tool")

    @classmethod
    def load(cls, registry, presets=()):
        try:
            return cls(registry.list_tools(), presets)
        except Exception:   # noqa: BLE001 - a damaged registry must not take the core commands with it
            return cls(presets=presets)

    def __contains__(self, name):
        return name in self.cmds

    def get(self, name):
        return self.cmds.get(name)

    def all(self):
        return list(self.cmds.values())

    def matching(self, prefix):
        """Commands whose name starts with prefix, then those that contain it."""
        prefix = prefix.lower()
        first = [c for c in self.cmds.values() if c.name.startswith(prefix)]
        return first + [c for c in self.cmds.values() if prefix in c.name and c not in first]

    def is_command(self, text):
        """Whether prompt text is a command: `/` first and a known name, or anything that does not look like an
        absolute path (an unknown command is reported, a task that starts with a path is a task)."""
        if not text.startswith("/"):
            return False
        word = text[1:].split(None, 1)[0] if text[1:].strip() else ""
        return word in self.cmds or "/" not in word

    def help_text(self):
        core = [c for c in self.cmds.values() if not c.tool]
        tools = [c for c in self.cmds.values() if c.tool]
        width = max(len(c.usage()) for c in core)
        lines = [f"{c.usage():<{width}}  {c.description}" for c in core]
        if tools:
            lines.append("tools (a form or questions; also mini tool NAME in a terminal): "
                         + "  ".join(f"/{c.name}" for c in tools))
        lines.append("Type / for the list; Tab or Enter picks. Esc leaves /registry, /config and /tools; a running "
                     "task keeps going.")
        return "\n".join(lines)

    # ------------------------------------------------------------ parsing
    def parse(self, text):
        """(Command or None, {property: value}, problem) for prompt text that starts with `/`."""
        try:
            words = shlex.split(text[1:])
        except ValueError:                         # an unbalanced quote: plain words
            words = text[1:].split()
        if not words:
            return None, {}, "type a command after /"
        c = self.cmds.get(words[0])
        if c is None:
            return None, {}, f"unknown command /{words[0]}. /help lists them"
        args, problems = parse_args(c, words[1:])
        return c, args, "; ".join(problems)

    # ------------------------------------------------------------ completion
    def complete(self, text, folder=None):
        """Suggestions for the prompt text: [(new prompt text, label, description)]. Empty when text is not a
        command being typed."""
        if not text.startswith("/") or "\n" in text:
            return []
        if " " not in text:                                                   # the command name
            return [(f"/{c.name} ", f"/{c.name}", c.description) for c in self.matching(text[1:])]
        c = self.cmds.get(text[1:].split(" ", 1)[0])
        if c is None:
            return []
        head, _, token = text.rpartition(" ")
        head += " "
        try:
            before = shlex.split(head[1:])[1:]
        except ValueError:
            before = head[1:].split()[1:]
        prev = before[-1] if before else ""
        field = _field(c, prev[2:]) if prev.startswith("--") and "=" not in prev else None
        if token.startswith("--"):                                            # a flag
            used = {_field(c, w[2:].split("=")[0]) for w in before if w.startswith("--")}
            out = []
            for k, p in c.props.items():
                p = p if isinstance(p, dict) else {}
                if k in used and p.get("type") != "array":
                    continue
                for flag in [f"--{kebab(k)}"] + ([f"--no-{kebab(k)}"] if p.get("type") == "boolean" else []):
                    if flag.startswith(token):
                        out.append((head + flag + " ", flag, _describe(p)))
            return out
        if field and _prop(c, field).get("type") != "boolean":               # the value of the flag before
            return self._values(c, field, head, token, folder)
        if c.positional:
            if _prop(c, c.positional).get("type") != "array" and any(not w.startswith("--") for w in before):
                return []                                                     # its one positional value is given
            return self._values(c, c.positional, head, token, folder)
        if not token:
            return [(head + f"--{kebab(k)} ", f"--{kebab(k)}", _describe(p if isinstance(p, dict) else {}))
                    for k, p in c.props.items()]
        return []

    def _values(self, c, field, head, token, folder):
        p = _prop(c, field)
        items = p.get("items") if isinstance(p.get("items"), dict) else {}
        enum = p.get("enum") or items.get("enum")
        if enum:
            return [(head + str(v) + " ", str(v), "") for v in enum if str(v).startswith(token)]
        ok, globs = c.file_field(field)
        if ok:
            return [(head + _quote(p_) + ("" if p_.endswith("/") else " "), p_, "folder" if p_.endswith("/") else "")
                    for p_ in paths(folder or os.getcwd(), token, globs)]
        return []


def _prop(c, field):
    p = c.props.get(field)
    return p if isinstance(p, dict) else {}


def _field(c, flag):
    """The property a flag names (kebab or snake case, --no-X for a boolean), or None."""
    k = flag.replace("-", "_")
    if k in c.props:
        return k
    if k.startswith("no_") and _prop(c, k[3:]).get("type") == "boolean":
        return k[3:]
    return None


def _describe(p):
    d = p.get("description") or p.get("type") or ""
    return d + (f" ({'|'.join(map(str, p['enum']))})" if p.get("enum") else "")


def _quote(path):
    return shlex.quote(path) if any(ch.isspace() or ch in "'\"" for ch in path) else path


def parse_args(c, words):
    """({property: value}, [problems]) from the words after a command name."""
    args, problems, i = {}, [], 0
    pos = []
    while i < len(words):
        w = words[i]
        i += 1
        if not w.startswith("--") or w == "--":
            pos.append(w)
            continue
        flag, eq, val = w[2:].partition("=")
        k = _field(c, flag)
        if k is None:
            problems.append(f"/{c.name} has no --{flag} (it takes: "
                            + (", ".join(f"--{kebab(x)}" for x in c.props) or "nothing") + ")")
            continue
        p = _prop(c, k)
        t = p.get("type")
        if t == "boolean":
            if eq:
                args[k] = val.lower() not in ("false", "no", "0", "off")
            else:
                args[k] = flag.replace("-", "_") == k                     # --no-X is False
            continue
        vals = [val] if eq else []
        if t == "array":
            while not eq and i < len(words) and not words[i].startswith("--"):
                vals.append(words[i])
                i += 1
            args.setdefault(k, []).extend(vals)
            continue
        if not eq:
            if i >= len(words) or words[i].startswith("--"):
                problems.append(f"--{kebab(k)} needs a value")
                continue
            vals, i = [words[i]], i + 1
        v, problem = convert(k, p, vals[0])
        if problem:
            problems.append(problem)
        else:
            args[k] = v
    if pos:
        if c.positional is None:
            problems.append(f"/{c.name} takes --field value arguments, not {' '.join(pos)!r}")
        elif _prop(c, c.positional).get("type") == "array":
            args.setdefault(c.positional, []).extend(pos)
        else:
            v, problem = convert(c.positional, _prop(c, c.positional), " ".join(pos))
            if problem:
                problems.append(problem)
            else:
                args[c.positional] = v
    return args, problems


def convert(key, p, raw):
    """(value, problem) for one word given for a property."""
    t = p.get("type")
    try:
        if t == "integer":
            v = int(raw)
        elif t == "number":
            v = float(raw) if any(ch in raw for ch in ".eE") else int(raw)
        elif t == "object":
            v = json.loads(raw)
            if not isinstance(v, dict):
                return raw, f"--{kebab(key)} takes a JSON object"
        else:
            v = raw
    except ValueError:
        return raw, f"--{kebab(key)}: {raw!r} is not " + ("JSON" if t == "object" else "a number")
    if p.get("enum") and v not in p["enum"]:
        return v, f"{key} must be one of {'|'.join(map(str, p['enum']))}"
    return v, ""


def paths(folder, token, globs=()):
    """Paths that complete token: relative to folder (or absolute / ~ ones as typed), folders with a trailing /,
    files matching the globs (when there are any). Hidden entries only when the token's last part starts with a dot."""
    base, part = os.path.split(token)
    shown_base = base + "/" if base and not base.endswith("/") else base
    root = os.path.expanduser(base) if base.startswith(("/", "~")) else os.path.join(folder, base)
    try:
        names = sorted(os.listdir(root or "/"))
    except OSError:
        return []
    out = []
    for n in names:
        if not n.startswith(part) or (n.startswith(".") and not part.startswith(".")) or n in SKIP_DIRS:
            continue
        full = os.path.join(root, n)
        if os.path.isdir(full):
            out.append(shown_base + n + "/")
        elif not globs or any(fnmatch.fnmatch(n, g) for g in globs):
            out.append(shown_base + n)
        if len(out) >= MAX_PATHS:
            break
    if token and token in out and len(out) > 1:              # an exact match first
        out.remove(token)
        out.insert(0, token)
    return out
