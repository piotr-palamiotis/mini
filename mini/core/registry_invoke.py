"""Run an installed tool in the sandbox: copy in the files its inputs name, export what it writes."""
import json
import os
import shutil
import time

from ._tools import describe_tool
from ..harness import workspace as ws
from ..harness.toolrun import failed
from ..harness.ask import GateAsker
from ..harness.sandbox import PolicyError, network_diagnosis
from ..harness.settings import MissingSetting

NAME = "invoke_tool"
DESCRIPTION = ("Run an installed tool in the sandbox. inputs follow its input schema; pass every input you already "
               "know, the tool asks the user only for what is missing and saved settings are filled in. Long data a "
               "script made (many rows) is not copied by hand: the script writes the inputs object as JSON to "
               ".mini/data/NAME.json and inputs_file names it.")
SCHEMA = {"type": "object", "required": ["name"], "properties": {
    "name": {"type": "string"}, "inputs": {"type": "object"},
    "inputs_file": {"type": "string", "description": "NAME.json a run_code script saved to .mini/data/: the inputs "
                    "object; keys in inputs are added over it"}}}
COVERS = ("invoke tool", "run tool", "call tool", "use tool")


def repair(call, args):
    """The local model often puts the tool's name inside inputs, or leaves it out next to a long inputs object.
    Take it from inputs when it names an installed tool that has no input called name; else, when exactly one
    installed tool accepts every input key given, that is the tool."""
    if args.get("name") or not isinstance(args.get("inputs"), dict):
        return args
    reg, inputs = call.agent.registry, args["inputs"]
    n = inputs.get("name")
    if isinstance(n, str) and reg.get(n) and "name" not in ((reg.get(n).get("input_schema") or {}).get("properties") or {}):
        return {**args, "name": n, "inputs": {k: v for k, v in inputs.items() if k != "name"}}
    if inputs:
        fits = [t for t in reg.names()
                if set(inputs) <= set(((reg.get(t) or {}).get("input_schema") or {}).get("properties") or {})]
        if len(fits) == 1:
            return {**args, "name": fits[0]}
    return args


def _fitting(a, inputs, other_than):
    """Installed tools whose required inputs are all given and that take every input given; built ones first."""
    built = getattr(a, "built_names", None) or []
    out = []
    for t in a.registry.names():
        sch = ((a.registry.get(t) or {}).get("input_schema") or {})
        props, req = set(sch.get("properties") or {}), set(sch.get("required") or [])
        if t != other_than and inputs and set(inputs) <= props and req <= set(inputs):
            out.append(t)
    return sorted(out, key=lambda t: (t not in built, t))


def _from_file(a, ref):
    """The inputs object a run_code script saved to .mini/data/ (in this run's folder), or (None, why not)."""
    base = os.path.basename(str(ref).replace("\\", "/"))
    p = os.path.join(a.run_dir, "data", base)
    if not os.path.isfile(p):
        have = sorted(os.listdir(os.path.join(a.run_dir, "data"))) if os.path.isdir(os.path.join(a.run_dir, "data")) else []
        return None, (f"No saved data named {base!r}" + (f" (saved: {', '.join(have)})" if have else "")
                      + ": a run_code script must write it to .mini/data/" + base + " first.")
    try:
        v = json.load(open(p))
    except ValueError as e:
        return None, f"{base} is not valid JSON: {e}"
    if not isinstance(v, dict):
        return None, f"{base} holds a {type(v).__name__}; it must be the tool's inputs object (input name -> value)."
    return v, ""


def run(call, args):
    a, name = call.agent, args["name"]
    m = a.registry.get(name)
    if not m:
        hint = f" {name} is a core capability: call it directly." if name in a.core else ""
        return f"No installed tool named {name!r}.{hint} Installed: {', '.join(a.registry.names()) or '(none)'}"
    inputs = dict(args.get("inputs") or {})
    if args.get("inputs_file"):
        saved, why = _from_file(a, args["inputs_file"])
        if saved is None:
            return f"{name} was not run: {why}"
        inputs = {**saved, **inputs}
    req = [k for k in ((m.get("input_schema") or {}).get("required") or []) if k not in inputs]
    if req:
        hint = ""
        fits = _fitting(a, inputs, other_than=name)
        if fits:                    # the inputs belong to another tool: the model mixed up two tools' schemas
            hint = (f"\nThe inputs you gave ({', '.join(sorted(inputs))}) are the inputs of {fits[0]}"
                    + (" (built for this task)" if fits[0] in (getattr(a, "built_names", None) or []) else "")
                    + f": call invoke_tool with name {fits[0]!r}.\n" + describe_tool(a.registry.get(fits[0])))
        return f"Missing required inputs for {name}: {', '.join(req)}.\n" + describe_tool(m) + hint
    a.calls += 1
    getattr(a, "invoked_any", set()).add(name)
    box = os.path.join(a.run_dir, "calls", f"{a.calls:03d}-{name}")      # the tool's /work for this call
    os.makedirs(box)
    try:
        inputs = ws.copy_in(a.folder, box, inputs, outside=a.attachments)
    except (OSError, PolicyError) as e:
        shutil.rmtree(box, ignore_errors=True)
        return f"{name} was not run: {e}"
    try:
        return _invoke_in(a, m, name, inputs, box)
    finally:
        shutil.rmtree(box, ignore_errors=True)


def _invoke_in(a, m, name, inputs, box):
    asker = GateAsker(a.gate, a.log, box, name, a.folder)
    try:
        settings = a.settings.resolve(m, asker)
        network = a.settings.network(m, settings)
    except MissingSetting as e:
        return (f"{name} cannot run: {e}. Tell the user in your answer what to save "
                f"(mini settings, or run the tool once from the terminal).")
    a.log.emit("invoke", tool=name, version=m["version"], inputs=inputs,
               settings=sorted(settings) or None, network=network or None)
    out, res = a.sandbox.invoke_tool(m, m["_dir"], box, inputs, on_message=asker, settings=settings, network=network)
    items, refused = ws.changes(a.folder, box)
    try:
        made = ws.make_dirs(a.folder, ws.new_dirs(a.folder, box))
    except (OSError, PolicyError):
        made = []
    written = []
    if items:
        try:
            written = a._change([rel for rel, _ in items], lambda: ws.export(a.folder, items))
        except (OSError, PolicyError) as e:
            refused.append(("(all files)", str(e)))
    a.last_invoke[name] = {"inputs": inputs, "output": out, "sandbox_tail": res["output"][-1500:],
                           "network": res.get("network_note") or None}
    a.log.emit("invoke", tool=name, version=m["version"], ok=not failed(out, res), output=out, exit_code=res["exit_code"])
    if not failed(out, res):
        a.invoked_ok.add(name)
    text = json.dumps(out, indent=1, default=str)
    if len(text) > 6000:
        p = os.path.join(a.run_dir, f"result-{name}-{int(time.time())}.json")
        open(p, "w").write(text)
        text = text[:5000] + f"\n[... output is {len(text)} characters; full JSON saved to {p} (read_file takes that path)]"
    if written:
        text += "\nFiles written to the folder: " + ", ".join(written)
    if made:
        text += "\nFolders made in the folder: " + ", ".join(d + "/" for d in made)
    if refused:
        text += "\nNot written: " + "; ".join(f"{rel} ({why})" for rel, why in refused)
        a.log.emit("watchdog", name="export_refused", tool=name, refused=refused)
    note = res.get("network_note") or (network_diagnosis(json.dumps(out), [], (m.get("permissions") or {}).get("network"))
                                       if failed(out, res) else "")
    if note:
        text += "\n" + note
        a.log.emit("watchdog", name="network_diagnosis", tool=name, note=note)
    if failed(out, res):
        text += ("\nThe tool failed. If this is a bug in the tool (not a problem with your inputs), "
                 "call report_tool_bug.")
    else:
        text += ("\nCheck this output against the request before you go on: is any part missing, empty, marked "
                 "as not found, or different from what was asked? If so, call report_tool_bug with what is wrong.")
    return text
