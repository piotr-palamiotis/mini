"""Search the tool registry."""
from ._tools import describe_tool

NAME = "search_tools"
DESCRIPTION = "Search the installed tools of the registry by what they do. Lists the best matches with their inputs."
SCHEMA = {"type": "object", "required": ["query"], "properties": {
    "query": {"type": "string"}, "limit": {"type": "integer", "description": "default 5"}}}
COVERS = ("search tool", "find tool", "list tool", "installed tool", "search registry")


def run(call, args):
    reg = call.agent.registry
    found = reg.search(args["query"], limit=max(1, min(args.get("limit") or 5, 20)))
    if not found:
        return "The registry is empty."
    return f"{len(reg.names())} tools installed; best matches:\n" + "\n".join(describe_tool(m) for m in found)
