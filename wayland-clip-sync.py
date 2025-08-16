"""
clip-sync v2.1
Wayland clipboard sync (text + images) with robust logging.

- Watches clipboard (and primary, if present)
- Sends changes over TCP, applies on peer
- Uses hashes to prevent loops
- Detailed logs: use --verbose/--debug and optional --log-file
- Self diagnostics: --self-test

Tested on KDE/Plasma + Hyprland with wl-clipboard.
"""

import argparse, socket, threading, time, json, hashlib, subprocess, sys, os, shutil, logging, random, string
from typing import List, Tuple, Optional

LOG = logging.getLogger("clip-sync")

# --------------------------- util / logging ---------------------------

def setup_logging(verbosity:int=0, log_file:Optional[str]=None):
    level = logging.WARNING
    if verbosity >= 1: level = logging.INFO
    if verbosity >= 2: level = logging.DEBUG
    LOG.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    for h in handlers:
        h.setFormatter(fmt)
        LOG.addHandler(h)

def run(cmd: List[str], inp: bytes = None) -> Tuple[int, bytes, bytes]:
    LOG.debug("exec: %s", " ".join(cmd))
    p = subprocess.run(cmd, input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        LOG.debug("exec rc=%s stderr=%s", p.returncode, p.stderr.decode("utf-8", "ignore").strip())
    return p.returncode, p.stdout, p.stderr

def which_or(label: str, name: str) -> Optional[str]:
    path = shutil.which(name)
    if not path:
        LOG.error("%s not found in PATH", name)
    else:
        LOG.info("%s: %s", label, path)
    return path

def env_dump():
    LOG.info("env WAYLAND_DISPLAY=%s", os.environ.get("WAYLAND_DISPLAY"))
    LOG.info("env XDG_SESSION_TYPE=%s", os.environ.get("XDG_SESSION_TYPE"))
    LOG.info("env XDG_RUNTIME_DIR=%s", os.environ.get("XDG_RUNTIME_DIR"))
    LOG.info("env DISPLAY=%s", os.environ.get("DISPLAY"))
    LOG.info("PATH=%s", os.environ.get("PATH"))
    comp = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "?"
    LOG.info("desktop: %s", comp)

# --------------------------- wayland helpers ---------------------------

def have_primary() -> bool:
    # wl-paste returns rc=1 "Nothing is copied" even if primary exists.
    rc, _, err = run(["wl-paste", "--primary", "-l"])
    if rc == 0:
        LOG.info("primary selection: supported")
        return True
    msg = err.decode("utf-8", "ignore").lower()
    if "not supported" in msg:
        LOG.info("primary selection: NOT supported by compositor (%s)", msg.strip())
        return False
    LOG.info("primary selection: appears supported (currently empty)")
    return True

def list_mimes(selection: str) -> List[str]:
    cmd = ["wl-paste", "-l"]
    if selection == "primary":
        cmd.insert(1, "--primary")
    rc, out, err = run(cmd)
    if rc != 0:
        msg = err.decode("utf-8", "ignore").strip()
        if "nothing is copied" in msg.lower():
            LOG.debug("[%s] empty selection", selection)
            return []
        LOG.warning("[%s] wl-paste -l failed: %s", selection, msg)
        return []
    mimes = [l.strip() for l in out.decode("utf-8", "ignore").splitlines() if l.strip()]
    LOG.debug("[%s] offered mimes: %s", selection, mimes)
    return mimes

def choose_mime(mimes: List[str]) -> Optional[str]:
    prefs = [
        "image/png", "image/jpeg", "image/webp",
        "text/plain;charset=utf-8", "text/plain",
    ]
    for pref in prefs:
        for m in mimes:
            if m == pref or (pref.startswith("text/plain") and m.startswith("text/plain")):
                return m
    return mimes[0] if mimes else None

def read_clip(selection: str, mime: str) -> Optional[bytes]:
    cmd = ["wl-paste", "--type", mime]
    if selection == "primary":
        cmd.insert(1, "--primary")
    if mime.startswith("text/"):
        cmd.append("--no-newline")
    rc, out, err = run(cmd)
    if rc != 0:
        LOG.warning("[%s] wl-paste read failed (%s): %s", selection, mime, err.decode("utf-8","ignore").strip())
        return None
    return out

def set_clip(selection: str, mime: str, data: bytes):
    # Spawn wl-copy and don't wait for it (it stays around to own the selection).
    cmd = ["wl-copy", "--type", mime]
    if selection == "primary":
        cmd.insert(1, "--primary")
    # For text, avoid trailing newline surprises if you like:
    # if mime.startswith("text/"): cmd.append("--trim-newline")
    try:
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            p.stdin.write(data)
            p.stdin.close()
        except BrokenPipeError:
            LOG.warning("[%s] wl-copy pipe closed early (%s)", selection, mime)
        # Do NOT wait/communicate — let wl-copy daemonize/hold the selection.
        LOG.info("[%s] applied %s (%d bytes)", selection, mime, len(data))
    except Exception as e:
        LOG.exception("[%s] wl-copy exception: %s", selection, e)

def sha(data: bytes, mime: str) -> str:
    h = hashlib.sha256(); h.update(mime.encode()+b"\0"+data); return h.hexdigest()

# --------------------------- networking ---------------------------

class Peer:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = threading.Lock()
    def send(self, header: dict, payload: bytes) -> bool:
        try:
            msg = (json.dumps(header) + "\n").encode("utf-8")
            with self.lock:
                self.sock.sendall(msg)
                self.sock.sendall(payload)
            return True
        except Exception as e:
            LOG.error("send error: %s", e)
            return False

def _set_keepalive(sock: socket.socket):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        pass

# --------------------------- rx/tx loops ---------------------------

def rx_loop(sock: socket.socket, last_remote: dict, stop_evt: threading.Event):
    f = sock.makefile("rb")
    while not stop_evt.is_set():
        line = f.readline()
        if not line:
            LOG.warning("rx: peer closed stream")
            break
        try:
            hdr = json.loads(line.decode("utf-8"))
        except Exception as e:
            LOG.error("rx: bad header (%s)", e)
            continue
        if hdr.get("t") != "put":
            LOG.debug("rx: unknown msg %s", hdr)
            continue
        sel = hdr.get("sel","clipboard")
        mime = hdr.get("mime","text/plain")
        length = int(hdr.get("len",0))
        LOG.info("rx: %s <- %s (%d bytes)", sel, mime, length)
        data = f.read(length)
        if data is None or len(data)!=length:
            LOG.error("rx: length mismatch, expected %d got %s", length, len(data) if data is not None else None)
            break
        h = hdr.get("hash")
        last_remote[sel] = h
        set_clip(sel, mime, data)
    stop_evt.set()

def tx_loop(sock: socket.socket, selections: List[str], last_local: dict, last_remote: dict, interval: float, stop_evt: threading.Event):
    peer = Peer(sock)
    while not stop_evt.is_set():
        for sel in selections:
            mimes = list_mimes(sel)
            if not mimes:
                continue
            mime = choose_mime(mimes)
            if not mime:
                continue
            data = read_clip(sel, mime)
            if data is None:
                continue
            h = sha(data, mime)
            if h == last_local[sel]:
                LOG.debug("tx: %s unchanged (same local hash)", sel)
                continue
            if h == last_remote[sel]:
                LOG.debug("tx: %s last change was from remote (skip)", sel)
                continue
            header = {"t":"put","sel":sel,"mime":mime,"len":len(data),"hash":h}
            LOG.info("tx: %s -> %s (%d bytes)", sel, mime, len(data))
            if not peer.send(header, data):
                LOG.error("tx: send failed; stopping")
                stop_evt.set()
                break
            last_local[sel] = h
        time.sleep(interval)

# --------------------------- session / endpoints ---------------------------

def run_session(sock: socket.socket, primary_ok: bool, interval: float):
    _set_keepalive(sock)
    try:
        sock.settimeout(None)      # ensure blocking I/O (no 10s idle TimeoutError)
    except Exception:
        pass
    LOG.info("session start; primary_ok=%s interval=%.2fs", primary_ok, interval)
    selections = ["clipboard"] + (["primary"] if primary_ok else [])
    last_local = {s: None for s in selections}
    last_remote = {s: None for s in selections}
    stop = threading.Event()
    rt = threading.Thread(target=rx_loop, args=(sock,last_remote,stop), daemon=True)
    tt = threading.Thread(target=tx_loop, args=(sock,selections,last_local,last_remote,interval,stop), daemon=True)
    rt.start(); tt.start()
    while not stop.is_set():
        time.sleep(0.5)
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except:
        pass
    LOG.warning("session end")

def serve(bind: str, primary_ok: bool, interval: float):
    host, port = bind.split(":"); port = int(port)
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port)); s.listen(1)
        LOG.info("listening on %s", bind)
        conn, addr = s.accept(); LOG.info("accepted %s", addr)
        try:
            run_session(conn, primary_ok, interval)
        finally:
            try: conn.close()
            except: pass
            try: s.close()
            except: pass
        time.sleep(1)

def connect(addr: str, primary_ok: bool, interval: float):
    host, port = addr.split(":"); port = int(port)
    while True:
        try:
            LOG.info("connecting to %s", addr)
            s = socket.create_connection((host, port), timeout=10)
            LOG.info("connected to %s", addr)
            run_session(s, primary_ok, interval)
        except Exception as e:
            LOG.error("connect error: %s; retrying in 3s", e)
            time.sleep(3)

# --------------------------- diagnostics ---------------------------

def self_test() -> int:
    LOG.info("=== SELF-TEST START ===")
    env_dump()
    wlp = which_or("found wl-paste", "wl-paste")
    wlc = which_or("found wl-copy", "wl-copy")
    if not (wlp and wlc):
        LOG.error("missing wl-clipboard tools; aborting")
        return 2
    p = have_primary()
    token = "CLIP_SYNC_TEST_" + "".join(random.choice(string.ascii_uppercase) for _ in range(6))
    set_clip("clipboard", "text/plain;charset=utf-8", token.encode())
    time.sleep(0.2)
    m = list_mimes("clipboard")
    LOG.info("clipboard mimes after set: %s", m)
    rd = read_clip("clipboard", "text/plain")
    ok_text = (rd is not None and token in rd.decode("utf-8","ignore"))
    LOG.info("text roundtrip: %s", "OK" if ok_text else "FAIL")
    img_ok = any(x.startswith("image/") for x in m)
    LOG.info("image support present now? %s", img_ok)
    LOG.info("primary supported? %s", p)
    LOG.info("=== SELF-TEST END ===")
    return 0 if ok_text else 1

# --------------------------- main ---------------------------

def main():
    ap = argparse.ArgumentParser(description="Wayland clipboard sync peer with logging")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--listen", help="bind address, e.g. 0.0.0.0:8377")
    mode.add_argument("--connect", help="peer address, e.g. 192.168.1.10:8377")
    ap.add_argument("--interval", type=float, default=0.35, help="poll interval seconds (default 0.35)")
    ap.add_argument("--no-primary", action="store_true", help="disable syncing primary selection even if available")
    ap.add_argument("--verbose", action="store_true", help="INFO logs")
    ap.add_argument("--debug", action="store_true", help="DEBUG logs")
    ap.add_argument("--log-file", help="append logs to file")
    ap.add_argument("--self-test", action="store_true", help="run local diagnostics and exit")
    args = ap.parse_args()

    verbosity = 0
    if args.verbose: verbosity = 1
    if args.debug:   verbosity = 2
    setup_logging(verbosity, args.log_file)

    if args.self_test:
        sys.exit(self_test())

    env_dump()
    which_or("found wl-paste", "wl-paste")
    which_or("found wl-copy", "wl-copy")

    primary_ok = have_primary() and not args.no_primary

    if args.listen:
        serve(args.listen, primary_ok, args.interval)
    else:
        connect(args.connect, primary_ok, args.interval)

if __name__ == "__main__":
    if not os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_SESSION_TYPE") != "wayland":
        print("[clip-sync] WARNING: not in a Wayland session", file=sys.stderr)
    main()
