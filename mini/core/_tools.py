"""Helpers shared by the registry capabilities."""


def describe_tool(m, full=True):
    def fields(schema):
        props = (schema or {}).get("properties") or {}
        req = set((schema or {}).get("required") or [])
        return ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', '?') if isinstance(v, dict) else '?'}"
                         + (f" ({v.get('description')})" if full and isinstance(v, dict) and v.get("description") else "")
                         for k, v in props.items()) or "-"
    src = m.get("created_by", {}).get("provider", "?")
    sets = ", ".join(f"{d['key']} ({d.get('description', '')})" for d in m.get("settings") or [])
    return (f"- {m['name']} (v{m['version']}, built by {src}): {m['description']}\n"
            f"    inputs: {fields(m.get('input_schema'))}\n    outputs: {fields(m.get('output_schema'))}"
            + (f"\n    saved settings: {sets}" if sets else ""))
