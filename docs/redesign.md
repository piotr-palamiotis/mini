# mini redesign

Notes from the design discussion of 2026-10-21. Steps 1 to 5 of the build order (sections 1 to 3 and items 1, 3 and 7 of section 5) are implemented; the rest is not yet.
Status of each part: **decided** (the user chose it), **proposed** (suggested, not yet confirmed).

## Problems this answers

- Files live in three places: `/host` (read-only), `runs/<id>/work/out` (scratch) and the user's folder after `mini apply`. Results are hard to find and cannot be seen in a file explorer or IDE while mini works.
- Runs are isolated: a new session knows nothing about what was done in the folder before.
- The agent builds tools for things core should already do. "Built-in actions" is one sentence in the prompt and nothing in the harness enforces it.
- The UI splits one conversation over four panes (chat, prompt, thinking, live log) and asks every question in a modal that hides the output.

## 1. Workspace and git safety (decided)

The folder mini is launched in is the workspace. No `/host`, no `out/` scratch, no `mini apply`.

- **Copy in, export out.** For each tool call the harness copies the files the call names into the sandbox. Whatever the tool writes is exported straight back to the same relative path in the folder. Edits happen in place. New files go where the task says; the harness only confines the path to the folder.
- Every exported path is confined to the folder (symlinks included). The sandbox never sees `.git`.
- `/tmp` stays a tmpfs inside the container. No temp folders on the host.
- Tool tests build their own fixtures and never touch the project.
- **Git is required.** If the folder is not a git repo, mini says so and offers `git init`. It does nothing else until the user agrees.
- **Checkpoints use hidden refs.** Before a task changes anything, mini snapshots the tree to `refs/mini/<run>`. This works with a dirty tree and adds nothing to the branch history. `mini undo` and `/undo` restore a whole task or single files from the snapshot.

## 2. Project memory (decided)

- `<folder>/.mini/` holds a journal of past tasks (task, files touched, result) and the run logs. It is excluded through `.git/info/exclude` and visible in a file explorer.
- At startup the agent is told what happened in this folder before (`Agent.context()`).
- Global state (tool registry, settings) stays in `state/`.

## 3. Core capabilities (decided, catalog design proposed)

Core capabilities live in one code catalog, `mini/core/`: one module per capability with a name, description, schema and handler.

- The prompt's list of built-in actions is generated from the catalog.
- Gap detection searches the catalog first.
- The tool creator rejects a spec that overlaps a core capability.
- Adding a core capability means adding one file.

Out of the box:

- **Files:** list, read (paged), write, edit (replace or by line range), copy, move, delete, search by name and by content, diff.
- **Git:** status, diff, log, checkpoint, undo.
- **Run code:** run a script or command in the sandbox against the files named.
- **Web:** fetch a URL from an approved host.
- **Ask:** ask the user a question or for a confirmation (`ask_user`, see section 4).
- **Notes and journal:** read and write project memory.
- **Registry:** search, inspect and invoke tools.

Document formats (PDF, docx, xlsx) ship as bundled tools in the same git-backed format as user tools. They are installed on first run and cannot be shadowed by the creator.

## 4. UI (decided)

One scrolling column replaces the chat, prompt, thinking and live-log panes. The prompt box is fixed at the bottom.

- Answers render as markdown, streamed.
- **Thinking block:** open and streaming while the model generates; collapses to "Thought for 12s" when done; expandable.
- **Tool and command blocks:** while running they are open with a spinner, elapsed time and a tail of the last ~8 lines of live output. This covers anything mini executes (sandbox runs, builds, tests, model loading). When finished they collapse to one line (name, ok, seconds, line count) and can be expanded. Failed blocks stay open.
- **Question card:** questions and approvals are an inline card at the bottom of the stream, not a modal. The card stays in the history with the answer.
  - Options come from the asker, as single-select or a checklist. Tools pass `choices` to `io.ask` (which gains a `multi` flag); the agent gets an `ask_user` capability with options and a single/multi flag.
  - There is always a free-text row, added by the UI.
  - Approvals (install, permissions, plan) keep their fixed options plus a text row for a note.
  - Keys: arrows or number keys move, Space ticks, Enter sends, typing starts the text row, Esc cancels (logged as a refusal).
- **Other views:** `/registry`, `/config` and `/tools` open full-screen views. Esc returns to the stream and the task keeps running underneath.

## 4b. Slash commands and tool forms (decided)

Typing `/` as the first character of the prompt opens an autocomplete list. Picking a command inserts it into the prompt box; the agent does not think about or execute commands. Commands run directly in the harness.

**Completion covers:**
- Command names with descriptions, filtered as you type; Tab or Enter picks one.
- Arguments of the chosen command: `--field` flags from the schema and enum values (for example `/model local|free|paid`).
- File paths for file arguments and `/attach`, relative to the project folder.

(Recent values per field were offered and not chosen.)

**One registry of commands.** Core commands (`/model`, `/new`, `/attach`, `/status`, `/undo`, `/registry`, `/config`, `/tools`, `/help`) and every tool in the registry are described by the same thing: a name, a description and an input schema. Tool names are shown in kebab-case (`/send-email` for `send_email`). The hardcoded `if text == "/..."` chain in `mini/ui/app.py` goes away.

**Every tool works three ways from one schema:**
1. From the terminal, as now: flags generated from `input_schema` (`mini tool NAME`, or the shim on PATH), with `io.ask` for anything left out.
2. From the mini UI as a slash command.
3. Called by the agent.

**Two interaction modes**, declared by the tool in its spec as `ui.mode`:
- `form`: the tool has independent inputs (for example subject, receiver, body, attachments). `/send-email` opens a modal form with one field per input; Enter runs it and Esc cancels. Anything the form already answered is passed as inputs, so `io.ask` does not ask again.
- `ask`: the tool drives a dialogue, with questions that depend on earlier answers or no fixed set of inputs. It starts straight away and each missing value is an inline question card, exactly as the agent's questions are.

Inline arguments prefill the form (`/send-email --to anna@example.com` opens it with the receiver filled in). A tool run from a slash command shows as a normal tool block with live output, and its result is added to the conversation so the agent knows it happened.

**Form fields are generated from `input_schema`:** string to text, number or integer to a number field, boolean to a switch, `enum` to a dropdown, array to a list field, a `file` input to a file picker, a secret to a masked field. The spec may add optional `ui` hints: `mode`, field `label`, field `order`, `multiline` for long text such as a mail body, and a `filter` for the file picker (for example `*.pdf`). The creator writes these hints; the harness validates that they only refer to schema properties.

## 5. Improvements to the overall design (proposed)

1. **One capability model.** Today there are built-in actions, `invoke_tool` on registry tools, and tool creation, with fuzzy boundaries. Make everything a capability with one schema: core ones in code, bundled and user ones in the registry. The agent sees one list and one calling convention. Gap detection becomes a deterministic search of that list instead of a model judgment.
2. **Enforcement in the harness, not the prompts.** Many watchdogs patch model behavior (`identical_call_repeat`, `file_rewrites`, `fail_streak_restart`, nudges). Prefer a smaller, stricter protocol: typed calls validated against the schema before they run, a reply without a valid call rejected and retried automatically, one generic duplicate-call detector. The pipeline (parse, confirm, gap, spec, code, tests, fix, restart, handover, consult) is long; measure which stages earn their cost and cut the rest.
3. **A cheaper path before creating a tool.** A core "run code" capability lets the agent write a script, run it in the sandbox against the named files, and only propose promoting it to a tool if it worked and looks reusable. This inverts the current flow and should cut hallucinated one-off tools.
4. **Model tiers behind one interface.** Local, free and paid tiers, escalation, consult and handover are spread through `core.py` (time budgets, token caps, `auto_escalate`, `offer_paid`, `consult`). Isolate them in one backend policy object (complete, can-continue, escalate) so the agent loop does not know about tiers.
5. **Split the big files along real seams.** `core.py` (633 lines) mixes the loop, tier switching, file actions and context. `creator.py` (716 lines) is the largest file. The catalog absorbs the file actions and the tier policy absorbs the switching.
6. **One event stream as the source of truth.** The UI, `mini doctor`, headless mode and the journal all consume the same structured events. This simplifies the UI work and makes the `.mini/` journal nearly free. Needs start/chunk/end events for thinking, tool output and questions, and live stdout/stderr from the sandbox.
7. **Behavioral tests.** Add a small replay suite: recorded tasks run against a scripted backend, asserting for example "no tool is created when core covers the step" and "no files change outside the folder".
8. **Smaller things.**
   - Drop the hackathon framing from the README and prompts. State one scope: a local-first agent that works on files in a git repo.
   - Fewer config knobs with sane defaults; the watchdog, cap and per-phase thinking settings go behind an "advanced" fold in the Config view.
   - Move checkable rules from `CREATE_RULES` (no `print`, relative paths, declared hosts) into harness validation so the prompt only teaches the shape of a tool.

### What to keep

Rootless sandbox with an egress allowlist; git-backed tools with staging, accept and rollback; the approval gate the agent cannot answer for; local-first inference.

## 6. Build order (proposed)

0. Commit or stash the current uncommitted work so the refactor starts from a clean base.
1. **Workspace and git guard:** copy in / export out, hidden-ref checkpoints, `mini undo`, git-required guard. Remove `pending`/`apply`.
2. **Project memory:** `.mini/` journal and run logs, fed into `Agent.context()`.
3. **Core catalog** with the capabilities above, a generated prompt section, the creator guard, and the run-code path (items 1 and 3 of section 5).
4. **Replay tests** (item 7), added alongside step 3 so later changes are checked.
5. **Bundled document tools** (PDF, docx, xlsx). Done: `mini/bundled/`, standard library only (PDF text extraction handles plain and Flate content streams, not scans, object streams or custom font encodings).
6. **Event model:** start/chunk/end events, live sandbox output, `multi` in `io.ask`. Done: `mini/harness/eventlog.py` (block kinds and fields in its docstring, `Sink`; the journal, `mini run` and the current UI panes are sinks), sandbox output streamed line by line, `choices`/`multi` through the approval gate.
7. **Stream UI:** markdown, thinking, tool and question blocks. Done: `mini/ui/app.py` (`Block`, `TextBlock`, `QuestionCard` driven straight from the event stream; the old chat, live and log panes are gone; Registry and Config stay as tabs until step 8).
8. **Full-screen views:** `/registry`, `/config`, `/tools`. Done.
8b. **Slash commands:** the command registry (core commands and tools from one schema), autocomplete, the modal form, `ui` hints in the spec and the creator prompt. Needs steps 3, 6 and 7. The CLI side (flags from the schema) already exists in `mini/toolcli.py`. Done: `mini/ui/commands.py` (registry, parsing, completion), `mini/harness/toolui.py` (hint validation, form fields from the schema; a file input is `"format": "file"`, a secret `"format": "password"`), `mini/ui/toolform.py` (the form), `mini/harness/toolrun.py` (a tool run for the user, shared with `mini tool`; from the UI it ends with a `run_end` marked `via: slash`, so the conversation and the journal have it). The bundled tools carry hints.
9. Tier policy extraction and the file splits (items 4 and 5), once the above has settled.
