"""Tool registry: every tool is its own git repository. Storage primitives only.

Layout (survives restarts):
  <state>/tools/<name>/                 git repo; main holds the accepted versions, tagged v1, v2, ...
  <state>/tools/<name>/.git/MINI_ACTIVE the version invoke() uses when it is not the newest (rollback)
  <state>/tools/.cache/<name>/<sha>/    read-only export of one commit, mounted into the sandbox

A task never writes to main. install() commits the tool to the branch task/<run_id> ("staged"); the run that
made it can use it right away, other runs cannot. accept() makes the staged tree the next tagged version on
main; discard() deletes the branch. Tools are generated whole, so accepting a branch whose base is no longer
the newest version replaces that version (force=True) rather than merging the two.

Manifest: name, version, kind, description, input_schema, output_schema, permissions, edge_cases, files,
test_file, test_results, created_by {provider, model}, attempts_used, created_at, image, parent_version, approvals.

search() is a plain token-overlap ranking over name, description and schema field names. Anything smarter
(semantic matching, composition planning) is the agent's job.
"""
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
STOP = set("a an the to of and or for in on at by with from into is are be this that it as".split())
GIT_ID = ["-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main"]
GIT_ENV = {"GIT_AUTHOR_NAME": "mini", "GIT_AUTHOR_EMAIL": "mini@localhost",
           "GIT_COMMITTER_NAME": "mini", "GIT_COMMITTER_EMAIL": "mini@localhost"}


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


class RegistryError(Exception):
    pass


class Registry:
    def __init__(self, state_dir, run_id=None, auto_accept=False):
        self.root = os.path.join(state_dir, "tools")
        self.cache = os.path.join(self.root, ".cache")
        self.run_id, self.auto_accept = run_id, auto_accept
        os.makedirs(self.cache, exist_ok=True)

    # ---------------------------------------------------------------- git
    def _git(self, name, *args, check=True, input=None):
        r = subprocess.run(["git", *GIT_ID, "-C", os.path.join(self.root, name), *args], capture_output=True,
                           text=input is None or isinstance(input, str), input=input, env={**os.environ, **GIT_ENV})
        if check and r.returncode:
            raise RegistryError(f"git {' '.join(args)} in {name}: {(r.stderr or r.stdout).strip()}")
        return r.stdout.strip() if isinstance(r.stdout, str) else r.stdout

    def _ref_ok(self, name, ref):
        return self._git(name, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False) != ""

    @staticmethod
    def branch(run_id):
        return f"task/{run_id}"

    def _repos(self):
        return sorted(n for n in os.listdir(self.root) if NAME_RE.match(n) and os.path.isdir(os.path.join(self.root, n, ".git")))

    # ---------------------------------------------------------------- read
    def names(self):
        """Accepted tools, plus the tools this run staged."""
        return [n for n in self._repos() if self.versions(n) or self._staged_ref(n)]

    def versions(self, name):
        if not os.path.isdir(os.path.join(self.root, name, ".git")):
            return []
        return sorted(int(t[1:]) for t in self._git(name, "tag", "--list", "v*").split() if t[1:].isdigit())

    def active_version(self, name):
        p = os.path.join(self.root, name, ".git", "MINI_ACTIVE")
        vs = self.versions(name)
        if os.path.exists(p):
            with open(p) as f:
                v = int(f.read().strip())
            if v in vs:
                return v
        return vs[-1] if vs else None

    def _staged_ref(self, name, run_id=None):
        run_id = run_id or self.run_id
        if not run_id or not os.path.isdir(os.path.join(self.root, name, ".git")):
            return None
        ref = self.branch(run_id)
        return ref if self._ref_ok(name, ref) else None

    def get(self, name, version=None):
        """A manifest with _dir (files to mount). No version: this run's staged version, else the active one."""
        if version is None:
            ref = self._staged_ref(name)
            if ref is None:
                version = self.active_version(name)
                if version is None:
                    return None
                ref = f"v{version}"
        else:
            ref = f"v{version}"
            if not self._ref_ok(name, ref):
                return None
        return self._load(name, ref)

    def _load(self, name, ref):
        sha = self._git(name, "rev-parse", ref + "^{commit}")
        m = json.loads(self._git(name, "show", f"{sha}:manifest.json"))
        m["_dir"] = self._export(name, sha)
        m["_staged"] = ref.startswith("task/")
        m["_active"] = not m["_staged"] and m.get("version") == self.active_version(name)
        m["_ref"] = ref
        return m

    def _export(self, name, sha):
        d = os.path.join(self.cache, name, sha)
        if not os.path.isdir(d):
            tmp = d + ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp)
            data = self._git(name, "archive", "--format=tar", sha, input=b"")
            with tarfile.open(fileobj=io.BytesIO(data)) as t:
                t.extractall(tmp, filter="data")
            os.remove(os.path.join(tmp, "manifest.json"))
            os.replace(tmp, d)
        return d

    def list_tools(self):
        return [m for m in (self.get(n) for n in self.names()) if m]

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
        return [{"name": m["name"], "version": m["version"], "versions": self.versions(m["name"]), "staged": m["_staged"],
                 "kind": m.get("kind"), "created_by": m.get("created_by")} for m in self.list_tools()]

    def staged(self):
        """Every staged tool across all runs: [{name, run_id, branch, version, created_by, test_results, base}]."""
        out = []
        for n in self._repos():
            for b in self._git(n, "for-each-ref", "--format=%(refname:short)", "refs/heads/task/").split():
                m = json.loads(self._git(n, "show", f"{b}:manifest.json"))
                base = self._git(n, "merge-base", "main", b, check=False) if self._ref_ok(n, "main") else ""
                out.append({"name": n, "run_id": b[len("task/"):], "branch": b, "version": m.get("version"),
                            "created_by": m.get("created_by"), "test_results": m.get("test_results"),
                            "description": m.get("description"),
                            "outdated": bool(base) and base != self._git(n, "rev-parse", "main")})
        return out

    def diff(self, name, run_id):
        """What accepting would change on main, as a git diff."""
        b = self.branch(run_id)
        if not self._ref_ok(name, b):
            raise RegistryError(f"{name} has nothing staged by run {run_id}")
        if not self._ref_ok(name, "main"):
            return self._git(name, "show", "--format=", "--stat", "-p", "--root", b)
        return self._git(name, "diff", "main", b)

    # ---------------------------------------------------------------- write
    def install(self, manifest, files_dir):
        """Stage the tool on this run's branch (accept it too with auto_accept). Returns the planned version."""
        name = manifest["name"]
        if not NAME_RE.match(name):
            raise ValueError(f"invalid tool name {name!r}")
        if not self.run_id:
            raise RegistryError("install needs a run id (the staging branch is named after the run)")
        repo = os.path.join(self.root, name)
        if not os.path.isdir(os.path.join(repo, ".git")):
            os.makedirs(repo, exist_ok=True)
            self._git(name, "init", "-q")
        version = (self.versions(name) or [0])[-1] + 1
        b = self.branch(self.run_id)
        start = b if self._ref_ok(name, b) else ("main" if self._ref_ok(name, "main") else None)
        if start:
            self._git(name, "checkout", "-q", "-B", b, start)
        else:
            self._git(name, "checkout", "-q", "--orphan", b)
        for f in os.listdir(repo):
            if f != ".git":
                p = os.path.join(repo, f)
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        shutil.copytree(files_dir, repo, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".git"))
        m = {k: v for k, v in manifest.items() if not k.startswith("_")}
        m.update(version=version, installed_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        with open(os.path.join(repo, "manifest.json"), "w") as f:
            json.dump(m, f, indent=2)
        self._git(name, "add", "-A")
        cb = m.get("created_by") or {}
        tr = (m.get("test_results") or {}).get("summary", "")
        self._git(name, "commit", "-q", "--allow-empty", "-m",
                  f"{name}: {m.get('description', '')[:60]}\n\ntests: {tr}\nbuilt by: {cb.get('provider')} / "
                  f"{cb.get('model')}\nrun: {self.run_id}")
        self._git(name, "checkout", "-q", "--detach")
        if self.auto_accept:
            return self.accept(name, self.run_id, force=True)
        return version

    def accept(self, name, run_id=None, force=False):
        """The staged tree becomes the next tagged version on main. Returns the version number."""
        run_id = run_id or self.run_id
        b = self.branch(run_id)
        if not self._ref_ok(name, b):
            raise RegistryError(f"{name} has nothing staged by run {run_id}")
        has_main = self._ref_ok(name, "main")
        if has_main and not force:
            base = self._git(name, "merge-base", "main", b, check=False)
            if base != self._git(name, "rev-parse", "main"):
                raise RegistryError(f"{name}: v{self.versions(name)[-1]} was accepted after run {run_id} staged its "
                                    "version. Accepting replaces it (force).")
        version = (self.versions(name) or [0])[-1] + 1
        tree = self._git(name, "rev-parse", f"{b}^{{tree}}")
        m = json.loads(self._git(name, "show", f"{b}:manifest.json"))
        m["version"] = version
        m["accepted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        # one commit on main whose tree is the staged tree with the final version number in the manifest
        blob = self._git(name, "hash-object", "-w", "--stdin", input=json.dumps(m, indent=2) + "\n")
        entries = self._git(name, "ls-tree", tree).splitlines()
        entries = [e for e in entries if not e.endswith("\tmanifest.json")] + [f"100644 blob {blob}\tmanifest.json"]
        new_tree = self._git(name, "mktree", input="\n".join(entries) + "\n")
        msg = self._git(name, "log", "-1", "--format=%B", b) + f"\naccepted as v{version}"
        parents = ["-p", "main"] if has_main else []
        commit = self._git(name, "commit-tree", new_tree, *parents, "-m", msg)
        self._git(name, "update-ref", "refs/heads/main", commit)
        self._git(name, "tag", f"v{version}", commit)
        self._git(name, "branch", "-D", b)
        p = os.path.join(self.root, name, ".git", "MINI_ACTIVE")
        if os.path.exists(p):
            os.remove(p)                       # a newly accepted version is the active one
        return version

    def discard(self, name, run_id=None):
        run_id = run_id or self.run_id
        b = self.branch(run_id)
        if not self._ref_ok(name, b):
            raise RegistryError(f"{name} has nothing staged by run {run_id}")
        self._git(name, "branch", "-D", b)
        if not self.versions(name) and not self._git(name, "for-each-ref", "refs/heads").strip():
            shutil.rmtree(os.path.join(self.root, name))       # never accepted, nothing left

    def set_active(self, name, version):
        if version not in self.versions(name):
            raise ValueError(f"{name} has no version {version}")
        p = os.path.join(self.root, name, ".git", "MINI_ACTIVE")
        with open(p + ".tmp", "w") as f:
            f.write(str(version))
        os.replace(p + ".tmp", p)

    def wipe(self):
        shutil.rmtree(self.root)
        os.makedirs(self.cache)
