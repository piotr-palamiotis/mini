"""Lessons: persistent memory of what went wrong (and what fixed it) when building tools. Storage primitive only.

  <state>/lessons.jsonl   one lesson per line, appended, survives restarts

A lesson is written by the agent itself during a run (never seeded): problem, cause, avoid, try_instead, plus
the recurring error signatures the harness counted. search() is the same plain token-overlap ranking as the
registry; deciding what a lesson means for the next tool is the agent's job.
"""
import json
import os
import time

from .registry import tokens


class LessonStore:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "lessons.jsonl")
        os.makedirs(state_dir, exist_ok=True)

    def all(self):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out

    def add(self, lesson):
        lesson = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **lesson}
        with open(self.path, "a") as f:
            f.write(json.dumps(lesson, default=str) + "\n")
        return lesson

    def search(self, query, limit=3):
        q = tokens(query)
        scored = []
        for i, l in enumerate(self.all()):
            text = " ".join(str(l.get(k, "")) for k in ("capability", "problem", "cause", "avoid", "try_instead"))
            score = len(q & tokens(text))
            if score:
                scored.append((score, i, l))
        scored.sort(key=lambda x: (-x[0], -x[1]))          # best match first, newer first on ties
        return [l for _, _, l in scored[:limit]]

    def wipe(self):
        if os.path.exists(self.path):
            os.remove(self.path)


def format_lessons(lessons):
    lines = []
    for l in lessons:
        sigs = "; ".join(f"{s} (x{n})" for s, n in (l.get("errors") or [])[:3])
        lines.append(f"- [{l.get('outcome')}] {l.get('capability')}: problem: {l.get('problem')} | cause: {l.get('cause')}"
                     f" | avoid: {l.get('avoid')} | try instead: {l.get('try_instead')}" + (f" | errors seen: {sigs}" if sigs else ""))
    return "\n".join(lines)
