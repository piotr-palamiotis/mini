"""Saved settings: values a tool needs on every run (an account, a server, a default folder), asked once and kept.

A tool declares what it needs in its manifest; nothing here knows what any setting means:
  "settings": [{"key": "some_service", "description": "...", "schema": {JSON Schema object},
                "secret": ["password"]}]
  "permissions": {"network": ["setting:some_service"]}   the host:port comes from the saved value
                                                          (fields host and port, or a "host:port" string)
Before a tool runs, the harness gives it exactly its declared settings. A missing one is asked for with the
declared schema and saved. Several tools can share a key. Files are <state>/settings/<key>.json, mode 600.
"""
import json
import os
import re
import time

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")


class MissingSetting(Exception):
    pass


def declared(manifest):
    return [d for d in (manifest.get("settings") or []) if isinstance(d, dict) and d.get("key")]


def validate_declarations(decls):
    """Returns a problem or None."""
    if not isinstance(decls, list):
        return "settings must be a list of {key, description, schema, secret}."
    for d in decls:
        if not isinstance(d, dict) or not KEY_RE.match(str(d.get("key", ""))):
            return "each setting needs a snake_case key (2-48 characters)."
        sc = d.get("schema")
        if not isinstance(sc, dict) or not isinstance(sc.get("properties"), dict) or not sc["properties"]:
            return f"setting {d['key']}: schema must be a JSON Schema object with properties."
        bad = [f for f, prop in sc["properties"].items() if not isinstance(prop, dict)]
        if bad:                                     # {"properties": {"type": "string"}} makes a field called type
            return (f"setting {d['key']}: each entry of schema.properties is a field name mapped to that field's "
                    f"schema, like {{\"host\": {{\"type\": \"string\"}}}}; {', '.join(map(repr, bad))} is not.")
        if not isinstance(d.get("secret") or [], list) or any(s not in sc["properties"] for s in d.get("secret") or []):
            return f"setting {d['key']}: secret lists field names from its schema."
    return None


def question_for(decl, field, prop):
    secret = field in (decl.get("secret") or []) or prop.get("format") == "password"
    kind = "secret" if secret else {"integer": "number", "number": "number", "boolean": "confirm"}.get(prop.get("type"), "text")
    if prop.get("enum"):
        kind = "choice"
    return {"op": "ask", "key": f"{decl['key']}.{field}", "kind": kind, "choices": prop.get("enum"),
            "default": prop.get("default"),
            "prompt": f"{decl.get('description') or decl['key']}: {prop.get('description') or field}"}


class SettingsStore:
    def __init__(self, state_dir):
        self.root = os.path.join(state_dir, "settings")
        os.makedirs(self.root, mode=0o700, exist_ok=True)

    def _path(self, key):
        if not KEY_RE.match(key):
            raise ValueError(f"invalid setting key {key!r}")
        return os.path.join(self.root, key + ".json")

    def get(self, key):
        try:
            with open(self._path(key)) as f:
                return json.load(f)
        except OSError:
            return None

    def save(self, key, value, decl, saved_by=""):
        rec = {"key": key, "value": value, "description": decl.get("description", ""), "schema": decl.get("schema"),
               "secret": decl.get("secret") or [], "saved_by": saved_by, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        p = self._path(key)
        fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f, indent=1)
        os.replace(p + ".tmp", p)

    def delete(self, key):
        os.remove(self._path(key))

    def list(self):
        out = []
        for f in sorted(os.listdir(self.root)):
            if f.endswith(".json"):
                rec = self.get(f[:-5])
                if rec:
                    out.append(rec)
        return out

    def masked(self, rec):
        v = rec.get("value")
        if isinstance(v, dict):
            return {k: ("***" if k in rec.get("secret", []) else x) for k, x in v.items()}
        return "***" if rec.get("secret") else v

    # ------------------------------------------------------------ for a tool run
    def resolve(self, manifest, asker=None):
        """{key: value} for every declared setting; missing ones are asked with asker(question) -> {"answer"} and
        saved. Raises MissingSetting when one is missing and nobody can be asked."""
        out = {}
        for d in declared(manifest):
            rec = self.get(d["key"])
            if rec is None:
                if asker is None:
                    raise MissingSetting(f"setting {d['key']} ({d.get('description', '')}) is not saved yet")
                value = {}
                props = (d.get("schema") or {}).get("properties") or {}
                required = set((d.get("schema") or {}).get("required") or props)
                for field, prop in props.items():
                    reply = asker(question_for(d, field, prop if isinstance(prop, dict) else {}))
                    if "answer" not in reply or reply["answer"] in (None, ""):
                        if field in required:
                            raise MissingSetting(f"setting {d['key']}: no value for {field}")
                        continue
                    value[field] = reply["answer"]
                self.save(d["key"], value, d, saved_by=manifest.get("name", ""))
                rec = self.get(d["key"])
            out[d["key"]] = rec["value"]
        return out

    def network(self, manifest, settings):
        """Concrete host:port targets: declared ones plus those taken from settings ("setting:<key>")."""
        out = []
        for t in (manifest.get("permissions") or {}).get("network") or []:
            if not t.startswith("setting:"):
                out.append(t)
                continue
            v = settings.get(t[len("setting:"):])
            if isinstance(v, dict) and v.get("host") and v.get("port"):
                out.append(f"{v['host']}:{v['port']}")
            elif isinstance(v, str) and ":" in v:
                out.append(v)
            else:
                raise MissingSetting(f"{t}: the saved value has no host and port")
        return out
