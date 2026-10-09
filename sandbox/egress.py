"""Egress forwarder: the only way out of a tool sandbox. Harness code, runs in its own container. Stdlib only.

The tool container sits on an --internal network with no route out. Every host the manifest declares (and the
user approved) is mapped in the tool's /etc/hosts to this forwarder, which listens on the declared ports and
forwards a connection only when its destination is in the allowlist:
  - a port with one declared host forwards to that host (any TCP protocol)
  - otherwise the destination comes from the TLS SNI name or the HTTP Host header
Every decision is printed as one JSON line (the harness copies them into the run log).

usage: python egress.py '<json list of "host:port">' ['<json {"host:port": "reset"}>']
The optional second argument is for failure-injection tests only: listed targets are accepted and then reset.
"""
import json
import select
import socket
import sys
import threading


def log(**kw):
    print(json.dumps(kw), flush=True)


def sni(data):
    """Server name from a TLS ClientHello, or None."""
    try:
        if data[0] != 0x16:
            return None
        i = 5 + 4 + 2 + 32                     # record hdr, handshake hdr, version, random
        i += 1 + data[i]                       # session id
        i += 2 + int.from_bytes(data[i:i + 2], "big")   # cipher suites
        i += 1 + data[i]                       # compression
        end = i + 2 + int.from_bytes(data[i:i + 2], "big")
        i += 2
        while i + 4 <= end:
            typ, ln = int.from_bytes(data[i:i + 2], "big"), int.from_bytes(data[i + 2:i + 4], "big")
            if typ == 0:
                n = int.from_bytes(data[i + 7:i + 9], "big")
                return data[i + 9:i + 9 + n].decode()
            i += 4 + ln
    except (IndexError, UnicodeDecodeError):
        return None
    return None


def http_host(data):
    for line in data.split(b"\r\n")[1:]:
        if line.lower().startswith(b"host:"):
            return line[5:].strip().decode(errors="replace").split(":")[0]
    return None


def pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 300)
            if not r:
                return
            for s in r:
                buf = s.recv(65536)
                if not buf:
                    return
                (b if s is a else a).sendall(buf)
    except OSError:
        return
    finally:
        a.close()
        b.close()


FAULTS = {}


def handle(conn, port, hosts):
    dest = None
    try:
        if len(hosts) == 1:
            dest = hosts[0]
            first = b""
        else:
            conn.settimeout(10)
            first = conn.recv(65536, socket.MSG_PEEK)
            dest = sni(first) or http_host(first)
            conn.settimeout(None)
            first = b""
        if dest not in hosts:
            log(event="deny", dest=dest, port=port, reason="destination not declared")
            conn.close()
            return
        if FAULTS.get(f"{dest}:{port}") == "reset":
            log(event="fault_injected", dest=dest, port=port, fault="reset")
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            conn.close()
            return
        up = socket.create_connection((dest, port), timeout=15)
        up.settimeout(None)
        log(event="allow", dest=dest, port=port)
        if first:
            up.sendall(first)
        pipe(conn, up)
    except OSError as e:
        log(event="error", dest=dest, port=port, error=str(e))
        conn.close()


def serve(port, hosts):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(64)
    while True:
        conn, _ = s.accept()
        threading.Thread(target=handle, args=(conn, port, hosts), daemon=True).start()


def main():
    targets = json.loads(sys.argv[1])
    if len(sys.argv) > 2:
        FAULTS.update(json.loads(sys.argv[2]))
    by_port = {}
    for t in targets:
        host, port = t.rsplit(":", 1)
        by_port.setdefault(int(port), []).append(host.lower())
    for port, hosts in by_port.items():
        threading.Thread(target=serve, args=(port, hosts), daemon=True).start()
    log(event="ready", ports=sorted(by_port))
    threading.Event().wait()


if __name__ == "__main__":
    main()
