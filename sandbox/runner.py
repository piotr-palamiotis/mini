"""Tool runner: harness code, mounted read-only at /mini/runner.py in the sandbox.

Reads /work/.mini/input.json (and settings.json when the tool declares settings), imports /tool/tool.py, calls
run(inputs, io) (or run(inputs) for tools without questions), writes /work/.mini/output.json.
Questions and messages travel as JSON lines on stdin/stdout (see mini_io.py); the tool's prints go to stderr.
Any exception becomes {"error": ..., "message": ..., "traceback": ...} so the agent can diagnose it.
"""
import importlib.util
import inspect
import json
import os
import sys
import traceback


def main():
    sys.path.insert(0, "/mini")
    import mini_io
    inputs = json.load(open("/work/.mini/input.json"))
    sp = "/work/.mini/settings.json"
    settings = json.load(open(sp)) if os.path.exists(sp) else {}
    io = mini_io.channel(inputs, settings)
    try:
        spec = importlib.util.spec_from_file_location("tool", "/tool/tool.py")
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, "/tool")
        spec.loader.exec_module(mod)
        takes_io = len(inspect.signature(mod.run).parameters) >= 2
        out = mod.run(inputs, io) if takes_io else mod.run(inputs)
        if not isinstance(out, dict):
            out = {"error": "TypeError", "message": f"run() returned {type(out).__name__}, expected dict"}
    except BaseException as e:   # noqa: BLE001 - everything is reported back
        out = {"error": type(e).__name__, "message": str(e)[:2000], "traceback": traceback.format_exc()[-3000:]}
    try:
        text = json.dumps(out, default=str)
    except (TypeError, ValueError) as e:
        text = json.dumps({"error": "SerializationError", "message": str(e)})
    open("/work/.mini/output.json", "w").write(text)
    sys.exit(1 if "error" in out else 0)


if __name__ == "__main__":
    main()
