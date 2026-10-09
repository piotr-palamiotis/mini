# mini

An agent that builds its own tools, for the Frankenstein track of the Dusk Till Dawn hackathon.

You give mini-agent a task in plain language. It works out what the task needs and searches its tool registry.
When a capability is missing, it writes a spec, the code and the tests, runs the tests in a sandbox,
asks you to approve the install, and then finishes the task with the new tool. The next session finds the
tool in the registry and reuses it.

Inference is local by default: gemma4:12b (Q4_K_M) on Ollama, thinking on, 64k context, fully in VRAM on an
RX 6700 XT. A cloud model is only a capped fallback, and only after you approve it.

mini has two parts:

- **mini-harness** (`mini/harness/`, `sandbox/`) owns everything the agent must not control: model backends,
  the sandbox, the network allowlist, permissions, caps, watchdogs, the approval gate, the registry storage
  and the run log.
- **mini-agent** (`mini/agent/`) runs the loop: parse → confirm → gap detection → tool creation → execution.

Demo video: [youtu.be/CYJnR0hDpY8](https://youtu.be/CYJnR0hDpY8)

The test results are in [docs/test-report.md](docs/test-report.md). The demo shot list is in
[docs/shot-list.md](docs/shot-list.md).

## Quick start

```sh
python3 -m venv .venv && .venv/bin/pip install textual   # the harness itself is standard library only
.venv/bin/python -m mini setup                            # build the sandbox base image (rootless Podman)
demo/services.sh up                                       # Mailpit: SMTP mailpit:1025, inbox http://127.0.0.1:8025
.venv/bin/python -m mini                                  # Textual UI
.venv/bin/python -m mini run "task" --attach file.pdf     # headless, approvals in the terminal
.venv/bin/python -m mini registry list|show|rollback|wipe
```

Configuration lives in `config/mini.toml` (every key is documented in `mini/harness/config.py`). The UI has a
config screen for the same file.

## How a task runs

1. **Parse.** The model calls `understand` with the goal, inputs, outputs, side effects, ambiguities and plan.
2. **Confirm-first** (config `approvals.confirm_first`, on by default). An approval dialog shows the
   understanding. You can proceed, clarify or cancel.
3. **Gap detection.** The registry state is logged. The model sees the installed tools (name, description,
   input and output fields) and the built-in actions (read and write files in the working directory, pure text
   reasoning). It calls `decide_coverage` and lists the missing capabilities.
4. **Tool creation**, once per missing capability (`mini/agent/creator.py`):
   - **Spec:** name, description, input and output schema, permissions (network `host:port`, apt and pip
     packages) and edge cases. Any new permission or package opens the approval gate. Approved packages go
     into a derived sandbox image, and the build is logged.
   - **Code:** `tool.py`, then `test_tool.py`. Each comes as a fenced block in a plain-text turn where the
     model is offered no tools. Code never travels inside a JSON tool-call argument (that broke Ollama's
     gemma4 parser in earlier runs).
   - **Tests** run in the sandbox. An attempt is one generate-or-fix of the code followed by a test run. The
     UI shows "attempt n/10".
   - **Fix:** `replace` (exact text), `replace_lines`, `rewrite_file` (the content comes in a plain-text turn),
     `update_spec` (any permission increase goes through the gate again), `run_in_sandbox` (an argv
     allowlist), `run_tests`. File content comes back in the tool results and stays current.
   - **Stop:** as soon as the tests pass, editing stops and the install dialog opens.
   - **Install:** the tool goes into the registry as a new version, with its manifest and test results.
5. **Execute.** The model invokes tools (each invocation runs in the sandbox), reads and writes files, and
   finishes. The harness checks each `finish` call: listed files must exist, and a plan that produces a file
   must have written one. If a tool misbehaves on the real input, `report_tool_bug` sends it back into the
   creation loop as a new version (at most 2 repairs per task). `need_tool` builds a capability discovered
   during execution.

## Hackathon rules: how each is met

| Rule | How |
|---|---|
| 1. No pre-written capabilities | The registry starts empty. No tool, template, few-shot example or demo hint exists in the code. `tests/test_prompts_generic.py` fails if a demo word (email, smtp, weather, pdf, ocr, markdown, …) appears anywhere in `mini/agent`, `mini/harness` or `sandbox`. `.gitignore` excludes `state/` and `runs/`, and the same test checks that nothing there is committed. |
| 2. The gap comes from the task | Gap detection only sees the task and the registry. There is no "build tool X" anywhere. |
| 3. Sandbox only | All generated code (tests and real runs) runs in rootless Podman, never on the host (see Sandbox). |
| 4. No install without passing tests | Install is only reachable from a passing test run, and the test output is in the log. |
| 5. Caps in code, set in config | `[caps]`, `[watchdogs]` and `[fallback]` in `config/mini.toml`, enforced in `watchdogs.py`, `turns.py`, `fallback.py` and `hwmon.py`. |
| 6. Authority cannot grow by itself | Network targets and packages live in the manifest. Any increase opens the approval gate, and the sandbox enforces exactly the approved set. |
| 7. No toy capabilities | The demo tools send mail over SMTP, fetch a live forecast, and convert PDFs with OCR. |
| 8. Reuse listed | See "Reused from the earlier mini" below. |
| 9. No provider agent features | The fallback runs as `claude -p --tools ""`, with no MCP, no settings and no slash commands. It is text in, text out. `tests/test_fallback.py` includes a live probe showing it cannot use tools. |

## Watchdogs (all thresholds in `[watchdogs]` / `[caps]`)

| Watchdog | Trigger | Action |
|---|---|---|
| Repeated identical call | Same call 3x in a row | Warning on the 2nd, blocked from the 3rd |
| Repeated passing test | 2 unchanged reruns, still passing | Straight to review |
| File rewritten too often | More than 3 full rewrites of one file | Forced `explain` step: what fails, why, next change |
| Failed attempts in a row | 3 failing test runs after edits | Fresh conversation with spec, current files and failing output. Counts toward the 10 attempts. |
| Attempt budget | 10 attempts on one tool | Cloud fallback dialog if enabled, otherwise stop and report |
| Unparseable tool call | Parse or JSON error | "Resend as one small call", up to 5x. Not an attempt. |
| Empty reply / lost call | No tool call | One nudge. Not an attempt. |
| Runaway output | 8,192 tokens or 300 s per reply | The reply is discarded with "make smaller changes" |
| Time and turn limits | 20 min or 30 turns per tool creation / execution phase | Dialog: hint, restart, fallback, stop |
| Hard run cap | 3600 s per run, 1.5M local tokens per run | Stop |
| Command policy | argv allowlist | Commands are argv lists and never touch a shell. Denials are logged. |
| Hardware | Swap grows by more than 250 MiB | Abort the run. The baseline is taken after the model is loaded. |

`tests/test_watchdogs.py` checks that each one fires exactly at its threshold. It also checks that the attempt
budget stops at 10 and that restarts do not reset it.

## Sandbox

- Rootless Podman 4.9 (`[sandbox] engine`). Each run gets a fresh container: read-only root filesystem,
  `/tmp` as tmpfs, `--cap-drop ALL`, `no-new-privileges`, 1 GiB memory with no extra swap, 2 CPUs,
  256 PIDs and a timeout.
- Mounts: the tool files (read-only) at `/tool` and the per-run scratch directory at `/work`, nothing else.
  No host environment variables (`HOME=/tmp`), no credentials, no home directory.
- Network: none by default. Declared and approved `host:port` targets put the container on a fresh
  `--internal` network with no route out. Each declared host resolves to an egress forwarder container
  (`sandbox/egress.py`), which forwards only to the allowlist and picks the destination by TLS SNI or HTTP
  Host. Every allow and deny is logged. `tests/test_policy.py` checks that undeclared hosts, raw IPs, the
  host loopback, the Ollama port, host paths and host env vars are all unreachable.
- **Sandbox base image** (`sandbox/Containerfile.base`): `python:3.12-slim` (Debian 13.6, Python 3.12.14),
  plus `pytest 8.3.5` (with its dependencies `iniconfig`, `packaging`, `pluggy`) and a non-root user `tool`
  (uid 1000). Nothing else. Extra apt or pip packages are requested in the spec, approved in the dialog,
  built into a derived image `mini-sandbox:pkg-<hash>`, and logged.

## Cloud fallback

- One interface (`mini/harness/fallback.py: Provider`). The first provider is the Claude subscription through
  `claude -p` and the existing Claude Code login, with no API key in the repo. Codex CLI, Gemini CLI or an
  API-key endpoint can be added as further `Provider` subclasses.
- Text in, text out. The reply's code blocks go through the same sandbox tests and install gate.
- It escalates only after the 10-attempt budget runs out (or when you pick "fallback" at a time/turn limit).
  The dialog shows the provider, the reason, how many local attempts failed, and that task content leaves the
  machine.
- It sends a compact summary: the task, the spec, the last code and the failing test output. Never the
  conversation.
- Caps per run: 3 attempts per tool, 60k tokens, and $0.50 estimated spend. Before every call the worst case
  (estimated input plus `CLAUDE_CODE_MAX_OUTPUT_TOKENS` = 8000 at Sonnet prices) must fit in what is left, so
  the cap cannot be overshot.
- Tools built by the fallback carry `created_by.fallback = true` and show as `[FALLBACK]` in the registry
  browser and the log.

## Reused from the earlier mini (rule 8)

Harness boilerplate only. No capability was reused.

- `mini/harness/backends.py`: copied from `offline-ai/mini/backends.py` (Ollama `/api/chat` streaming,
  OpenAI-compatible streaming, fake backend). Changes: reply caps come from config, a local `Endpoint` class,
  and `load()` for warm-up.
- `mini/agent/codeblocks.py`: the `ELISION` regex (detects "..." and "lines omitted") comes from
  `offline-ai/mini/engine.py`.
- Design ideas, not code: `replace` / `replace_lines` edit tools, file content inside tool results, and a
  fail-streak restart (all from `engine.py`). The shell-string command policy (`policy.py`) was **not**
  reused. Its quote-splitting bug caused 7 of 9 command denials in earlier runs. Commands are now argv lists.

## What is real

(Filled in from the test campaign: see docs/test-report.md.)

## What is simulated

- **Mail.** Mailpit is a local SMTP server with a web inbox. No mail leaves the machine. The SMTP host comes
  from a user-set environment fact in `config/mini.toml` (`[environment] facts`), the way a user would tell
  an assistant where their mail server is.
- **The demo PDF** is generated (`demo/make_pdf.py`, seeded and byte-for-byte reproducible). Pages 2 and 4 are
  rasterized "scans" with rotation, blur and noise. The ground truth (`demo/ground_truth.json`) is used only
  by the graders and is never visible to the agent.
- **"API returning an error"** in failure injection is a connection reset at the egress forwarder. An HTTP
  5xx response body is not simulated.
- **Approvals during the test campaign** were decided by a logged auto-policy (`--auto`), not a person. The
  dialogs themselves are tested in `tests/test_ui.py`.

## What is missing

(Filled in after the campaign.)
