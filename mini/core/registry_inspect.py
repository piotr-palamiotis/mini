"""Everything about one installed tool."""
import json

from ._tools import describe_tool

NAME = "inspect_tool"
DESCRIPTION = ("Show everything about one installed tool: inputs and outputs with descriptions, how it works, "
               "permissions, saved settings, versions and its last test results.")
SCHEMA = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
COVERS = ("inspect tool", "describe tool", "tool detail", "show tool")


def run(call, args):
    reg = call.agent.registry
    m = reg.get(args["name"])
    if not m:
        return f"No installed tool named {args['name']!r}. Installed: {', '.join(reg.names()) or '(none)'}"
    lines = [describe_tool(m)]
    if m.get("interaction"):
        lines.append("how it works:\n" + "\n".join(f"  {i}. {x}" for i, x in enumerate(m["interaction"], 1)))
    lines.append(f"permissions: {json.dumps(m.get('permissions') or {})}")
    lines.append(f"versions: {reg.versions(m['name'])}" + (" (this one is staged by this task)" if m.get("_staged") else ""))
    if m.get("edge_cases"):
        lines.append("edge cases: " + "; ".join(map(str, m["edge_cases"])))
    tr = m.get("test_results") or {}
    if tr:
        lines.append(f"tests: {tr.get('summary', '')} ({'passed' if tr.get('passed') else 'failed'})")
    return "\n".join(lines)
