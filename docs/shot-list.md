# Demo video shot list (≤ 90 s)

Rules for the edit: speed up waiting (model thinking, container start) with a visible "×N" badge. Never cut a
failure: a failing test run, a watchdog trigger or a repair stays in, sped up if needed.

Screen layout: mini's Textual UI on the left two thirds (chat, live log, status bar with the attempt counter);
the Mailpit inbox (http://127.0.0.1:8025) in a browser on the right third.

| # | Time | Shot | What must be visible |
|---|---|---|---|
| 1 | 0–6 s | Title card: "mini: an agent that builds its own tools. Local gemma4:12b, sandboxed, capped." | — |
| 2 | 6–10 s | Registry tab, empty. | "(registry is empty)": nothing is pre-built |
| 3 | 10–14 s | Type: *Send an email to anna@example.local saying the meeting moved to Friday.* | The plain-language task, no tool named |
| 4 | 14–20 s | Confirm-first dialog → Proceed. Live log: GAP line "missing: send email via SMTP". | The understanding; gap detection reasoning |
| 5 | 20–26 s | Permission dialog: network `mailpit:1025` → Approve. | Authority needs a human |
| 6 | 26–38 s (×8) | Code arrives; test run in the sandbox. Keep the first FAILED run, then "attempt 2/10" PASSED. | Status bar attempt counter; red then green test line |
| 7 | 38–42 s | Install dialog (manifest: permissions, tests, created by local/gemma4:12b) → Approve. | No install without passing tests |
| 8 | 42–46 s | Mailpit inbox refreshes: the mail to anna is there. | The real side effect |
| 9 | 46–60 s (×10) | Type: *What will the weather be in Prague tomorrow?* Gap: email tool does not cover it → new tool; dialog for the API host it chose; tests; answer with tomorrow's numbers. | It picks a real free API itself; registry now has 2 tools |
| 10 | 60–64 s | Quit mini (Ctrl+C) and start it again. Registry tab shows both tools with versions. | Persistence across restarts |
| 11 | 64–80 s (×6) | Type: *Email anna@example.local tomorrow's weather forecast for Prague.* Gap line: "covered: use fetch_weather…, send_email…; missing: none". No build. Mailpit shows the forecast mail. | Fresh-session composition, no rebuilding |
| 12 | 80–86 s | Registry browser: a tool with v1/v2 (from a repair) → Roll back to v1. | Versioning and rollback |
| 13 | 86–90 s | End card: success rates from docs/test-report.md (the real numbers, including failures). | Honesty |

Optional if a run hits a cap during recording: keep the red "CAP:" indicator in the status bar on screen and
cut to the dialog; do not re-record to hide it.
