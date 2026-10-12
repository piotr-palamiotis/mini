"""Read a GGUF file's metadata header (stdlib only): enough to know the model architecture before loading it."""
import struct

TYPES = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


def _str(f):
    (n,) = struct.unpack("<Q", f.read(8))
    return f.read(n).decode("utf-8", "replace")


def _skip(f, t):
    if t == 8:
        _str(f)
    elif t == 9:
        it, n = struct.unpack("<IQ", f.read(12))
        for _ in range(n):
            _skip(f, it)
    else:
        f.read(struct.calcsize(TYPES[t]))


def metadata(path, keys=("general.architecture", "general.name", "general.size_label"), limit=4096):
    """{key: value} for the requested string or number keys (stops once all are found)."""
    out = {}
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError(f"{path} is not a GGUF file")
        version, _, n_kv = struct.unpack("<IQQ", f.read(20))
        if version < 2:
            raise ValueError(f"{path}: GGUF version {version} is too old")
        for _ in range(min(n_kv, limit)):
            key = _str(f)
            (t,) = struct.unpack("<I", f.read(4))
            if key in keys and t == 8:
                out[key] = _str(f)
            elif key in keys and t in TYPES:
                (out[key],) = struct.unpack(TYPES[t], f.read(struct.calcsize(TYPES[t])))
            else:
                _skip(f, t)
            if len(out) == len(keys):
                break
    return out


def architecture(path):
    return metadata(path, ("general.architecture",)).get("general.architecture")
