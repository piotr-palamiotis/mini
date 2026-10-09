"""Approval gate. Installs, permission increases, the confirm-first step, cloud escalation and cap decisions all
pass through request(). The agent cannot answer for the user.

Modes (config [approvals]):
  ask   the UI handler shows a dialog and blocks until the user answers (headless: a terminal prompt)
  auto_visible  for demo recordings: the UI shows each dialog, then the auto policy answers it after a countdown
        (logged as decided_by="auto-policy (shown)")
  auto  for unattended test runs only: kinds listed in auto_approve are approved, the rest are refused.
        Every auto decision is logged as decided_by="auto-policy".
"""
import sys

KINDS = ("confirm", "permissions", "install", "escalate", "cap")


class Decision:
    def __init__(self, approved, choice="", note="", decided_by="user"):
        self.approved, self.choice, self.note, self.decided_by = approved, choice, note, decided_by

    def __repr__(self):
        return f"Decision({self.approved}, {self.choice!r}, {self.note!r}, {self.decided_by})"


class ApprovalGate:
    def __init__(self, cfg, log, handler=None):
        self.cfg = cfg["approvals"]
        self.log = log
        self.handler = handler           # handler(kind, title, details, options) -> Decision

    def request(self, kind, title, details, options=("approve", "refuse")):
        assert kind in KINDS, kind
        self.log.emit("approval", phase="requested", req=kind, title=title, details=details, options=list(options))
        if self.cfg["mode"] == "auto_visible" and self.handler:
            ok = kind in self.cfg["auto_approve"]
            choice = self.cfg.get("auto_choice", {}).get(kind, options[0]) if ok else options[-1]
            d = self.handler(kind, title, details, list(options), auto=(choice, self.cfg.get("auto_delay_s", 3)))
        elif self.cfg["mode"] in ("auto", "auto_visible"):
            ok = kind in self.cfg["auto_approve"]
            choice = self.cfg.get("auto_choice", {}).get(kind, options[0]) if ok else options[-1]
            d = Decision(ok, choice, "", "auto-policy")
        elif self.handler:
            d = self.handler(kind, title, details, list(options))
        else:
            d = self._terminal(kind, title, details, options)
        self.log.emit("approval", phase="decided", req=kind, approved=d.approved, choice=d.choice, note=d.note,
                      decided_by=d.decided_by)
        return d

    @staticmethod
    def _terminal(kind, title, details, options):
        print(f"\n=== APPROVAL: {title} ===", file=sys.stderr)
        for k, v in details.items():
            print(f"  {k}: {v}", file=sys.stderr)
        print("  options: " + " / ".join(options), file=sys.stderr)
        ans = input("> ").strip()
        choice = ans.split(" ", 1)[0] if ans else options[-1]
        note = ans.split(" ", 1)[1] if " " in ans else ""
        if choice not in options:
            choice, note = options[0] if kind == "confirm" else options[-1], ans
        return Decision(choice == options[0] or choice not in ("refuse", "stop"), choice, note, "user")
