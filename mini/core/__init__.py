"""Core capabilities: what the agent can always do, without a tool from the registry.

One module per capability in this package. A module defines
  NAME         the call's name
  DESCRIPTION  what it does, for the model (the prompt's list of core capabilities is generated from these)
  SCHEMA       JSON Schema object of the call's arguments
  COVERS       short phrases of what it does ("copy file"), for covering(): gap detection and the creator guard
  run(call, args) -> str   the handler; call is a Call, args are already validated against SCHEMA
load() finds every module, so adding a capability means adding one file. Modules whose name starts with "_" are
shared helpers, not capabilities.
"""
import importlib
import pkgutil
import re

TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
# words that say nothing about what a step does; covering() ignores them in the text it checks
GENERIC = set("""file files folder folders directory directories dir path paths text content contents user users
local workspace project given named specified existing new another other one some all each every its their them it
this that these those from into to in on of the a an and or with for by as at current here show display me""".split())


class Call:
    """What a handler gets besides its arguments: the agent (its folder, checkpoint, sandbox, gate, registry and log),
    the text of the reply that made the call (fenced blocks travel there, not in arguments) and the conversation
    (a handler may ask for content in a text turn; it sets answered when it already replied in the conversation)."""

    def __init__(self, agent, content="", messages=None):
        self.agent, self.content, self.messages = agent, content or "", messages
        self.answered = False


class Capability:
    def __init__(self, module):
        self.name, self.description, self.schema = module.NAME, module.DESCRIPTION, module.SCHEMA
        self.covers = tuple(getattr(module, "COVERS", ()))
        self.handler = module.run

    def tool(self):
        """The function definition offered to the model."""
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "required": list(self.schema.get("required") or []),
                               "properties": self.schema.get("properties") or {}}}}

    def signature(self):
        props, req = self.schema.get("properties") or {}, set(self.schema.get("required") or [])
        return f"{self.name}({', '.join(k + ('' if k in req else '?') for k in props)})"


def validate(schema, args):
    """(arguments, problem): a call's arguments checked against its object schema. Missing required and unknown
    properties, wrong types and values outside an enum are problems; an integer, number or boolean sent as a string
    is converted. problem is "" when the call may run."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return args, "the arguments must be an object"
    props, out, problems = schema.get("properties") or {}, {}, []
    unknown = [k for k in args if k not in props]
    if unknown:
        problems.append(f"unknown argument{'s' if len(unknown) > 1 else ''} {', '.join(unknown)} "
                        f"(it takes: {', '.join(props) or 'nothing'})")
    for k in schema.get("required") or []:
        if args.get(k) is None:
            problems.append(f"{k} is required")
    for k, v in args.items():
        if k not in props or v is None:
            continue
        v, problem = _check(k, props[k], v)
        if problem:
            problems.append(problem)
        out[k] = v
    return out, "; ".join(problems)


def _check(key, spec, v):
    t = spec.get("type")
    if t in ("integer", "number") and isinstance(v, str):
        try:
            v = int(v) if t == "integer" else float(v)
        except ValueError:
            pass
    elif t == "boolean" and isinstance(v, str) and v.lower() in ("true", "false"):
        v = v.lower() == "true"
    elif t == "array" and isinstance(v, str):
        v = [v]                                   # one value where a list is expected
    elif t == "string" and isinstance(v, list) and len(v) <= 1 and all(isinstance(x, str) for x in v):
        v = v[0] if v else ""                     # a list of one (or none) where a string is expected
    if t in TYPES and (not isinstance(v, TYPES[t]) or (t in ("integer", "number") and isinstance(v, bool))):
        return v, f"{key} must be {'an' if t[0] in 'aeio' else 'a'} {t}"
    if "enum" in spec and v not in spec["enum"]:
        return v, f"{key} must be one of {', '.join(map(str, spec['enum']))}"
    if t == "array" and isinstance((spec.get("items") or {}).get("type"), str):
        it = spec["items"]["type"]
        if any(not isinstance(x, TYPES.get(it, object)) for x in v):
            return v, f"every item of {key} must be {'an' if it[0] in 'aeio' else 'a'} {it}"
    return v, ""


def _words(text):
    stem = lambda w: w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
    return {stem(w) for w in re.findall(r"[a-z0-9]+", str(text).lower())}


class Catalog:
    def __init__(self, caps):
        self.caps = {c.name: c for c in caps}

    def __contains__(self, name):
        return name in self.caps

    def names(self):
        return list(self.caps)

    def get(self, name):
        return self.caps.get(name)

    def tools(self):
        return [c.tool() for c in self.caps.values()]

    def listing(self):
        """The prompt's list of core capabilities, one line each."""
        return "\n".join(f"- {c.signature()}: {c.description}" for c in self.caps.values())

    def search(self, query, limit=8):
        """Capabilities ranked by word overlap of the query with their name, description and covered phrases."""
        q = _words(query) - GENERIC
        scored = []
        for c in self.caps.values():
            score = len(q & (_words(c.name.replace("_", " ") + " " + c.description + " " + " ".join(c.covers)) - GENERIC))
            if score:
                scored.append((-score, c.name))
        return [self.caps[n] for _, n in sorted(scored)[:limit]]

    def covering(self, text):
        """The capability that already does what text (a need, a tool name or a description) asks for, or None.
        Deterministic and strict: text matches a phrase when it has every word of the phrase and no other word
        beyond GENERIC ones, so "copy a file to another folder" is copy_file and "count the words of a file" is not."""
        text = str(text or "").strip()
        if text in self.caps:
            return text
        words = _words(text.replace("_", " "))
        content = words - GENERIC
        for c in self.caps.values():
            for phrase in c.covers:
                p = _words(phrase)
                if p <= words and not content - p:
                    return c.name
        return None

    def invoke(self, call, name, args):
        """Validate the arguments, then run the capability. Returns the result text for the model."""
        c = self.caps[name]
        args, problem = validate(c.schema, args)
        if problem:
            return f"{name} was not run: {problem}. Call it as {c.signature()}."
        return c.handler(call, args)


_CATALOG = None


def load():
    """The catalog of every capability module in this package (loaded once)."""
    global _CATALOG
    if _CATALOG is None:
        caps = []
        for m in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
            if not m.name.startswith("_"):
                caps.append(Capability(importlib.import_module(f"{__name__}.{m.name}")))
        _CATALOG = Catalog(caps)
    return _CATALOG

