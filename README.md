# mini

A personal agent that builds its own tools. Started at the Dusk Till Dawn hackathon (Frankenstein track):
[demo video](https://youtu.be/CYJnR0hDpY8).

by Peter and Lukas

You give mini a task in plain language. It works out what the task needs and searches its tool registry.
When a capability is missing, it writes a spec, the code and the tests, runs the tests in a sandbox,
asks you to approve the install, and then finishes the task with the new tool. The next session finds the
tool in the registry and reuses it.

Inference is local first. A cloud model is only used after the local model runs out of time or attempts.

mini has two parts:

- **mini-harness** (`mini/harness/`, `sandbox/`) owns everything the agent must not control: model backends,
  the sandbox, the network allowlist, permissions, caps, watchdogs, the approval gate, the registry storage
  and the run log.
- **mini-agent** (`mini/agent/`) runs the loop: parse → confirm → gap detection → tool creation → execution.

## Quick start

```sh
python3 -m venv .venv && .venv/bin/pip install textual   # the harness itself is standard library only
.venv/bin/python -m mini setup                            # guided first-run setup (also offered on first launch)
.venv/bin/python -m mini doctor                           # check everything, change nothing
```

`mini setup` goes through each piece, says what is missing, and asks before it changes anything:

1. **Sandbox:** podman (prints the `sudo apt` command if missing) and the sandbox image (builds it).
2. **Local models:** a checklist of local backends: llama.cpp, and any other OpenAI-compatible server
   (LM Studio, vLLM, ...). Tick several and pick which comes first: each task runs on the first one that
   answers with its model, so the others are a fallback (`[local] use`, `[backends.*]`).
   - **llama.cpp** is built in `[llamacpp] dir` (setup asks where) from exactly the release mini is tested
     with, **`b11567` (commit `972b5fc`)**, for the GPU backend of the active preset (Vulkan, CUDA or CPU; each in
     its own `build*` folder, so both can exist). The pin lives in `mini/setup.py` (`LLAMA_TAG`,
     `LLAMA_COMMIT`); `doctor` checks that the built `llama-server` is that commit.
   - `llama-server` runs in **router mode** over `[llamacpp] models_dir`: every `NAME.gguf`, or `NAME/` folder
     with the model and its `mmproj-*.gguf` (vision), is a model, loaded on demand (`models_max` at once).
     Add a GGUF to the folder and it is available; no reconfiguration.
   - Setup checks each model's architecture (read from the GGUF header, looked up in the built library) and
     **load-tests** the model mini uses: a known architecture is not enough (Ollama's qwen3.5 9B conversion
     names a known architecture but this release rejects its rope metadata). Tested: gemma4 12B, Vulkan on
     an RX 6700 XT (64k context) and CUDA on an RTX 3070 (QAT build, 40k context, about 44 tokens/s), tool
     calls via `--jinja`, thinking via `--reasoning-format auto`, q8_0 KV cache.
   - **Presets** (`[presets.NAME]`) are named ways to run it: `gpu` (vulkan | cuda | cpu), `device`, `model`,
     `ctx`, `models_max`, `extra_args`. `[llamacpp] preset` is the active one, laid over `[llamacpp]`.
     `mini preset list`, `mini preset add NAME --gpu cuda --device CUDA0 --model M --ctx N [--args A]`,
     `mini preset use NAME` (builds that backend if missing, rewrites the service, restarts it, points the
     local tier at the preset's model). CUDA needs `sudo apt install nvidia-cuda-toolkit`. On 8 GB the 12B QAT
     model fits with the vision projector kept on the CPU (`--no-mmproj-offload`) up to about 49k context;
     the `rtx3070` preset uses 40960 for headroom.
   - Setup picks the GPU and the context size and runs the router as a systemd user service started at boot.
     It prints the `sudo apt` line for missing build tools.
3. **Start at boot:** user services keep running without a login (`loginctl enable-linger`).
4. **Cloud tiers (optional):** installs OmniRoute with npm if missing, runs it as a user service bound to
   127.0.0.1 with an API key required, walks you through the dashboard (providers, an API key, the combos
   `mini-free` and `mini-paid`), checks the key and saves it to `~/.config/mini/env` (mode 600). You can
   also rename the combos or turn a tier off.

It never replaces a systemd unit it did not write without asking (and keeps a `.bak`). The UI shows the state
of each tier at startup and on `/status`.

**Models from Ollama:** `scripts/ollama-export DEST` prints the commands that move an Ollama store's GGUF
files into the models folder layout (instant on the same disk, no copies); run them with
`scripts/ollama-export ~/models | sudo sh -e`. Draft models go to `DEST-drafts/`.

```sh
.venv/bin/python -m mini                                  # Textual UI
.venv/bin/python -m mini run "task" --attach file.pdf     # headless, approvals in the terminal
.venv/bin/python -m mini tools list|staged|review|accept|discard|show|rollback|shims|wipe
.venv/bin/python -m mini tool NAME [--field VALUE ...]   # run a tool; it asks for what you leave out
.venv/bin/python -m mini settings list|show|set|delete KEY
```

Configuration comes in two layers (every key is documented in `mini/harness/config.py`):

- `config/mini.toml` (in git): the shared settings, the same on every machine.
- `~/.config/mini/mini.toml` (never in git): this machine's own settings, laid over the shared file: the model,
  the GPU and presets, the llama.cpp and models folders, the backends and the cloud tiers. Everything mini
  changes (`mini setup`, `mini preset`, the UI's Config tab) is written here, so a `git pull` never conflicts
  with your settings and they never end up in a commit. Secrets go in `~/.config/mini/env`.
- `--config PATH` or `$MINI_CONFIG` names one file instead, read alone and written to (tests and benches).

The UI's Config tab is a form for the common settings (switches, dropdowns, number fields) that saves only what
you changed, in place, so the file's comments stay; a second sub-tab edits the machine file's raw TOML. Backends
and presets are managed with `mini preset` and `mini setup`.

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
   - **Install:** the tool is committed to its own git repo on the branch `task/<run>` (staged), with its
     manifest and test results. The task uses it right away; other tasks see it once you accept it.
5. **Execute.** The model invokes tools (each invocation runs in the sandbox), reads and writes files, and
   finishes. The harness checks each `finish` call: listed files must exist, and a plan that produces a file
   must have written one. If a tool misbehaves on the real input, `report_tool_bug` sends it back into the
   creation loop as a new version (at most 2 repairs per task). `need_tool` builds a capability discovered
   during execution.

## Watchdogs (all thresholds in `[watchdogs]` / `[caps]`)

| Watchdog | Trigger | Action |
|---|---|---|
| Repeated identical call | Same call 3x in a row | Warning on the 2nd, blocked from the 3rd |
| Repeated passing test | 2 unchanged reruns, still passing | Straight to review |
| File rewritten too often | More than 3 full rewrites of one file | Forced `explain` step: what fails, why, next change |
| Failed attempts in a row | 3 failing test runs after edits | Fresh conversation with spec, current files and failing output. Counts toward the 10 attempts. |
| Attempt budget | 10 attempts on one tool, per model tier | Local: the task moves to free. Then a dialog: advice, paid, or stop |
| Unparseable tool call | Parse or JSON error | "Resend as one small call", up to 5x. Not an attempt. |
| Empty reply / lost call | No tool call | One nudge. Not an attempt. |
| Runaway output | 8,192 tokens or 300 s per reply | The reply is discarded with "make smaller changes" |
| Local time budget | 15 min of local model time per task | The task moves to free and stays there |
| Time and turn limits | 20 min or 30 turns per tool creation / execution phase | Dialog: hint, restart, paid, stop |
| Hard run cap | 3600 s per run, 1.5M local tokens per run | Stop |
| Command policy | argv allowlist | Commands are argv lists and never touch a shell. Denials are logged. |
| Hardware | Swap grows by more than 250 MiB (`[watchdogs] swap_watch`, on by default) | Abort the run. The baseline is taken after the model is loaded. Growth is recorded even when the watchdog is off. |

`tests/test_watchdogs.py` checks that each one fires exactly at its threshold. It also checks that the attempt
budget stops at 10 and that restarts do not reset it.

## Your folder: changes in place, checkpointed in git

The folder you start `mini` in is the workspace (your home folder and `/` are refused, `[workspace] refuse`). It
must be a git repository: if it is not, mini says so and offers `git init`, and does nothing else until you agree.
The plan-confirm dialog shows the folder.

- The agent lists, reads and writes files itself (`list_files`, `read_file`, `write_file`), with paths relative to
  the folder. Edits happen in place, new files go where the task says. "The current folder" means this folder.
  No tool has to be built to list, read or write your files.
- Tools never see the folder. For each tool call the harness copies the files the call names into a fresh
  sandbox folder (same relative paths) and exports whatever the tool wrote back to the same relative path.
  Every path is confined to the folder, symlinks included; a symlink the tool creates is never followed, and
  nothing under `.git` is ever copied in or written.
- Listings are compact so a big folder cannot fill the model's context: names without their path, folders with
  their item count, files with their size, `depth` 1 to 3, at most 60 lines / 4,000 characters (about 1,000
  tokens), `.git`, `node_modules` and similar shown but not opened. The agent lists a subfolder to see inside it.
  Files are read 8,000 characters at a time (`offset` for the rest).
- Context guard: before each model call, older large tool results are shortened once the conversation passes 75%
  of the local model's context; if the server still refuses a request as too long, they are shortened further and
  the call is retried. When nothing is left to shorten the run stops with a clear message (it used to be reported
  as unparseable tool calls).
- **Checkpoints.** Before a task changes anything, mini snapshots the folder's tree (uncommitted and untracked
  files included) into the hidden ref `refs/mini/<run>` with git plumbing (a temporary index, `write-tree`,
  `commit-tree`, `update-ref`). Your index, branch and history are not touched. The ref also records which files
  the task wrote.
- `mini undo` (or `/undo` in the UI) puts back what the newest task changed; `mini undo RUN` a given task,
  `mini undo [RUN] FILE...` only those files, `mini undo --list` lists the checkpoints. Files a task created are
  deleted. An undo is checkpointed itself (`undo-<run>-<time>`), so it can be undone too.
  `tests/test_workspace.py` and `tests/test_checkpoint.py` check this.
- The llama-server prompt cache is capped (`--cache-ram 1024` in the default arguments). Without it the
  server's RAM grew by about 560 MB per long prompt on a 7.7 GB machine until it swapped.

## Sandbox

- Rootless Podman 4.9 (`[sandbox] engine`). Each run gets a fresh container: read-only root filesystem,
  `/tmp` as tmpfs, `--cap-drop ALL`, `no-new-privileges`, 1 GiB memory with no extra swap, 2 CPUs,
  256 PIDs and a timeout.
- Mounts: the tool files (read-only) at `/tool` and the call's own folder at `/work`, holding copies of the files
  the call names (previous section). Never your folder itself or its `.git`. Nothing else. No host environment
  variables (`HOME=/tmp`), no credentials, no home directory.
- Network: none by default. Declared and approved `host:port` targets put the container on a fresh
  `--internal` network with no route out. Each declared host resolves to an egress forwarder container
  (`sandbox/egress.py`), which forwards only to the allowlist and picks the destination by TLS SNI or HTTP
  Host. Every allow and deny is logged. `tests/test_policy.py` checks that undeclared hosts, raw IPs, the
  host loopback, the model server ports, host paths and host env vars are all unreachable.
- **Sandbox base image** (`sandbox/Containerfile.base`): `python:3.12-slim` (Debian 13.6, Python 3.12.14),
  plus `pytest 8.3.5` (with its dependencies `iniconfig`, `packaging`, `pluggy`) and a non-root user `tool`
  (uid 1000). Nothing else. Extra apt or pip packages are requested in the spec, approved in the dialog,
  built into a derived image `mini-sandbox:pkg-<hash>`, and logged.

## Tools are git repos

- Each tool lives in `state/tools/<name>/`, a git repository. `main` holds the accepted versions, one commit
  each, tagged `v1`, `v2`, ...
- A task never writes to `main`. What it builds or repairs is staged on `task/<run>`; tasks running in
  parallel each get their own branch.
- `mini tools staged` lists what waits for you, `mini tools review NAME` shows the diff, the tests and which
  model built it, `mini tools accept NAME` makes it the next version, `mini tools discard NAME` drops it. The
  Registry tab in the UI has the same actions.
- If another version was accepted after a task staged its own, accepting asks first: tools are generated
  whole, so it replaces that version instead of merging the two (`--force`, or press Accept twice).
- `[tools] auto_accept = true` skips the review step. `mini tools rollback NAME V` changes the active version.

## Tools that ask, and tools on your PATH

Nothing about a tool's dialogue is written into mini. When the agent specs a new tool it also plans how the
tool works for you: which **saved settings** it needs (an account, a server, a default) and what happens when
one is missing, which **questions** it asks and in what order, what it confirms before acting. With
`confirm_first` on, you see that plan before any code is written and can revise it in plain words.

- The tool contract is `run(inputs, io)`. `io.ask(key, prompt, kind)` returns the value when the caller passed
  it and otherwise asks: in the terminal, as a dialog in the UI, or (unattended) as an error that tells the
  agent to pass the value. Kinds: text, number, confirm, choice, file, secret. Tests drive it with
  `ScriptedIO` (`sandbox/mini_io.py`).
- Saved settings are declared in the manifest with a JSON schema; `state/settings/<key>.json`, mode 600.
  mini asks for a missing one before the tool starts. Secret fields are never echoed or logged, and a tool
  only gets the settings it declares. A network target can come from a setting (`setting:<key>`, fields
  host and port), so a tool can reach a server you name without the address being in the code.
- `mini tools accept NAME` also writes a command into `[tools] bin_dir` (`~/.local/bin`): run `NAME` with no
  arguments and it asks, or give everything in one line (`NAME --title x --body y --go`). If the name is
  taken by another command, it becomes `mini-NAME`. It runs in the same sandbox with the same permissions;
  the current directory is its folder (a git repository) and the files it writes land there, checkpointed like
  a task's.

## Model tiers

| Tier | What | When |
|---|---|---|
| `local` | llama.cpp `llama-server` (router over your models folder) on this machine | Every session starts here; `/model` changes it |
| `free` | OmniRoute combo `mini-free`: free cloud models with failover | Only when you pick it (`/model free`). Exists only when `[free]` is in the config |
| `paid` | OmniRoute combo `mini-paid`: your subscription | Only when you pick it (`/model paid`, `--model paid`, or "use paid" in a dialog). Exists only when `[paid]` is in the config |

- A cloud tier is on only when its section (`[free]`, `[paid]`) is in the config (`mini setup` writes it to
  `~/.config/mini/mini.toml`;
  `enabled = false` in a section switches it off again). With neither, nothing leaves the machine. mini never
  moves a task to another model by itself: a task that fails on its tier fails. `/model` changes the tier of the
  current session.
- Config: `[local]`, `[free]`, `[paid]` (this machine's values in `~/.config/mini/mini.toml`). Free and paid are OpenAI-compatible
  endpoints; the actual models behind `mini-free` and `mini-paid` are chosen in OmniRoute's dashboard.
- The OmniRoute API key is read from `$OMNIROUTE_API_KEY` or `~/.config/mini/env` (`OMNIROUTE_API_KEY=...`). It is
  never in the repo and never reaches a sandbox.
- A switch keeps the conversation: the new model carries on from where the old one stopped. After a used-up
  attempt budget it gets a fresh conversation with the spec, the files and the last failure instead.
- Every switch is logged as a `tier` event and shown in the UI, including that task data now leaves the machine.
  Tools record which tier built them (`created_by.provider`, `escalated_from`) and attempts per tier.
- `mini run --model free` runs on another tier.
