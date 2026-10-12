"""Read Ollama's model store: which models are there and where their GGUF files are, so llama-server can load
them without a second copy on disk."""
import json
import os
import re
import subprocess


def models_dir():
    if os.environ.get("OLLAMA_MODELS"):
        return os.environ["OLLAMA_MODELS"]
    try:
        unit = subprocess.run(["systemctl", "cat", "ollama"], capture_output=True, text=True).stdout
        m = re.search(r'^Environment="?OLLAMA_MODELS=([^"\n]+)', unit, re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return os.path.expanduser("~/.ollama/models")


def list_models(root=None):
    """[(name:tag, size in bytes)] of models whose weights are a readable GGUF."""
    root = root or models_dir()
    base = os.path.join(root, "manifests", "registry.ollama.ai")
    out = []
    for ns_dir, _, files in os.walk(base):
        for tag in files:
            name = os.path.relpath(ns_dir, base)
            name = name[len("library/"):] if name.startswith("library/") else name
            try:
                path = gguf(f"{name}:{tag}", root=root)
                out.append((f"{name}:{tag}", os.path.getsize(path)))
            except (OSError, ValueError, KeyError):
                pass
    return sorted(out)


def gguf(model, part="model", root=None):
    """Path of a model's GGUF layer (part: model | projector | draft). Raises ValueError/OSError."""
    root = root or models_dir()
    name, _, tag = model.partition(":")
    if "/" not in name:
        name = "library/" + name
    with open(os.path.join(root, "manifests", "registry.ollama.ai", name, tag or "latest")) as f:
        layers = json.load(f)["layers"]
    for layer in layers:
        if layer["mediaType"] == f"application/vnd.ollama.image.{part}":
            path = os.path.join(root, "blobs", layer["digest"].replace(":", "-"))
            with open(path, "rb") as f:
                if f.read(4) != b"GGUF":
                    raise ValueError(f"{path} is not a GGUF file")
            return path
    raise ValueError(f"{model} has no {part} layer")
