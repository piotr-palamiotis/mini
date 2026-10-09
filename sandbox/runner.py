"""Tool runner: harness code, mounted read-only at /mini/runner.py in the sandbox.

Reads /work/.mini/input.json, imports /tool/tool.py, calls run(inputs), writes /work/.mini/output.json.
Any exception becomes {"error": ..., "message": ..., "traceback": ...} so the agent can diagnose it.
"""
import importlib.util
import json
import sys
import traceback


def main():
    inputs = json.load(open("/work/.mini/input.json"))
    try:
        spec = importlib.util.spec_from_file_location("tool", "/tool/tool.py")
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, "/tool")
        spec.loader.exec_module(mod)
        out = mod.run(inputs)
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
