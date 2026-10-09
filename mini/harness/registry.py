"""Tool registry: persistent storage primitives only.

Layout (survives restarts):
  <state>/registry/<name>/<version>/manifest.json   one directory per version, never modified after install
  <state>/registry/<name>/<version>/files/...        tool code and tests
  <state>/registry/<name>/ACTIVE                     the version that invoke() uses (rollback = rewrite this)

Manifest: name, version, kind, description, input_schema, output_schema, permissions, edge_cases, files,
test_file, test_results, created_by {provider, model}, attempts_used, created_at, image, parent_version, approvals.

search() is a plain token-overlap ranking over name, description and schema field names. Anything smarter
(semantic matching, composition planning) is the agent's job.
"""
import json
import os
import re
import shutil
import time

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
STOP = set("a an the to of and or for in on at by with from into is are be this that it as".split())


def tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if w not in STOP and len(w) > 1}


def schema_fields(schema):
    out = []
    if isinstance(schema, dict):
        for k, v in (schema.get("properties") or {}).items():
            out.append(k)
            if isinstance(v, dict) and v.get("description"):
                out.append(v["description"])
    return " ".join(out)


class Registry:
    def __init__(self, state_dir):
        self.root = os.path.join(state_dir, "registry")
        os.makedirs(self.root, exist_ok=True)

    # ---------------------------------------------------------------- read
    def names(self):
        return sorted(n for n in os.listdir(self.root) if os.path.isdir(os.path.join(self.root, n)) and self.versions(n))

    def versions(self, name):
        d = os.path.join(self.root, name)
        if not os.path.isdir(d):
            return []
        return sorted(int(v) for v in os.listdir(d) if v.isdigit() and os.path.exists(os.path.join(d, v, "manifest.json")))

    def active_version(self, name):
        p = os.path.join(self.root, name, "ACTIVE")
        if os.path.exists(p):
            return int(open(p).read().strip())
        vs = self.versions(name)
        return vs[-1] if vs else None

    def get(self, name, version=None):
        version = version or self.active_version(name)
        if version is None:
            return None
        p = os.path.join(self.root, name, str(version), "manifest.json")
        if not os.path.exists(p):
            return None
        m = json.load(open(p))
        m["_dir"] = os.path.join(self.root, name, str(version), "files")
        m["_active"] = version == self.active_version(name)
        return m

    def list_tools(self):
        return [self.get(n) for n in self.names()]

    def search(self, query, limit=10):
        q = tokens(query)
        scored = []
        for m in self.list_tools():
            text = " ".join([m["name"].replace("_", " "), m.get("description", ""),
                             schema_fields(m.get("input_schema")), schema_fields(m.get("output_schema"))])
            score = len(q & tokens(text))
            scored.append((score, m["name"], m))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [m for s, _, m in scored[:limit]]

    def snapshot(self):
        """Compact registry state for the log."""
        return [{"name": m["name"], "version": m["version"], "versions": self.versions(m["name"]),
                 "kind": m.get("kind"), "created_by": m.get("created_by")} for m in self.list_tools()]

    # ---------------------------------------------------------------- write
    def install(self, manifest, files_dir):
        name = manifest["name"]
        if not NAME_RE.match(name):
            raise ValueError(f"invalid tool name {name!r}")
        version = (self.versions(name) or [0])[-1] + 1
        d = os.path.join(self.root, name, str(version))
        os.makedirs(d)
        shutil.copytree(files_dir, os.path.join(d, "files"),
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
        m = {k: v for k, v in manifest.items() if not k.startswith("_")}
        m.update(version=version, installed_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        tmp = os.path.join(d, "manifest.json.tmp")
        json.dump(m, open(tmp, "w"), indent=2)
        os.replace(tmp, os.path.join(d, "manifest.json"))
        self.set_active(name, version)
        return version

    def set_active(self, name, version):
        if version not in self.versions(name):
            raise ValueError(f"{name} has no version {version}")
        tmp = os.path.join(self.root, name, "ACTIVE.tmp")
        open(tmp, "w").write(str(version))
        os.replace(tmp, os.path.join(self.root, name, "ACTIVE"))

    def wipe(self):
        shutil.rmtree(self.root)
        os.makedirs(self.root)
