"""Approval gate. Installs, permission increases, the confirm-first step and cap decisions all
pass through request(). The agent cannot answer for the user.

Modes (config [approvals]):
  ask   the UI handler shows a dialog and blocks until the user answers (headless: a terminal prompt)
  auto  for unattended test runs only: kinds listed in auto_approve are approved, the rest are refused.
        Every auto decision is logged as decided_by="auto-policy".
"""
import re
import sys

KINDS = ("confirm", "permissions", "install", "consult", "cap", "plan", "question")


class Decision:
    """picks: the chosen options of a question with choices (one, or several with multi)."""

    def __init__(self, approved, choice="", note="", decided_by="user", picks=None):
        self.approved, self.choice, self.note, self.decided_by = approved, choice, note, decided_by
        self.picks = picks

    def __repr__(self):
        return f"Decision({self.approved}, {self.choice!r}, {self.note!r}, {self.decided_by})"


class ApprovalGate:
    def __init__(self, cfg, log, handler=None):
        self.cfg = cfg["approvals"]
        self.log = log
        self.handler = handler           # handler(kind, title, details, options) -> Decision

    def request(self, kind, title, details, options=("approve", "refuse"), choices=None, multi=False):
        """choices: answers to pick from (a question). Single-select, they become the options; with multi the
        handler gets a numbered list in details and the picks come from the note (numbers or names, comma-separated)
        unless the handler returns them in Decision.picks. The whole exchange is a question block in the stream."""
        assert kind in KINDS, kind
        choices = [str(c) for c in choices or [] if str(c).strip()]
        if choices and multi:
            details = {**details, "options": "\n".join(f"{i}) {c}" for i, c in enumerate(choices, 1)),
                       "how": "type the numbers or names of your picks, separated by commas"}
            options = ("answer", "cancel")
        elif choices:
            options = tuple(choices) + ("cancel",)
        bid = self.log.start("question", req=kind, title=title, details={k: v for k, v in details.items()
                                                                         if not k.startswith("_")},
                             options=list(options), choices=choices, multi=bool(multi and choices))
        self.log.emit("approval", phase="requested", req=kind, title=title, details=details, options=list(options))
        if self.cfg["mode"] == "auto":
            ok = kind in self.cfg["auto_approve"]
            choice = self.cfg.get("auto_choice", {}).get(kind, options[0]) if ok else options[-1]
            d = Decision(ok, choice, "", "auto-policy")
        elif self.handler:
            d = self.handler(kind, title, details, list(options))
        else:
            d = self._terminal(kind, title, details, options)
        if choices and d.picks is None and d.approved and d.choice != "cancel":
            d.picks = pick(choices, d.note) if multi else [d.choice] if d.choice in choices else []
        note = "***" if details.get("_secret") and d.note else d.note
        self.log.emit("approval", phase="decided", req=kind, approved=d.approved, choice=d.choice,
                      note=note, decided_by=d.decided_by)
        self.log.end(bid, "answered" if d.approved and d.choice not in ("cancel", "refuse", "stop") else
                     "cancelled" if d.choice == "cancel" else "refused",
                     choice=d.choice, answer=note, picks=d.picks, decided_by=d.decided_by)
        return d

    @staticmethod
    def _terminal(kind, title, details, options):
        print(f"\n=== {'QUESTION' if kind == 'question' else 'APPROVAL'}: {title} ===", file=sys.stderr)
        for k, v in details.items():
            if not k.startswith("_"):
                print(f"  {k}: {v}", file=sys.stderr)
        print("  options: " + " / ".join(options) + ("   (type: answer YOUR TEXT)" if "answer" in options else ""),
              file=sys.stderr)
        if details.get("_secret"):
            import getpass
            secret = getpass.getpass("secret> ")
            return Decision(bool(secret), "answer" if secret else "cancel", secret, "user")
        ans = input("> ").strip()
        choice = ans.split(" ", 1)[0] if ans else options[-1]
        note = ans.split(" ", 1)[1] if " " in ans else ""
        if choice not in options:
            choice, note = options[0] if kind == "confirm" else options[-1], ans
        return Decision(choice == options[0] or choice not in ("refuse", "stop"), choice, note, "user")


def pick(choices, text):
    """The options named in a free-text answer: numbers (1-based) or names, comma-separated; other names are kept
    as typed (the free-text row)."""
    out = []
    for part in filter(None, (p.strip() for p in re.split(r"[,;\n]", text or ""))):
        p = choices[int(part) - 1] if part.isdigit() and 1 <= int(part) <= len(choices) else part
        if p not in out:
            out.append(p)
    return out
