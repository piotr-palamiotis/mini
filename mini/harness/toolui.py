"""A tool's `ui` hints and the form fields generated from its input schema (slash commands, see docs/redesign.md 4b).

The spec may carry optional hints; everything else comes from input_schema:
  "ui": {"mode": "form" | "ask",            form: a modal form, one field per input; ask: start at once, every
                                             missing value is a question card (io.ask)
         "order": [property, ...],           fields shown first, in this order; the rest follow in schema order
         "fields": {property: {"label": "Message", "multiline": true, "filter": "*.csv"}}}
multiline is for long text (a message body); filter (a glob, or a list of them) makes the field a file picker.

Field kinds: string -> text, number / integer -> number, boolean -> switch, enum -> select, array -> list (one item
per line), object -> json, a file input -> file, a secret -> secret (masked). A property is a file when its format
is "file" or "path", it has a filter, or it is named path / file / *_path / *_file; a secret when its format is
"password" or "secret", or it is writeOnly.

validate() is the harness check on a spec (the creator rejects a spec whose hints name anything but its schema
properties); fields() is lenient and ignores hints it does not understand, so an old manifest never breaks a form.
"""
import json

MODES = ("form", "ask")
FIELD_HINTS = {"label": str, "multiline": bool, "filter": (str, list)}


def _props(schema):
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    return props if isinstance(props, dict) else {}


def tidy(ui, schema):
    """The ui hints with what validate would refuse left out: hints only shape the form, so a wrong one is dropped
    instead of costing the spec another try."""
    if not isinstance(ui, dict):
        return {}
    props = _props(schema)
    out = {}
    if ui.get("mode") in MODES:
        out["mode"] = ui["mode"]
    order = ui.get("order")
    if isinstance(order, list):
        seen = []
        for k in order:
            if isinstance(k, str) and k in props and k not in seen:
                seen.append(k)
        if seen:
            out["order"] = seen
    fields = ui.get("fields")
    if isinstance(fields, dict):
        kept = {}
        for k, hint in fields.items():
            if k not in props or not isinstance(hint, dict):
                continue
            t = (props[k] or {}).get("type") if isinstance(props[k], dict) else None
            h = {}
            for name, v in hint.items():
                if name not in FIELD_HINTS or not isinstance(v, FIELD_HINTS[name]):
                    continue
                if name == "filter" and (t not in (None, "string", "array") or
                                         (isinstance(v, list) and not all(isinstance(x, str) for x in v))):
                    continue
                if name == "multiline" and v and t not in (None, "string"):
                    continue
                h[name] = v
            if h:
                kept[k] = h
        if kept:
            out["fields"] = kept
    return out


def validate(ui, schema):
    """A problem with a spec's ui hints, or None. Hints may only refer to properties of input_schema."""
    if ui in (None, {}):
        return None
    if not isinstance(ui, dict):
        return "ui must be an object {mode, order, fields}."
    props = _props(schema)
    extra = [k for k in ui if k not in ("mode", "order", "fields")]
    if extra:
        return f"ui: unknown key{'s' if len(extra) > 1 else ''} {', '.join(map(str, extra))} (it takes mode, order, fields)."
    if "mode" in ui and ui["mode"] not in MODES:
        return "ui.mode must be \"form\" or \"ask\"."
    order = ui.get("order", [])
    if not isinstance(order, list) or not all(isinstance(k, str) for k in order):
        return "ui.order must be a list of input property names."
    bad = [k for k in order if k not in props]
    if bad:
        return f"ui.order names {', '.join(bad)}, not in input_schema properties ({', '.join(props) or 'none'})."
    if len(set(order)) != len(order):
        return "ui.order lists a property twice."
    fields = ui.get("fields", {})
    if not isinstance(fields, dict):
        return "ui.fields must be an object {property: {label, multiline, filter}}."
    for k, hint in fields.items():
        if k not in props:
            return f"ui.fields.{k} is not an input_schema property ({', '.join(props) or 'none'})."
        if not isinstance(hint, dict):
            return f"ui.fields.{k} must be an object {{label, multiline, filter}}."
        for h, v in hint.items():
            if h not in FIELD_HINTS:
                return f"ui.fields.{k}: unknown hint {h} (label, multiline, filter)."
            if not isinstance(v, FIELD_HINTS[h]) or (h == "filter" and isinstance(v, list)
                                                     and not all(isinstance(x, str) for x in v)):
                return f"ui.fields.{k}.{h} has the wrong type."
        t = (props[k] or {}).get("type") if isinstance(props[k], dict) else None
        if hint.get("multiline") and t not in (None, "string"):
            return f"ui.fields.{k}: multiline is for string inputs."
        if "filter" in hint and t not in (None, "string", "array"):
            return f"ui.fields.{k}: filter is for file inputs (string, or an array of files)."
    return None


def mode(manifest):
    """form or ask: the declared ui.mode; without one, form when the tool has inputs."""
    ui = manifest.get("ui") if isinstance(manifest.get("ui"), dict) else {}
    if ui.get("mode") in MODES:
        return ui["mode"]
    return "form" if _props(manifest.get("input_schema")) else "ask"


def is_file(name, prop, hint=None):
    prop = prop if isinstance(prop, dict) else {}
    if (hint or {}).get("filter") or prop.get("format") in ("file", "path"):
        return True
    return prop.get("type", "string") == "string" and not prop.get("enum") and (
        name in ("path", "file") or name.endswith(("_path", "_file")))


def is_secret(prop):
    prop = prop if isinstance(prop, dict) else {}
    return prop.get("format") in ("password", "secret") or bool(prop.get("writeOnly"))


def filters(hint):
    f = (hint or {}).get("filter") or []
    return [f] if isinstance(f, str) else [x for x in f if isinstance(x, str)]


class Field:
    """One form field: name (the input property), kind, label, required, help, choices, multiline, filter (globs),
    default, and for a list whether its items are files."""

    def __init__(self, name, kind, label, required=False, help="", choices=None, multiline=False, filter=(),
                 default=None, files=False):
        self.name, self.kind, self.label, self.required, self.help = name, kind, label, required, help
        self.choices, self.multiline, self.filter, self.default, self.files = choices, multiline, list(filter), default, files

    def __repr__(self):
        return f"Field({self.name}, {self.kind})"


def fields(schema, ui=None):
    """[Field] for an input schema and its ui hints: the ui.order properties first, then the rest in schema order."""
    props, req = _props(schema), set((schema or {}).get("required") or [])
    ui = ui if isinstance(ui, dict) else {}
    hints = ui.get("fields") if isinstance(ui.get("fields"), dict) else {}
    order = [k for k in ui.get("order") or [] if isinstance(k, str) and k in props]
    out = []
    for k in list(dict.fromkeys(order)) + [k for k in props if k not in order]:
        p = props[k] if isinstance(props[k], dict) else {}
        h = hints.get(k) if isinstance(hints.get(k), dict) else {}
        t, files = p.get("type"), False
        if p.get("enum"):
            kind = "select"
        elif t == "boolean":
            kind = "switch"
        elif t in ("integer", "number"):
            kind = t
        elif t == "array":
            kind = "list"
            files = is_file(k[:-1] if k.endswith("s") else k, p.get("items") or {}, h)
        elif t == "object":
            kind = "json"
        elif is_secret(p):
            kind = "secret"
        elif is_file(k, p, h):
            kind = "file"
        else:
            kind = "text"
        label = h.get("label") if isinstance(h.get("label"), str) and h.get("label") else k.replace("_", " ")
        out.append(Field(k, kind, label, k in req, p.get("description") or "",
                         [str(c) for c in p["enum"]] if p.get("enum") else None,
                         bool(h.get("multiline")) and kind == "text", filters(h), p.get("default"), files))
    return out


def initial(field, value):
    """The value a form widget starts with, from an inline argument or the schema default (None: empty)."""
    if value is None:
        value = field.default
    if field.kind == "switch":
        return bool(value)
    if value is None:
        return None if field.kind == "select" else ""
    if field.kind == "list":
        return "\n".join(map(str, value)) if isinstance(value, list) else str(value)
    if field.kind == "json":
        return value if isinstance(value, str) else json.dumps(value)
    return str(value)


def to_inputs(fields_, raw):
    """(inputs, errors) from the widgets' raw values {name: value}. Empty fields are left out, so the tool's io.ask
    may still ask for an optional one; a required one left empty is an error. Answered values go in as inputs, so
    io.ask returns them without asking again."""
    inputs, errors = {}, []
    for f in fields_:
        v = raw.get(f.name)
        if f.kind == "switch":
            inputs[f.name] = bool(v)
            continue
        if f.kind == "select":
            if v in (None, ""):
                if f.required:
                    errors.append(f"{f.label}: choose one")
                continue
            inputs[f.name] = v
            continue
        text = "" if v is None else str(v)
        if f.kind == "list":
            items = [x.strip() for x in text.splitlines() if x.strip()]
            if items:
                inputs[f.name] = items
            elif f.required:
                errors.append(f"{f.label} is required")
            continue
        if not text.strip():
            if f.required:
                errors.append(f"{f.label} is required")
            continue
        try:
            if f.kind == "integer":
                inputs[f.name] = int(text.strip())
            elif f.kind == "number":
                s = text.strip()
                inputs[f.name] = float(s) if any(c in s for c in ".eE") else int(s)
            elif f.kind == "json":
                inputs[f.name] = json.loads(text)
            else:                                         # a secret or a long text is kept exactly as typed
                inputs[f.name] = text if f.kind == "secret" or f.multiline else text.strip()
        except ValueError:
            errors.append(f"{f.label}: {text.strip()!r} is not " + ("valid JSON" if f.kind == "json" else "a number"))
    return inputs, errors
