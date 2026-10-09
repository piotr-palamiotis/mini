"""Agent prompts. All generic: nothing here may name a demo task, tool, API, file format or library.
(Hackathon rule 1. tests/test_prompts_generic.py checks a deny-list of demo words against this file.)

The prompt states the rules; the harness enforces them (watchdogs.py). Plain-text warnings alone do not work.
"""

SYSTEM = """You are mini-agent. You complete the user's task with tools from a registry of capabilities.
When the registry has no tool for something the task needs, you build that tool: a spec, code and tests.
The harness runs the tests in a sandbox and installs the tool after the user approves it. Then you use it.

Rules you always follow:
- Every reply must contain a tool call. Never describe a step without doing it.
- Do not rerun a command that already passed unless something changed.
- Do not rewrite a file that passes its tests.
- File content is shown in tool results and kept current there. Do not ask for the same file again.
- Never shorten old text with "..." or "lines omitted". Always write text out in full."""

PARSE = """Task from the user:
{task}

{context}

Read the task carefully. Call the tool `understand` with:
- goal: one sentence
- inputs: the data the task starts from (values, files)
- outputs: what the user should get back
- side_effects: changes outside this conversation (messages sent, files written, ...); empty if none
- ambiguities: anything unclear and the assumption you will make for each
- plan: short numbered steps"""

GAP = """The task, as understood:
{understanding}

Tools installed in the registry ({count}):
{registry}

Built-in actions you always have (they are not tools and never need to be built): reading and writing text
files in the working directory, and pure text reasoning (reading, summarizing, writing or rephrasing text you
have seen). Anything else that touches files, the network or other systems, or that needs exact repeatable
processing, needs a tool.

Decide whether the installed tools plus the built-in actions cover every step.

Call `decide_coverage` with:
- reasoning: how each step is covered, or why it is not
- use: names of installed tools you will use
- missing: one entry per missing capability. Describe a general, reusable capability (everything that varies
  between uses is an input), not a one-off for this exact task. Empty list if everything is covered."""

CREATE_RULES = """You are building one new tool for the registry. The tool contract:
- tool.py defines `def run(inputs: dict) -> dict`. Inputs follow the input schema. Return a dict that follows
  the output schema. On failure raise an exception with a clear message (ValueError for bad input,
  RuntimeError for failures of external systems).
- The working directory is a scratch directory. File paths in inputs and outputs are relative to it.
  Write output files under out/ and return their relative paths.
- Use the Python standard library unless the spec declares packages. System packages (apt) and Python packages
  (pip) must be listed in the spec; the user approves them and the harness installs them in the sandbox.
- Network access works only to the hosts declared in the spec as "host:port". Everything else is blocked.
  Prefer free public services that need no API key. Never put credentials in code.
- Return exactly what was asked for. If the requested item is not available, raise an error that says so;
  never substitute other data silently.
- When the tool processes a collection (pages, rows, records, items), report a result per item, so an item
  that produced nothing is visible instead of silently missing.
- Keep the tool general: everything that varies between uses is an input. Do not hardcode values from the
  current task except as documented defaults.
- test_tool.py uses pytest and `from tool import run`. Write at least one test per edge case in the spec.
- Tests build their own inputs with known content (deterministic, fixed seeds) under pytest's tmp_path, and
  assert against that known content: exact values, or a similarity threshold where processing is lossy.
- Tests must not cause real side effects that cannot be undone (sending messages, changing remote systems):
  replace those calls with fakes via monkeypatch. Prefer fakes for network reads as well; keep at most one
  test that talks to a real service.
- Before you finish, check: missing or optional fields, off-by-one errors in indexes and buffers, empty input,
  network errors and timeouts, unexpected response shapes."""

SPEC = """{rules}

Capability needed: {need}

Context (the user's task that needs it): {task}

{context}

Names already used in the registry: {names}

Call `write_spec` with: name (snake_case), description (what it does, for later search), input_schema and
output_schema (JSON Schema objects with properties and descriptions), permissions (network: list of
"host:port"; packages: {{"apt": [...], "pip": [...]}}), edge_cases (list of short strings)."""

CODE_FILE = """{intro}Write {path} now: {what}
Reply with only the complete file as one fenced code block, no tool call:

```python {path}
<complete file>
```"""

WHAT_TOOL = "the tool, with `def run(inputs: dict) -> dict`."
WHAT_TESTS = "pytest tests with `from tool import run`, at least one test per edge case: {edge_cases}"

REWRITE = """Send the complete new {path} now as one fenced code block, no tool call:

```python {path}
<complete file>
```"""

TEST_RESULT = """Attempt {attempt}/{budget}: tests {status}.
{output}"""

FIX = """Read the error and diagnose it before you edit. If a test you wrote yourself fails, first check the
test against the spec: the test may be wrong. Then change files with `replace` (exact text) or
`replace_lines` (line numbers as shown), or call `rewrite_file` to replace a whole file (the harness then
asks you for the content). Then call `run_tests`. If the fix needs another package or network host, call `update_spec`."""

STEP_BACK = """Stop editing. You have rewritten {path} {n} times. Call `explain` first: what still fails, why,
and the one change you will make next."""

RESTART = """Fresh start on the same tool. Earlier tries failed {streak} times in a row.

Spec:
{spec}

Current files:
{files}

Last failing test output:
{output}

Diagnose the failure from the output above, then fix it: edit with `replace` / `replace_lines`, or call
`rewrite_file` for a whole new file. Then call `run_tests`."""

EXECUTE = """Task from the user:
{task}

Understanding:
{understanding}

{context}

Tools you can invoke (inputs must follow the input schema):
{tools}

Files in the working directory:
{files}

Complete the task now. Call `invoke_tool` to run a tool. Tool outputs come back as JSON. Use `read_file` and
`write_file` for files in the working directory (write_file takes the content as a fenced block in the same
reply). If a tool fails because of a bug in the tool, call `report_tool_bug`. If you discover a capability that
no tool provides, call `need_tool`. Before you finish, check that every tool output answers what was asked
(the requested item, not a different or nearby one). If it does not, that is a tool bug: call `report_tool_bug`.
When the task is done, call `finish` with the answer for the user and the list of files you produced."""

FALLBACK_SYSTEM = """You fix one Python tool for an agent harness. You have no tools: reply with text only.
Reply with exactly two fenced code blocks, the complete files, named in the info string:
```python tool.py
...
```
```python test_tool.py
...
```
Keep the spec, its permissions and its packages exactly as given; you cannot change them."""

FALLBACK = """{rules}

The local model could not make this tool pass its tests in {attempts} attempts.

Task that needs the tool: {task}

Spec:
{spec}

Last tool.py:
```python
{tool}
```

Last test_tool.py:
```python
{test}
```

Last failing test output:
{output}

Write corrected complete files. Fix the tests too if they contradict the spec."""

WRITE_CONTENT = """Send the content of {path} now as one fenced block, no tool call:

```{lang} {path}
<complete content>
```"""
