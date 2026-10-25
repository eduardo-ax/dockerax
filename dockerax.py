#!/usr/bin/env python3
"""dockerax — Docker container dashboard for the terminal.

Lightweight: Python standard library only, talks straight to the Docker API
over /var/run/docker.sock. Shows the host, containers (grouped by compose),
live graphs, logs, processes, events, images, volumes and networks — and
always the equivalent `docker` command for every action.

Press ? inside the program for shortcuts and what every number means.
"""
import curses
import errno
import http.client
import json
import locale
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote

SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
INTERVAL = 2.0
HIST = 300
FPS = 10
VERSION = "2.0"

# palette (catppuccin mocha) — converted to 256 colors when drawing
TEXT = (205, 214, 244)
SUB = (166, 173, 200)
DIM = (127, 132, 156)
FAINT = (69, 71, 90)
LINE = (88, 91, 112)
BAR_BG = (49, 50, 68)
SEL_BG = (58, 60, 82)
BASE = (30, 30, 46)
BLUE = (137, 180, 250)
MAUVE = (203, 166, 247)
GREEN = (166, 227, 161)
YELLOW = (249, 226, 175)
PEACH = (250, 179, 135)
RED = (243, 139, 168)
TEAL = (148, 226, 213)
SKY = (137, 220, 235)
PINK = (245, 194, 231)
LAVENDER = (180, 190, 254)

G_LOAD = [GREEN, YELLOW, PEACH, RED]
G_MEM = [BLUE, LAVENDER, MAUVE, PINK]
G_RX = [TEAL, SKY, BLUE]
G_TX = [PEACH, PINK, MAUVE]
G_SIZE = [TEAL, BLUE, MAUVE]

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
APP_BG = (24, 24, 37)
CONFIG = os.path.expanduser("~/.config/dockerax.json")
BLOCKS = " ▁▂▃▄▅▆▇█"
VIEWS = ["containers", "images", "volumes", "networks"]
TABS = ["metrics", "logs", "info", "processes"]

TIPS = [
    "press ? to learn what every number means",
    "port 8080→80 = host:8080 leads to port 80 inside the container",
    "CPU 100% = one full core; with 8 cores the maximum is 800%",
    "e opens a shell inside the container (docker exec -it)",
    "l opens live logs · z enlarges the bottom panel",
    "the events panel shows everything happening in docker in real time",
    "exit code 137 = the container was killed (SIGKILL or out of memory)",
    "volumes keep data outside the container: they survive docker rm",
    "containers on the same compose network find each other by service name",
    "use the mouse: click to select, wheel to scroll",
    "/ filters by name or image",
    "b toggles solid/transparent background · g toggles block/braille graphs",
]


def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w") as f:
            json.dump(cfg, f)
    except OSError:
        pass


def nice_ceil(v):
    """round up to a nice value (1, 2, 5 × 10^n)"""
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * e:
            return m * e
    return 10 * e


# ════════════════════════════════════════════════════════════ docker api
class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=15):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def api(method, url, timeout=15, body=None):
    conn = UnixHTTP(SOCK, timeout)
    try:
        if body is None:
            conn.request(method, url)
        else:
            conn.request(method, url, json.dumps(body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            msg = body.decode(errors="replace")
            try:
                msg = json.loads(msg).get("message", msg)
            except ValueError:
                pass
            raise RuntimeError(msg.strip())
        return json.loads(body) if body else None
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════ helpers
def human(n, suffix=""):
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}{suffix}" if unit == "B" else f"{n:.1f}{unit}{suffix}"
        n /= 1024
    return f"{n:.1f}P{suffix}"


def ago(sec):
    sec = max(0, sec)
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec // 60)} min"
    if sec < 86400:
        return f"{int(sec // 3600)} h"
    if sec < 86400 * 60:
        return f"{int(sec // 86400)} days"
    if sec < 86400 * 365:
        return f"{int(sec // (86400 * 30))} months"
    return f"{int(sec // (86400 * 365))} years"


def parse_ts(s):
    """docker RFC3339 (with nanoseconds) -> epoch, or None."""
    if not s or s.startswith("0001"):
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|[\x00-\x08\x0b-\x1f\x7f]")


def clean(s):
    return ANSI.sub("", s.replace("\t", "    ").rstrip("\r"))


STATUS_SHORT = [
    (r"^Created$", "created, never started"),
    (r"Less than a second", "<1s"),
    (r"About a minute", "~1 min"),
    (r"About an hour", "~1 h"),
    (r"(\d+) seconds?", r"\1s"),
    (r"(\d+) minutes?", r"\1 min"),
    (r"(\d+) hours?", r"\1 h"),
]


def status_text(s):
    """docker status without the health suffix and with shorter units."""
    s = re.sub(r"\s*\((healthy|unhealthy|health: starting)\)", "", s or "")
    for pat, rep in STATUS_SHORT:
        s = re.sub(pat, rep, s)
    return s


def fmt_ports(ports):
    seen, out = set(), []
    for p in sorted(ports or [], key=lambda p: (p.get("PrivatePort", 0), p.get("PublicPort", 0))):
        if p.get("PublicPort"):
            s = f"{p['PublicPort']}→{p['PrivatePort']}"
        else:
            s = f"{p['PrivatePort']}"
        if p.get("Type", "tcp") != "tcp":
            s += f"/{p['Type']}"
        if s not in seen:  # docker repeats the port for ipv4 and ipv6
            seen.add(s)
            out.append(s)
    return " ".join(out)


def host_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # sends nothing, just picks the interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"


def read_cpu_times():
    out = []
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            v = list(map(int, line.split()[1:9]))
            out.append((v[3] + v[4], sum(v)))  # idle+iowait, total
    return out


def read_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) * 1024
    return info


def read_net():
    rx = tx = 0
    with open("/proc/net/dev") as f:
        for line in list(f)[2:]:
            name, data = line.split(":", 1)
            name = name.strip()
            if name == "lo" or name.startswith(("veth", "docker", "br-", "virbr")):
                continue
            v = data.split()
            rx += int(v[0])
            tx += int(v[8])
    return rx, tx


def docker_run_cmd(ins):
    """Rebuild (approximately) the docker run that would create this container."""
    hc, cfg = ins.get("HostConfig") or {}, ins.get("Config") or {}
    parts = ["docker run -d", f"--name {ins.get('Name', '').lstrip('/')}"]
    rp = (hc.get("RestartPolicy") or {}).get("Name")
    if rp and rp != "no":
        parts.append(f"--restart {rp}")
    seen = set()
    for cport, binds in sorted((hc.get("PortBindings") or {}).items()):
        cp, _, proto = cport.partition("/")
        for b in binds or []:
            s = f"{b.get('HostPort') or cp}:{cp}"
            if b.get("HostIp") not in (None, "", "0.0.0.0", "::"):
                s = f"{b['HostIp']}:{s}"
            if proto and proto != "tcp":
                s += f"/{proto}"
            if s not in seen:
                seen.add(s)
                parts.append(f"-p {s}")
    for m in ins.get("Mounts") or []:
        if m.get("Type") == "volume":
            anon = re.fullmatch(r"[0-9a-f]{64}", m.get("Name", ""))
            s = m["Destination"] if anon else f"{m['Name']}:{m['Destination']}"
        elif m.get("Type") == "bind":
            s = f"{m['Source']}:{m['Destination']}"
        else:
            continue
        parts.append(f"-v {s}" + ("" if m.get("RW", True) else ":ro"))
    net = hc.get("NetworkMode")
    if net and net not in ("default", "bridge"):
        parts.append(f"--network {net}")
    if hc.get("Memory"):
        parts.append(f"--memory {human(hc['Memory'])}")
    if hc.get("NanoCpus"):
        parts.append(f"--cpus {hc['NanoCpus'] / 1e9:g}")
    parts.append(cfg.get("Image", "?"))
    return " ".join(parts)


RESTARTS = ["no", "unless-stopped", "always", "on-failure"]
RUN_FIELDS = ["name", "host", "cport", "mode", "rm", "restart"]
RUN_HINTS = {
    "name": "--name: a fixed name to use in other commands (docker logs NAME). Empty = docker picks a random one.",
    "host": "-p HOST:CONTAINER · the port opened on this machine. Empty = not published (only reachable from docker networks).",
    "cport": "the port the app listens on inside the container. The image's EXPOSE tells you which one.",
    "mode0": "-d (detached): runs in the background and gives the terminal back. Right for servers like nginx.",
    "mode1": "-it (interactive): attaches your keyboard to it. For shells (ubuntu, python). Leaves dockerax until it exits.",
    "rm": "--rm: delete the container automatically when it stops. Great for tests: no leftovers in docker ps -a.",
    "restart0": "--restart no: if it stops or the machine reboots, it stays stopped.",
    "restart1": "--restart unless-stopped: comes back after crashes and reboots, unless you stopped it yourself.",
    "restart2": "--restart always: comes back after crashes and reboots, even if you had stopped it.",
    "restart3": "--restart on-failure: restarts only when it exits with an error (exit code ≠ 0).",
}


def port_user(port, rows):
    """Who already holds this host port: a container name, 'another program' or None."""
    for r in rows:
        for p in r["port_list"]:
            if p.get("PublicPort") == port:
                return r["name"]
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("", port))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            return "another program"
    finally:
        s.close()
    return None


def run_argv(f):
    """The docker run command the run form stands for."""
    a = ["docker", "run", "-d" if f["mode"] == 0 else "-it"]
    if f["rm"]:
        a.append("--rm")
    if f["name"]:
        a += ["--name", f["name"]]
    if f["host"] and f["cport"]:
        a += ["-p", f"{f['host']}:{f['cport']}"]
    if f["restart"]:
        a += ["--restart", RESTARTS[f["restart"]]]
    a.append(f["ref"])
    return a


def split_ref(ref):
    """'nginx' -> ('docker.io', 'library/nginx', 'latest', default_tag=True)."""
    name, tag, digest = ref, "", ""
    if "@" in name:
        name, digest = name.split("@", 1)
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        reg, path = first, rest
    else:
        reg, path = "docker.io", name
        if "/" not in path:
            path = "library/" + path
    return reg, path, digest or tag or "latest", not (tag or digest)


def pull_stream(ref, on_msg, keep):
    """POST /images/create and feed each progress message (a dict) to on_msg.
    keep(conn) receives the connection so the pull can be cancelled by closing it."""
    name, tag = ref, ""
    if "@" not in ref and ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    url = f"/images/create?fromImage={quote(name, safe='')}" + (f"&tag={quote(tag, safe='')}" if tag else "&tag=latest" if "@" not in ref else "")
    conn = UnixHTTP(SOCK, timeout=600)
    keep(conn)
    try:
        conn.request("POST", url)
        resp = conn.getresponse()
        if resp.status >= 400:
            msg = resp.read().decode(errors="replace")
            try:
                msg = json.loads(msg).get("message", msg)
            except ValueError:
                pass
            raise RuntimeError(msg.strip())
        while True:
            line = resp.readline()
            if not line:
                break
            try:
                on_msg(json.loads(line))
            except ValueError:
                continue
    finally:
        conn.close()


def wrap(text, width):
    out, line = [], ""
    for word in text.split(" "):
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = "  " + word
        else:
            line = f"{line} {word}" if line else word
    if line:
        out.append(line)
    return out


# ════════════════════════════════════════════════════════════ collectors
class Cache:
    """Background fetch with expiry; get() never blocks the screen."""

    def __init__(self, workers=4):
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.lock = threading.Lock()
        self.data = {}
        self.pending = set()

    def get(self, key, fn, ttl):
        with self.lock:
            ent = self.data.get(key)
            if (ent is None or time.time() - ent[0] > ttl) and key not in self.pending:
                self.pending.add(key)
                self.pool.submit(self._run, key, fn)
        return ent[1] if ent else None

    def _run(self, key, fn):
        try:
            v = fn()
        except Exception as e:  # noqa: BLE001 - the error becomes the value and is shown
            v = e
        with self.lock:
            self.data[key] = (time.time(), v)
            self.pending.discard(key)

    def invalidate(self, pred=lambda k: True):
        with self.lock:
            for k in [k for k in self.data if pred(k)]:
                del self.data[k]


class Collector(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=12)
        self.rows = []
        self.hist = {}   # id -> {"cpu","mem","rx","tx"}
        self.prev = {}   # id -> (t, cpu_total, system_total, rx, tx)
        self.host = {}
        self.hh = {k: deque(maxlen=HIST) for k in ("cpu", "mem", "rx", "tx")}
        self.info = {}
        self.error = None
        self.polls = 0
        self.last_poll = 0.0
        self._cpu = read_cpu_times()
        self._net = (time.time(), *read_net())

    def run(self):
        while True:
            t0 = time.time()
            try:
                if self.polls % 15 == 0:
                    self.info = api("GET", "/info") or {}
                self.poll()
                self.error = None
            except Exception as e:  # noqa: BLE001 - shown on screen
                self.error = str(e)
            self.polls += 1
            self.last_poll = time.time()
            self.wake.wait(max(0.2, INTERVAL - (time.time() - t0)))
            self.wake.clear()

    @staticmethod
    def _stats(cid):
        try:
            return api("GET", f"/containers/{cid}/stats?stream=false&one-shot=true")
        except Exception:  # noqa: BLE001
            return None

    def poll(self):
        now = time.time()
        cpu = read_cpu_times()
        pcts = []
        for (i, t), (pi, pt) in zip(cpu, self._cpu):
            dt = t - pt
            pcts.append(max(0.0, 100.0 * (1 - (i - pi) / dt)) if dt > 0 else 0.0)
        self._cpu = cpu
        mem = read_mem()
        rx, tx = read_net()
        t0, prx, ptx = self._net
        dt = max(1e-3, now - t0)
        rxr, txr = max(0, rx - prx) / dt, max(0, tx - ptx) / dt
        self._net = (now, rx, tx)
        try:
            du = shutil.disk_usage(self.info.get("DockerRootDir") or "/")
        except OSError:
            du = shutil.disk_usage("/")

        lst = api("GET", "/containers/json?all=1")
        running = [c["Id"] for c in lst if c["State"] == "running"]
        stats = dict(zip(running, self.pool.map(self._stats, running)))
        now = time.time()

        rows = []
        for c in lst:
            cid = c["Id"]
            labels = c.get("Labels") or {}
            status = c.get("Status", "")
            m = re.search(r"\((healthy|unhealthy|health: starting)\)", status)
            ex = re.search(r"(?:Exited|Restarting) \((-?\d+)\)", status)
            row = {
                "id": cid,
                "name": (c.get("Names") or ["?"])[0].lstrip("/"),
                "image": c.get("Image", ""),
                "image_id": c.get("ImageID", ""),
                "state": c["State"],
                "status": status,
                "health": {"health: starting": "starting"}.get(m.group(1), m.group(1)) if m else None,
                "exit": int(ex.group(1)) if ex else None,
                "ports": fmt_ports(c.get("Ports")),
                "port_list": c.get("Ports") or [],
                "compose": labels.get("com.docker.compose.project", ""),
                "service": labels.get("com.docker.compose.service", ""),
                "created": c.get("Created", 0),
                "mounts": c.get("Mounts") or [],
                "networks": {k: v.get("IPAddress", "") for k, v in
                             ((c.get("NetworkSettings") or {}).get("Networks") or {}).items()},
                "cpu": 0.0, "mem": 0, "mem_limit": 0,
                "rx": 0, "tx": 0, "rx_rate": 0.0, "tx_rate": 0.0,
                "blk_r": 0, "blk_w": 0, "pids": 0,
            }
            st = stats.get(cid)
            if st:
                cs = st.get("cpu_stats") or {}
                tot = (cs.get("cpu_usage") or {}).get("total_usage", 0)
                sysu = cs.get("system_cpu_usage", 0)
                ncpu = cs.get("online_cpus") or os.cpu_count() or 1
                ms = st.get("memory_stats") or {}
                mst = ms.get("stats") or {}
                inactive = mst.get("inactive_file", mst.get("total_inactive_file", 0))
                row["mem"] = max(0, ms.get("usage", 0) - inactive)
                row["mem_limit"] = ms.get("limit", 0)
                nets = (st.get("networks") or {}).values()
                crx = sum(n.get("rx_bytes", 0) for n in nets)
                ctx = sum(n.get("tx_bytes", 0) for n in nets)
                row["rx"], row["tx"] = crx, ctx
                prev = self.prev.get(cid)
                if prev:
                    dsys, dtot = sysu - prev[2], tot - prev[1]
                    if dsys > 0 and dtot >= 0:
                        row["cpu"] = dtot / dsys * ncpu * 100.0
                    pdt = now - prev[0]
                    if pdt > 0:
                        row["rx_rate"] = max(0, crx - prev[3]) / pdt
                        row["tx_rate"] = max(0, ctx - prev[4]) / pdt
                self.prev[cid] = (now, tot, sysu, crx, ctx)
                for e in ((st.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []):
                    op = e.get("op", "").lower()
                    if op == "read":
                        row["blk_r"] += e.get("value", 0)
                    elif op == "write":
                        row["blk_w"] += e.get("value", 0)
                row["pids"] = (st.get("pids_stats") or {}).get("current", 0)
            rows.append(row)

        with self.lock:
            for r in rows:
                if r["state"] != "running":
                    continue
                h = self.hist.setdefault(r["id"], {k: deque(maxlen=HIST) for k in ("cpu", "mem", "rx", "tx")})
                h["cpu"].append(r["cpu"])
                h["mem"].append(r["mem"])
                h["rx"].append(r["rx_rate"])
                h["tx"].append(r["tx_rate"])
            alive = {r["id"] for r in rows}
            for d in (self.hist, self.prev):
                for k in [k for k in d if k not in alive]:
                    del d[k]
            used = mem["MemTotal"] - mem["MemAvailable"]
            self.rows = rows
            self.host = {
                "cpu": pcts[0], "cores": pcts[1:],
                "mem": used, "mem_total": mem["MemTotal"],
                "swap": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0), "swap_total": mem.get("SwapTotal", 0),
                "disk": du.used, "disk_total": du.total,
                "rx": rxr, "tx": txr, "load": os.getloadavg(),
            }
            self.hh["cpu"].append(pcts[0])
            self.hh["mem"].append(used / mem["MemTotal"] * 100)
            self.hh["rx"].append(rxr)
            self.hh["tx"].append(txr)


EV = {
    ("container", "create"): ("✚", BLUE, "created"),
    ("container", "start"): ("▶", GREEN, "started"),
    ("container", "restart"): ("↻", YELLOW, "restarted"),
    ("container", "pause"): ("‖", BLUE, "paused"),
    ("container", "unpause"): ("▶", GREEN, "resumed"),
    ("container", "destroy"): ("✕", RED, "removed"),
    ("container", "oom"): ("!", RED, "ran out of memory (OOM)"),
    ("container", "rename"): ("✎", BLUE, "renamed"),
    ("image", "pull"): ("⇣", TEAL, "image pulled"),
    ("image", "delete"): ("✕", RED, "image deleted"),
    ("image", "tag"): ("#", BLUE, "tagged"),
    ("volume", "create"): ("✚", BLUE, "volume created"),
    ("volume", "destroy"): ("✕", RED, "volume removed"),
    ("network", "create"): ("✚", BLUE, "network created"),
    ("network", "destroy"): ("✕", RED, "network removed"),
}
EXIT_INFO = {0: "normal", 1: "application error", 125: "docker error", 126: "command not executable",
             127: "command not found", 130: "Ctrl+C", 137: "killed: SIGKILL or out of memory",
             139: "segmentation fault", 143: "stopped with SIGTERM"}


class EventFeed(threading.Thread):
    """Subscribes to docker's /events stream and describes each event."""

    def __init__(self, on_event, since=0):
        super().__init__(daemon=True)
        self.items = deque(maxlen=300)
        self.on_event = on_event
        self.since = max(int(time.time()) - 6 * 3600, int(since) + 1)
        self.connected = False

    def run(self):
        filters = quote(json.dumps({"type": ["container", "image", "volume", "network"]}))
        while True:
            try:
                conn = UnixHTTP(SOCK, timeout=None)
                conn.request("GET", f"/events?since={self.since}&filters={filters}")
                resp = conn.getresponse()
                self.connected = True
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    if line.strip():
                        self.handle(json.loads(line))
            except Exception:  # noqa: BLE001 - reconnects on its own
                pass
            self.connected = False
            time.sleep(2)

    def handle(self, ev):
        typ, action = ev.get("Type", ""), ev.get("Action", "")
        actor = ev.get("Actor") or {}
        attrs = actor.get("Attributes") or {}
        t = ev.get("time", time.time())
        self.since = int(t) + 1
        name = attrs.get("name") or actor.get("ID", "")[:20]
        if typ == "container" and action == "die":
            code = int(attrs.get("exitCode", 0) or 0)
            why = EXIT_INFO.get(code, "error")
            item = ("■", DIM if code == 0 else RED, f"exited {code} · {why}")
        elif typ == "container" and action.startswith("health_status"):
            hs = action.split(":", 1)[-1].strip()
            item = {"healthy": ("♥", GREEN, "healthy"),
                    "unhealthy": ("♥", RED, "unhealthy: healthcheck failed")}.get(hs, ("♥", YELLOW, hs))
        elif (typ, action) in EV:
            item = EV[(typ, action)]
        else:
            return
        if typ == "image":
            name = actor.get("ID", name)
            if name.startswith("sha256:"):
                name = name[7:19]
        elif typ == "volume" and re.fullmatch(r"[0-9a-f]{64}", name):
            name = name[:12]
        self.items.appendleft((t, item[0], item[1], name, item[2], typ, actor.get("ID", "")))
        self.on_event(typ, actor.get("ID", ""))


class LogTail(threading.Thread):
    """docker logs -f for one container, demultiplexing stdout/stderr."""

    def __init__(self, cid, tty):
        super().__init__(daemon=True)
        self.cid, self.tty = cid, tty
        self.lines = deque(maxlen=3000)
        self.partial = {1: "", 2: ""}
        self.conn = None
        self.stopped = False
        self.done_at = None
        self.error = None

    def run(self):
        try:
            self.conn = UnixHTTP(SOCK, timeout=None)
            self.conn.request("GET", f"/containers/{self.cid}/logs?stdout=1&stderr=1&follow=1&tail=300&timestamps=1")
            resp = self.conn.getresponse()
            if resp.status >= 400:
                raise RuntimeError(resp.read().decode(errors="replace"))
            while not self.stopped:
                if self.tty:
                    data = resp.read1(65536)
                    if not data:
                        break
                    self.feed(1, data)
                else:
                    hdr = resp.read(8)
                    if len(hdr) < 8:
                        break
                    self.feed(2 if hdr[0] == 2 else 1, resp.read(int.from_bytes(hdr[4:8], "big")))
        except Exception as e:  # noqa: BLE001
            if not self.stopped:
                self.error = str(e)
        self.done_at = time.time()

    def feed(self, stream, data):
        text = self.partial[stream] + data.decode(errors="replace")
        *lines, self.partial[stream] = text.split("\n")
        for line in lines:
            ts, _, msg = line.partition(" ")
            self.lines.append((stream, parse_ts(ts), clean(msg)))

    def stop(self):
        self.stopped = True
        try:
            self.conn.sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001
            pass


# ════════════════════════════════════════════════════════════ colors
def to256(r, g, b):
    def q(v):
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    levels = (0, 95, 135, 175, 215, 255)
    ri, gi, bi = q(r), q(g), q(b)
    dc = (levels[ri] - r) ** 2 + (levels[gi] - g) ** 2 + (levels[bi] - b) ** 2
    gray = max(0, min(23, round(((r + g + b) / 3 - 8) / 10)))
    gv = 8 + gray * 10
    dg = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return 232 + gray if dg < dc else 16 + 36 * ri + 6 * gi + bi


def to8(r, g, b):
    if max(r, g, b) - min(r, g, b) < 40:
        return curses.COLOR_WHITE
    basic = {curses.COLOR_RED: (220, 60, 60), curses.COLOR_GREEN: (60, 200, 60),
             curses.COLOR_YELLOW: (220, 200, 60), curses.COLOR_BLUE: (60, 100, 220),
             curses.COLOR_MAGENTA: (200, 60, 200), curses.COLOR_CYAN: (60, 200, 200)}
    return min(basic, key=lambda k: sum((a - b) ** 2 for a, b in zip(basic[k], (r, g, b))))


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def grad(stops, t):
    t = max(0.0, min(1.0, t))
    seg = t * (len(stops) - 1)
    i = min(int(seg), len(stops) - 2)
    return lerp(stops[i], stops[i + 1], seg - i)


class Pal:
    def __init__(self, solid=True):
        curses.start_color()
        try:
            curses.use_default_colors()
            self.term_bg = -1
        except curses.error:
            self.term_bg = curses.COLOR_BLACK
        self.full = curses.COLORS >= 256
        self.limit = min(curses.COLOR_PAIRS, 256)
        self.set_solid(solid)

    def set_solid(self, solid):
        """solid paints APP_BG; otherwise uses the terminal's (maybe transparent) background."""
        self.solid = solid and self.full
        self.dflt = to256(*APP_BG) if self.solid else self.term_bg
        self.pairs = {}

    def idx(self, rgb):
        if rgb is None:
            return self.dflt
        return to256(*rgb) if self.full else to8(*rgb)

    def __call__(self, fg, bg=None):
        key = (self.idx(fg), self.idx(bg))
        p = self.pairs.get(key)
        if p is None:
            n = len(self.pairs) + 1
            p = 0
            if n < self.limit:
                try:
                    curses.init_pair(n, *key)
                    p = curses.color_pair(n)
                except curses.error:
                    pass
            self.pairs[key] = p
        return p


BOLD = curses.A_BOLD
# braille bits: left/right column, bottom-up and top-down
BR_UP_L = (0, 0x40, 0x44, 0x46, 0x47)
BR_UP_R = (0, 0x80, 0xA0, 0xB0, 0xB8)
BR_DN_L = (0, 0x01, 0x03, 0x07, 0x47)
BR_DN_R = (0, 0x08, 0x18, 0x38, 0xB8)


# ════════════════════════════════════════════════════════════ ui
class UI:
    def __init__(self, scr, col):
        self.scr = scr
        self.col = col
        self.cfg = load_config()
        self.pal = Pal(self.cfg.get("solid", True))
        self.blocks = self.cfg.get("graph", "blocks") == "blocks"
        self.cache = Cache()
        self.feed = EventFeed(self.on_event, self.cfg.get("events_cleared", 0))
        self.feed.start()
        self.view = 0
        self.tab = 0
        self.sel = [0, 0, 0, 0]
        self.sel_key = [None] * 4
        self.scroll = [0, 0, 0, 0]
        self.sort = "default"
        self.show_all = True
        self.filter = ""
        self.typing = False
        self.zoom = False
        self.help = False
        self.help_scroll = 0
        self.confirm = None   # (question, command, callback)
        self.form = None      # run form state (images view, r)
        self.pull = None      # pull dialog state (images view, p)
        self.goto_img = None  # (image ref, open the run form?) to select once it shows up
        self.goto = None      # container id to select once it shows up
        self.rows = []
        self.msg = None       # (text, color, command, t)
        self.pending = set()  # ids with an action in progress
        self.logs = None
        self.detail_scroll = 0
        self.ease_vals = {}
        self.hits = []
        self.regions = {}
        self.frame = 0
        self.h = self.w = 0
        self.ip = host_ip()
        self.hostname = socket.gethostname()
        self.started = time.time()

    # ------------------------------------------------------------ events
    def on_event(self, typ, aid):
        self.col.wake.set()
        self.cache.invalidate(lambda k: k[0] in ("images", "volumes", "networks", "df")
                              or (len(k) > 1 and k[1] == aid))

    # ------------------------------------------------------------ primitives
    def put(self, y, x, text, attr=0, maxx=None):
        if y < 0 or y >= self.h or not text:
            return 0
        lim = self.w if maxx is None else min(self.w, maxx)
        if y == self.h - 1:
            lim = min(lim, self.w - 1)
        if x < 0:
            text, x = text[-x:], 0
        text = text[:max(0, lim - x)]
        if text:
            try:
                self.scr.addstr(y, x, text, attr)
            except curses.error:
                pass
        return len(text)

    def segs(self, y, x, parts, maxx=None, bg=None):
        """parts: [(text, color, bold?)]"""
        for p in parts:
            text, color = p[0], p[1]
            bold = BOLD if len(p) > 2 and p[2] else 0
            x += self.put(y, x, text, self.pal(color, bg) | bold, maxx)
        return x

    def box(self, y, x, h, w, title="", color=LINE, right=None, rcolor=DIM, tcolor=TEXT):
        if h < 2 or w < 4:
            return
        a = self.pal(color)
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", a)
        side = "│"
        for i in range(1, h - 1):
            self.put(y + i, x, side, a)
            self.put(y + i, x + w - 1, side, a)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", a)
        if title:
            self.put(y, x + 2, f" {title} ", self.pal(tcolor) | BOLD, x + w - 2)
        if right and len(right) + len(title) + 8 < w:
            self.put(y, x + w - len(right) - 4, f" {right} ", self.pal(rcolor))

    def hbar(self, y, x, w, pct, stops, bg=None):
        if w <= 0:
            return
        fill = max(0.0, min(1.0, pct / 100)) * w
        full = int(fill)
        run, run_attr, cx = "", None, x
        for i in range(w):
            if i < full:
                ch, attr = "━", self.pal(grad(stops, (i + 0.5) / w), bg)
            elif i == full and fill - full >= 0.5:
                ch, attr = "╸", self.pal(grad(stops, (i + 0.5) / w), bg)
            else:
                ch, attr = "━", self.pal(FAINT, bg)
            if attr != run_attr and run:
                cx += self.put(y, cx, run, run_attr)
                run = ""
            run_attr = attr
            run += ch
        self.put(y, cx, run, run_attr)

    def graph(self, y, x, w, h, data, maxv, stops, bg=None, invert=False):
        """Braille area graph: 2 samples per column, 4 levels per row."""
        if w <= 0 or h <= 0:
            return
        if self.blocks:
            return self.block_graph(y, x, w, h, data, maxv, stops, bg, invert)
        vals = list(data)[-(w * 2):]
        vals = [None] * (w * 2 - len(vals)) + vals
        top = h * 4
        lv = []
        for v in vals:
            if v is None or maxv <= 0:
                lv.append(0)
            else:
                n = int(round(v / maxv * top))
                lv.append(max(1 if v > 0 else 0, min(top, n)))
        tl, tr = (BR_DN_L, BR_DN_R) if invert else (BR_UP_L, BR_UP_R)
        for row in range(h):
            base = row * 4 if invert else (h - 1 - row) * 4
            chars = []
            for cx in range(w):
                a = max(0, min(4, lv[2 * cx] - base))
                b = max(0, min(4, lv[2 * cx + 1] - base))
                chars.append(chr(0x2800 | tl[a] | tr[b]) if a or b else " ")
            t = (row + 0.5) / h if invert else (h - 1 - row + 0.5) / h
            line = "".join(chars)
            if line.strip() or bg is not None:
                self.put(y + row, x, line, self.pal(grad(stops, t), bg))

    def block_graph(self, y, x, w, h, data, maxv, stops, bg=None, invert=False):
        """Same graph with solid blocks: 1 sample per column, 8 levels per row."""
        vals = list(data)[-w:]
        vals = [None] * (w - len(vals)) + vals
        top = h * 8
        lv = []
        for v in vals:
            if v is None or maxv <= 0:
                lv.append(0)
            else:
                lv.append(max(1 if v > 0 else 0, min(top, int(round(v / maxv * top)))))
        for row in range(h):
            base = row * 8 if invert else (h - 1 - row) * 8
            chars = []
            for n in lv:
                f = max(0, min(8, n - base))
                if invert:
                    chars.append("█" if f >= 8 else "▀" if f >= 4 else "▔" if f else " ")
                else:
                    chars.append(BLOCKS[f])
            t = (row + 0.5) / h if invert else (h - 1 - row + 0.5) / h
            line = "".join(chars)
            if line.strip() or bg is not None:
                self.put(y + row, x, line, self.pal(grad(stops, t), bg))

    def ease(self, key, target, k=0.35):
        v = self.ease_vals.get(key, target)
        v += (target - v) * k
        self.ease_vals[key] = v
        return v

    def flash(self, text, color=TEXT, cmd=None):
        self.msg = (text, color, cmd, time.time())

    def hit(self, y, x0, x1, fn):
        self.hits.append((y, x0, x1, fn))

    # ------------------------------------------------------------ data
    def snapshot(self):
        with self.col.lock:
            return (list(self.col.rows), dict(self.col.host),
                    {k: list(v) for k, v in self.col.hh.items()})

    def hist_of(self, cid):
        with self.col.lock:
            h = self.col.hist.get(cid)
            return {k: list(v) for k, v in h.items()} if h else {"cpu": [], "mem": [], "rx": [], "tx": []}

    def container_items(self, rows):
        if not self.show_all:
            rows = [r for r in rows if r["state"] == "running"]
        if self.filter:
            f = self.filter.lower()
            rows = [r for r in rows if f in r["name"].lower() or f in r["image"].lower() or f in r["compose"].lower()]
        if self.sort != "default":
            key = {"cpu": lambda r: -r["cpu"], "mem": lambda r: -r["mem"], "name": lambda r: r["name"]}[self.sort]
            return sorted(rows, key=key)
        rows = sorted(rows, key=lambda r: (r["state"] != "running", r["name"]))
        groups = {}
        for r in rows:
            groups.setdefault(r["compose"], []).append(r)
        if list(groups) == [""]:
            return rows
        items = []
        for proj in sorted(groups, key=lambda p: (p == "", p)):
            members = groups[proj]
            on = sum(1 for r in members if r["state"] == "running")
            items.append({"_group": proj or "standalone", "on": on, "total": len(members), "compose": bool(proj)})
            for i, r in enumerate(members):
                items.append(dict(r, _tree="└─ " if i == len(members) - 1 else "├─ "))
        return items

    # ------------------------------------------------------------ actions
    def act(self, cli, method, url, ok, ident=None):
        self.flash(f"running… {cli}", BLUE)
        if ident:
            self.pending.add(ident)

        def work():
            try:
                api(method, url, timeout=120)
                self.flash(f"✓ {ok}", GREEN, cli)
            except Exception as e:  # noqa: BLE001
                self.flash(f"✗ {e}", RED, cli)
            self.pending.discard(ident)
            self.cache.invalidate()
            self.col.wake.set()
        threading.Thread(target=work, daemon=True).start()

    def ask(self, question, cli, fn):
        self.confirm = (question, cli, fn)

    def container_action(self, r, key):
        n, cid = r["name"], r["id"]
        running = r["state"] == "running"
        if key == "s":
            if running:
                self.ask(f"Stop container {n}?", f"docker stop {n}",
                         lambda: self.act(f"docker stop {n}", "POST", f"/containers/{cid}/stop", f"{n} stopped", cid))
            else:
                self.act(f"docker start {n}", "POST", f"/containers/{cid}/start", f"{n} started", cid)
        elif key == "r":
            self.ask(f"Restart container {n}?", f"docker restart {n}",
                     lambda: self.act(f"docker restart {n}", "POST", f"/containers/{cid}/restart", f"{n} restarted", cid))
        elif key == "p":
            if r["state"] == "paused":
                self.act(f"docker unpause {n}", "POST", f"/containers/{cid}/unpause", f"{n} resumed", cid)
            elif running:
                self.act(f"docker pause {n}", "POST", f"/containers/{cid}/pause", f"{n} paused (processes frozen)", cid)
        elif key == "x":
            if running or r["state"] == "paused":
                self.flash(f"stop the container before removing it (s) — {n} is running", YELLOW)
            else:
                self.ask(f"Remove container {n}? (named volumes are kept)", f"docker rm {n}",
                         lambda: self.act(f"docker rm {n}", "DELETE", f"/containers/{cid}", f"{n} removed", cid))
        elif key == "e":
            if running:
                self.exec_shell(r)
            else:
                self.flash("the container must be running to open a shell", YELLOW)

    def exec_shell(self, r):
        curses.def_prog_mode()
        curses.endwin()
        n = r["name"]
        print(f"\n\033[1;38;5;183m$ docker exec -it {n} sh\033[0m   \033[2m(type exit to return to dockerax)\033[0m\n")
        rc = subprocess.call(["docker", "exec", "-it", r["id"], "sh", "-c",
                              "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"])
        if rc not in (0, 130):
            input(f"\n\033[2m(exited with code {rc} — the image may not have a shell) press enter to return\033[0m")
        curses.reset_prog_mode()
        self.scr.clear()
        curses.curs_set(0)
        self.flash(f"back from {n} shell", BLUE, f"docker exec -it {n} sh")

    # ------------------------------------------------------------ run form
    def open_run(self, item):
        ref = item["tag"] if not item["dangling"] else item["short"]
        self.form = {"img": item, "ref": ref, "name": "", "host": "", "cport": "", "mode": 0, "rm": False,
                     "restart": 0, "focus": 0, "auto": True, "err": None, "chk": None}

    def form_check(self):
        """Validate the form; returns an error message or None."""
        f = self.form
        if f["name"]:
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", f["name"]):
                return "name: letters, digits, _ . - only (and not starting with a symbol)"
            if any(r["name"] == f["name"] for r in self.rows):
                return f"a container named {f['name']} already exists"
        if f["host"]:
            if not f["cport"]:
                return "say which container port the host port leads to"
            if not 0 < int(f["host"]) < 65536 or not re.fullmatch(r"\d{1,5}(/(tcp|udp|sctp))?", f["cport"]):
                return "ports go from 1 to 65535 (container port may end in /udp)"
            who = self.host_port_user()
            if who:
                return f"host port {f['host']} is already used by {who}"
        return None

    def host_port_user(self):
        f = self.form
        if not f["host"]:
            return None
        if not f["chk"] or f["chk"][0] != f["host"]:
            f["chk"] = (f["host"], port_user(int(f["host"]), self.rows))
        return f["chk"][1]

    def form_key(self, k):
        f = self.form
        field = RUN_FIELDS[f["focus"]]
        f["err"] = None
        if k == 27:
            self.form = None
            self.flash("cancelled", DIM)
        elif k in (10, 13, curses.KEY_ENTER):
            self.submit_run()
        elif k in (9, curses.KEY_DOWN):
            f["focus"] = (f["focus"] + 1) % len(RUN_FIELDS)
        elif k in (curses.KEY_BTAB, curses.KEY_UP):
            f["focus"] = (f["focus"] - 1) % len(RUN_FIELDS)
        elif field in ("name", "host", "cport"):
            if k in (curses.KEY_BACKSPACE, 127, 8):
                f[field] = f[field][:-1]
            elif 32 < k < 127:
                ch = chr(k)
                ok = {"name": r"[a-zA-Z0-9_.-]", "host": r"\d", "cport": r"[\d/a-z]"}[field]
                if re.fullmatch(ok, ch) and len(f[field]) < (40 if field == "name" else 9):
                    f[field] += ch
            if field == "cport":
                f["auto"] = False
        elif k in (32, curses.KEY_LEFT, curses.KEY_RIGHT):
            d = -1 if k == curses.KEY_LEFT else 1
            if field == "mode":
                f["mode"] = (f["mode"] + d) % 2
            elif field == "rm":
                f["rm"] = not f["rm"]
                if f["rm"]:
                    f["restart"] = 0   # docker refuses --rm together with --restart
            elif field == "restart":
                f["restart"] = (f["restart"] + d) % len(RESTARTS)
                if f["restart"]:
                    f["rm"] = False

    def submit_run(self):
        f = self.form
        err = self.form_check()
        if err:
            f["err"] = err
            return
        self.form = None
        argv = run_argv(f)
        cli = " ".join(argv)
        if f["mode"] == 1:
            return self.run_interactive(argv, f["ref"])
        body = {"Image": f["ref"], "HostConfig": {"AutoRemove": f["rm"], "RestartPolicy": {"Name": RESTARTS[f["restart"]]}}}
        if f["host"]:
            cp = f["cport"] if "/" in f["cport"] else f["cport"] + "/tcp"
            body["ExposedPorts"] = {cp: {}}
            body["HostConfig"]["PortBindings"] = {cp: [{"HostPort": f["host"]}]}
        url = "/containers/create" + (f"?name={quote(f['name'])}" if f["name"] else "")
        self.flash(f"running… {cli}", BLUE)

        def work():
            try:
                cid = api("POST", url, timeout=60, body=body)["Id"]
                try:
                    api("POST", f"/containers/{cid}/start", timeout=60)
                except RuntimeError:
                    # docker run would leave a dead "created" container behind; don't
                    api("DELETE", f"/containers/{cid}?force=1")
                    raise
                self.goto, self.filter = cid, ""
                self.set_view(0)
                self.set_tab(1)
                self.flash("✓ container started · showing its logs", GREEN, cli)
            except Exception as e:  # noqa: BLE001
                self.flash(f"✗ {e}", RED, cli)
            self.cache.invalidate()
            self.col.wake.set()
        threading.Thread(target=work, daemon=True).start()

    def run_interactive(self, argv, ref):
        curses.def_prog_mode()
        curses.endwin()
        cli = " ".join(argv)
        print(f"\n\033[1;38;5;183m$ {cli}\033[0m   \033[2m(exit or Ctrl+C to return to dockerax)\033[0m\n")
        try:
            rc = subprocess.call(argv)
        except KeyboardInterrupt:
            rc = 130
        if rc not in (0, 130):
            input(f"\n\033[2m(exited with code {rc}) press enter to return\033[0m")
        curses.reset_prog_mode()
        self.scr.clear()
        curses.curs_set(0)
        self.flash(f"back from {ref}", BLUE, cli)
        self.cache.invalidate()
        self.col.wake.set()

    # ------------------------------------------------------------ pull
    def open_pull(self):
        self.pull = {"text": "", "phase": "input", "layers": {}, "lock": threading.Lock(),
                     "status": "", "err": None, "conn": None, "ref": "", "t0": 0, "t1": 0}

    def pull_key(self, k):
        pl = self.pull
        if pl["phase"] == "input":
            if k == 27:
                self.pull = None
            elif k in (10, 13, curses.KEY_ENTER):
                if pl["text"]:
                    self.start_pull()
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                pl["text"] = pl["text"][:-1]
            elif 32 < k < 127 and re.fullmatch(r"[a-zA-Z0-9._/:@-]", chr(k)) and len(pl["text"]) < 120:
                pl["text"] += chr(k)
        elif pl["phase"] == "pulling":
            if k == 27:
                pl["phase"] = "cancelled"
                try:
                    pl["conn"].sock.shutdown(socket.SHUT_RDWR)
                except (AttributeError, OSError):
                    pass
                self.pull = None
                self.flash("pull cancelled (layers already downloaded are kept)", YELLOW, f"docker pull {pl['ref']}")
        else:
            self.pull = None
            if pl["phase"] == "done" and k in (ord("r"), 10, 13, curses.KEY_ENTER):
                self.goto_img = (pl["ref"], k == ord("r"))

    def start_pull(self):
        pl = self.pull
        reg, path, tag, default = split_ref(pl["text"])
        pl["ref"] = pl["text"] + (":latest" if default else "")
        pl["phase"], pl["t0"] = "pulling", time.time()
        cli = f"docker pull {pl['ref']}"

        def on_msg(m):
            with pl["lock"]:
                if m.get("error"):
                    raise RuntimeError(m["error"])
                lid, st = m.get("id"), m.get("status", "")
                pd = m.get("progressDetail") or {}
                if lid and re.fullmatch(r"[0-9a-f]{12,64}", lid):
                    ly = pl["layers"].setdefault(lid, {"st": "", "cur": 0, "total": 0, "size": 0})
                    ly["st"] = st
                    if pd.get("total"):
                        ly["cur"], ly["total"] = pd.get("current", 0), pd["total"]
                        if st == "Downloading":
                            ly["size"] = pd["total"]
                elif st:
                    pl["status"] = st

        def work():
            try:
                pull_stream(pl["ref"], on_msg, lambda c: pl.__setitem__("conn", c))
                if pl["phase"] == "cancelled":
                    return
                pl["phase"], pl["t1"] = "done", time.time()
                up = "up to date" in pl["status"]
                self.flash(f"✓ {pl['ref']} " + ("was already up to date" if up else "downloaded"), GREEN, cli)
            except Exception as e:  # noqa: BLE001
                if pl["phase"] == "cancelled":
                    return
                pl["phase"], pl["err"], pl["t1"] = "error", str(e), time.time()
                self.flash(f"✗ pull failed: {e}", RED, cli)
            self.cache.invalidate()
        threading.Thread(target=work, daemon=True).start()

    def resource_action(self, item, key):
        if self.view == 1:
            ref = item["tag"] if item["tag"] != "<none>:<none>" else item["short"]
            if key == "r":
                self.open_run(item)
            elif key == "x":
                if item["used"]:
                    self.flash(f"{ref} is used by {item['used']} container(s) — remove them first", YELLOW)
                    return
                self.ask(f"Delete image {ref}?", f"docker rmi {ref}",
                         lambda: self.act(f"docker rmi {ref}", "DELETE", f"/images/{quote(item['id'], safe='')}", f"{ref} deleted"))
            elif key == "P":
                self.ask("Delete all untagged (dangling) images?", "docker image prune",
                         lambda: self.act("docker image prune", "POST", "/images/prune", "dangling images deleted"))
        elif self.view == 2 and key == "x":
            n = item["name"]
            if item["users"]:
                self.flash(f"volume in use by {', '.join(u[0] for u in item['users'])}", YELLOW)
                return
            self.ask(f"Delete volume {n[:30]}? THE DATA WILL BE LOST", f"docker volume rm {n}",
                     lambda: self.act(f"docker volume rm {n}", "DELETE", f"/volumes/{quote(n, safe='')}", "volume deleted"))
        elif self.view == 3 and key == "x":
            n = item["name"]
            if n in ("bridge", "host", "none"):
                self.flash(f"{n} is a built-in docker network and cannot be removed", YELLOW)
                return
            self.ask(f"Remove network {n}?", f"docker network rm {n}",
                     lambda: self.act(f"docker network rm {n}", "DELETE", f"/networks/{item['id']}", f"network {n} removed"))

    # ------------------------------------------------------------ keyboard/mouse
    def move(self, d):
        self.sel[self.view] = max(0, self.sel[self.view] + d)
        self.sel_key[self.view] = None
        self.detail_scroll = 0

    def set_view(self, v):
        self.view, self.detail_scroll = v, 0

    def set_tab(self, t):
        self.tab, self.detail_scroll = t % len(TABS), 0

    def handle_key(self, k):
        if k == curses.KEY_MOUSE:
            return self.handle_mouse()
        if self.typing:
            if k in (10, 13, curses.KEY_ENTER):
                self.typing = False
            elif k == 27:
                self.typing, self.filter = False, ""
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                self.filter = self.filter[:-1]
            elif 32 <= k < 0x110000:
                self.filter += chr(k)
            self.sel[self.view], self.sel_key[self.view] = 0, None
            return True
        if self.pull:
            self.pull_key(k)
            return True
        if self.form:
            self.form_key(k)
            return True
        if self.confirm:
            q, cli, fn = self.confirm
            self.confirm = None
            if k in (ord("y"), ord("Y")):
                fn()
            else:
                self.flash("cancelled", DIM)
            return True
        if self.help:
            if k in (curses.KEY_DOWN, ord("j")):
                self.help_scroll += 1
            elif k in (curses.KEY_UP, ord("k")):
                self.help_scroll = max(0, self.help_scroll - 1)
            elif k == curses.KEY_NPAGE:
                self.help_scroll += 10
            elif k == curses.KEY_PPAGE:
                self.help_scroll = max(0, self.help_scroll - 10)
            else:
                self.help = False
            return True
        ch = chr(k) if 0 < k < 0x110000 else ""
        if ch == "q":
            return False
        if k == 27:
            if self.zoom:
                self.zoom = False
            elif self.filter:
                self.filter = ""
            else:
                return False
        elif ch == "?":
            self.help, self.help_scroll = True, 0
        elif ch in "1234" and ch:
            self.set_view(int(ch) - 1)
        elif ch == "/":
            self.typing = True
        elif ch == "z":
            self.zoom = not self.zoom
        elif ch == "b":
            self.pal.set_solid(not self.pal.solid)
            self.cfg["solid"] = self.pal.solid
            save_config(self.cfg)
            if self.pal.full:
                self.flash("solid background" if self.pal.solid else "transparent background (uses the terminal's)", BLUE)
            else:
                self.flash("solid background needs a 256-color terminal", YELLOW)
        elif ch == "C":
            # docker keeps its event history in memory; we just hide everything up to now
            self.feed.items.clear()
            self.cfg["events_cleared"] = int(time.time())
            save_config(self.cfg)
            self.flash("events cleared", BLUE)
        elif ch == "g":
            self.blocks = not self.blocks
            self.cfg["graph"] = "blocks" if self.blocks else "braille"
            save_config(self.cfg)
            self.flash("solid block graphs" if self.blocks else "braille graphs (higher resolution)", BLUE)
        elif k in (curses.KEY_RIGHT, 9):
            if self.view == 0:
                self.set_tab(self.tab + 1)
            else:
                self.set_view((self.view + 1) % 4)
        elif k in (curses.KEY_LEFT, curses.KEY_BTAB):
            if self.view == 0:
                self.set_tab(self.tab - 1)
            else:
                self.set_view((self.view - 1) % 4)
        elif k in (curses.KEY_DOWN, ord("j")):
            if self.zoom and self.view == 0 and self.tab:
                self.detail_scroll = max(0, self.detail_scroll + (-1 if self.tab == 1 else 1))
            else:
                self.move(1)
        elif k in (curses.KEY_UP, ord("k")):
            if self.zoom and self.view == 0 and self.tab:
                self.detail_scroll = max(0, self.detail_scroll + (1 if self.tab == 1 else -1))
            else:
                self.move(-1)
        elif k == curses.KEY_NPAGE:
            if self.view == 0 and self.tab:
                self.detail_scroll = max(0, self.detail_scroll - 10) if self.tab == 1 else self.detail_scroll + 10
            else:
                self.move(10)
        elif k == curses.KEY_PPAGE:
            if self.view == 0 and self.tab:
                self.detail_scroll = self.detail_scroll + 10 if self.tab == 1 else max(0, self.detail_scroll - 10)
            else:
                self.move(-10)
        elif k in (curses.KEY_END, ord("G")):
            self.detail_scroll = 0
        elif self.view == 0:
            if ch == "c":
                self.sort = "default" if self.sort == "cpu" else "cpu"
            elif ch == "m":
                self.sort = "default" if self.sort == "mem" else "mem"
            elif ch == "n":
                self.sort = "default" if self.sort == "name" else "name"
            elif ch == "a":
                self.show_all = not self.show_all
            elif ch == "l":
                self.set_tab(1)
            elif ch == "i":
                self.set_tab(2)
            elif ch == "t":
                self.set_tab(3)
            elif ch in ("s", "r", "p", "x", "e") and self.current:
                self.container_action(self.current, ch)
        elif self.view == 1 and ch == "p":
            self.open_pull()
        elif ch in ("x", "P", "r") and (self.current or ch == "P") and (ch != "r" or self.view == 1):
            self.resource_action(self.current, ch)
        return True

    def handle_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return True
        up = bstate & curses.BUTTON4_PRESSED
        down = bstate & getattr(curses, "BUTTON5_PRESSED", 0x200000)
        if up or down:
            d = -3 if up else 3
            if self.help:
                self.help_scroll = max(0, self.help_scroll + d)
                return True
            if self.form or self.pull:
                return True
            y0, y1 = self.regions.get("detail", (-1, -1))
            if y0 <= my < y1 and self.view == 0 and self.tab:
                self.detail_scroll = max(0, self.detail_scroll + (-d if self.tab == 1 else d))
            else:
                self.move(1 if down else -1)
            return True
        if bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            if self.help:
                self.help = False
                return True
            for y, x0, x1, fn in reversed(self.hits):
                if y == my and x0 <= mx < x1:
                    fn()
                    break
        return True

    # ------------------------------------------------------------ drawing
    def draw(self):
        self.frame += 1
        self.h, self.w = self.scr.getmaxyx()
        self.hits = []
        self.scr.bkgd(" ", self.pal(TEXT))
        self.scr.erase()
        h, w = self.h, self.w
        if h < 18 or w < 70:
            self.put(h // 2, max(0, (w - 34) // 2), "enlarge the terminal (min 70×18)", self.pal(YELLOW))
            self.scr.refresh()
            return
        rows, host, hh = self.snapshot()
        self.rows = rows
        if self.goto and any(r["id"] == self.goto for r in rows):
            self.sel_key[0], self.goto = self.goto, None
        self.current = None
        self.draw_header(rows)
        host_h = 0 if self.zoom else (8 if h >= 38 else 6 if h >= 26 else 0)
        if host_h and host:
            self.draw_host(1, host_h, host, hh)
        y0 = 1 + host_h
        y1 = h - 1
        [self.view_containers, self.view_images, self.view_volumes, self.view_networks][self.view](y0, y1, rows)
        self.draw_footer()
        if self.help:
            self.draw_help()
        if self.confirm:
            self.draw_confirm()
        if self.form:
            self.draw_run()
        if self.pull:
            self.draw_pull()
        self.scr.refresh()

    def draw_header(self, rows):
        w = self.w
        bar = self.pal(SUB, BAR_BG)
        self.put(0, 0, " " * w, bar)
        x = self.put(0, 0, " ◆ dockerax ", self.pal(BASE, MAUVE) | BOLD)
        info = self.col.info
        busy = time.time() - self.col.last_poll < 0.3
        x += self.put(0, x, f" {SPIN[self.frame % len(SPIN)] if busy else '•'} ", self.pal(MAUVE, BAR_BG))
        x += self.put(0, x, self.hostname, self.pal(TEXT, BAR_BG) | BOLD)
        if w >= 120:
            ver = info.get("ServerVersion", "")
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
            x += self.put(0, x, f"  docker {ver} · up {ago(up)}", bar)
        x += 3
        for i, name in enumerate(VIEWS):
            active = i == self.view
            bg = BLUE if active else BAR_BG
            end = self.segs(0, x, [(f" {i + 1}", BASE if active else BLUE, True), (f" {name} ", BASE if active else SUB, active)], bg=bg)
            self.hit(0, x, end, lambda i=i: self.set_view(i))
            x = end + 1
        on = sum(1 for r in rows if r["state"] == "running")
        off = len(rows) - on
        clock = time.strftime("%H:%M:%S")
        right = [(" ● ", GREEN), (f"{on} running ", TEXT), (" ○ ", DIM), (f"{off} stopped ", SUB), (f"  {clock} ", TEXT)]
        rl = sum(len(t) for t, _ in right)
        if x + rl < w:
            self.segs(0, w - rl, [(t, c) for t, c in right], bg=BAR_BG)

    def draw_host(self, y, hh_, host, hh):
        w = self.w
        cw = int(w * 0.56)
        load = host["load"]
        cores = host["cores"]
        self.box(y, 0, hh_, cw, f"cpu · {len(cores)} cores", right=f"load {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}")
        gh = hh_ - 2
        ix, iw = 2, cw - 4
        ccols = math.ceil(len(cores) / gh) if cores else 0
        core_w = 14
        cores_w = ccols * core_w if iw - ccols * core_w >= 24 else 0
        gw = iw - cores_w - (1 if cores_w else 0)
        self.graph(y + 1, ix, gw, gh, hh["cpu"], 100, G_LOAD)
        cpu = self.ease("host.cpu", host["cpu"])
        self.segs(y + 1, ix, [(" total ", SUB), (f"{cpu:5.1f}% ", grad(G_LOAD, cpu / 100), True)])
        if cores_w:
            for i, p in enumerate(cores):
                cx = ix + gw + 1 + (i // gh) * core_w
                cy = y + 1 + i % gh
                p = self.ease(f"core{i}", p)
                self.put(cy, cx, f"{i:>2}", self.pal(DIM))
                self.hbar(cy, cx + 3, 6, p, G_LOAD)
                self.put(cy, cx + 10, f"{p:3.0f}%", self.pal(SUB))

        rx0, rw = cw, w - cw
        self.box(y, rx0, hh_, rw, "memory · disk · network")
        ix, iw = rx0 + 2, rw - 4
        lines = []

        def meter(label, used, total, stops, key):
            def draw(yy):
                pct = self.ease(key, used / total * 100 if total else 0)
                val = f"{pct:5.1f}%  {human(used)} / {human(total)}"
                bw = max(4, iw - 7 - len(val))
                self.put(yy, ix, label, self.pal(SUB) | BOLD)
                self.hbar(yy, ix + 6, bw, pct, stops)
                self.put(yy, ix + 7 + bw, val, self.pal(TEXT), ix + iw)
            return draw

        def net(label, key, color, stops):
            def draw(yy):
                v = host[key]
                txt = f"{human(v, '/s'):>9}"
                self.segs(yy, ix, [(label, color, True), (txt, TEXT)])
                gx = ix + 6 + len(txt) + 1
                data = hh[key]
                self.graph(yy, gx, ix + iw - gx, 1, data, max(10240, max(data[-120:] or [0]) * 1.2), stops)
            return draw

        lines.append(meter("MEM", host["mem"], host["mem_total"], G_MEM, "h.mem"))
        if gh >= 6 and host["swap_total"]:
            lines.append(meter("SWAP", host["swap"], host["swap_total"], G_MEM, "h.swap"))
        lines.append(meter("DISK", host["disk"], host["disk_total"], G_SIZE, "h.disk"))
        lines.append(net("↓ in ", "rx", TEAL, G_RX))
        lines.append(net("↑ out", "tx", PEACH, G_TX))
        if gh >= 6:
            info = self.col.info

            def dinfo(yy):
                self.segs(yy, ix, [("docker ", DIM), (f"{info.get('Images', 0)} images · {info.get('Containers', 0)} containers · "
                                                       f"driver {info.get('Driver', '?')} · ip {self.ip}", SUB)], ix + iw)
            lines.append(dinfo)
        for i, fn in enumerate(lines[:gh]):
            fn(y + 1 + i)

    def table(self, y, x, h, w, title, cols, items, cell, right=None, keyf=lambda it: it.get("id")):
        """Table with header, selection, scrolling and mouse. Items with '_group' are headings."""
        v = self.view
        self.box(y, x, h, w, title, right=right)
        ix, iw = x + 2, w - 4
        gaps = len(cols) - 1
        fixed = sum(c[2] for c in cols if c[2])
        nflex = sum(1 for c in cols if not c[2]) or 1
        fw = max(6, (iw - fixed - gaps) // nflex)
        widths = [c[2] or fw for c in cols]
        hx = ix
        for (key, hdr, _), cw in zip(cols, widths):
            self.put(y + 1, hx, hdr[:cw], self.pal(DIM) | BOLD, x + w - 1)
            hx += cw + 1
        body_y, body_h = y + 2, h - 3
        selectable = [i for i, it in enumerate(items) if "_group" not in it]
        if not selectable or body_h <= 0:
            return None
        if self.sel_key[v] is not None:
            for si, i in enumerate(selectable):
                if keyf(items[i]) == self.sel_key[v]:
                    self.sel[v] = si
                    break
        si = max(0, min(self.sel[v], len(selectable) - 1))
        self.sel[v] = si
        pos = selectable[si]
        self.sel_key[v] = keyf(items[pos])
        top = self.scroll[v]
        if pos < top:
            top = pos - 1 if pos > 0 and "_group" in items[pos - 1] else pos
        if pos >= top + body_h:
            top = pos - body_h + 1
        top = max(0, min(top, len(items) - body_h)) if len(items) > body_h else 0
        self.scroll[v] = top
        sel_index = {i: n for n, i in enumerate(selectable)}
        for n, it in enumerate(items[top:top + body_h]):
            yy, idx = body_y + n, top + n
            if "_group" in it:
                self.draw_group(yy, ix, iw, it)
                continue
            selected = idx == pos
            bg = SEL_BG if selected else None
            if selected:
                self.put(yy, x + 1, " " * (w - 2), self.pal(TEXT, SEL_BG))
                self.put(yy, x + 1, "▌", self.pal(BLUE, SEL_BG))
            self.hit(yy, x + 1, x + w - 1, lambda n=sel_index[idx]: (self.__setattr__("detail_scroll", 0),
                                                                      self.sel.__setitem__(v, n),
                                                                      self.sel_key.__setitem__(v, None)))
            cx = ix
            for (key, _, _), cw in zip(cols, widths):
                cell(it, key, yy, cx, cw, bg)
                cx += cw + 1
        if len(items) > body_h:
            th = max(1, body_h * body_h // len(items))
            ty = body_y + (body_h - th) * top // max(1, len(items) - body_h)
            for i in range(th):
                self.put(ty + i, x + w - 1, "┃", self.pal(SUB))
        return items[pos]

    def draw_group(self, y, x, w, it):
        x0 = x
        x += self.put(y, x, "▾ ", self.pal(MAUVE))
        x += self.put(y, x, it["_group"], self.pal(MAUVE) | BOLD)
        kind = "compose" if it["compose"] else "no compose"
        color = GREEN if it["on"] == it["total"] else YELLOW if it["on"] else DIM
        x += self.segs(y, x + 2, [(f"{kind} · ", DIM), (f"{it['on']}/{it['total']} running", color)]) - x
        if x0 + w - x > 3:
            self.put(y, x + 1, "┄" * (x0 + w - x - 1), self.pal(FAINT))

    def state_icon(self, r):
        st, f = r["state"], self.frame
        if r["id"] in self.pending or st == "restarting":
            return SPIN[f % len(SPIN)], YELLOW
        if st == "running":
            if r["health"] == "unhealthy":
                return "●", RED if (f // 4) % 2 else PEACH
            if r["health"] == "starting":
                return "◐◓◑◒"[(f // 2) % 4], YELLOW
            return "●", GREEN
        if st == "paused":
            return "‖", BLUE
        if st == "created":
            return "◇", DIM
        if r["exit"] not in (None, 0):
            return "✕", RED
        return "○", DIM

    # ------------------------------------------------------------ containers
    def view_containers(self, y0, y1, rows):
        w = self.w
        items = self.container_items(rows)
        area = y1 - y0
        if self.zoom:
            tbl_h = 0
        else:
            # the table grows with the number of containers; the rest goes to the bottom panel
            tbl_h = max(7, min(len(items) + 4, area - 12, int(area * 0.6)))
        det_h = area - tbl_h
        sel = None
        if tbl_h >= 4:
            iw = w - 4
            cols = [("dot", "", 1), ("name", "NAME", 0)]
            if iw >= 140:
                cols.append(("image", "IMAGE", 22))
            cols += [("cpu", "CPU", 16), ("mem", "MEMORY", 16)]
            if iw >= 100:
                cols.append(("net", "NET ↓ ↑", 15))
            if iw >= 88:
                cols.append(("status", "STATUS", 19))
            cols.append(("ports", "PORTS host→container", 0))
            title = "containers"
            if self.sort != "default":
                title += {"cpu": " · ↓cpu", "mem": " · ↓memory", "name": " · ↓name"}[self.sort]
            if not self.show_all:
                title += " · running only"
            if self.filter:
                title += f" · filter “{self.filter}”"
            n_on = sum(1 for r in rows if r["state"] == "running")
            sel = self.table(y0, 0, tbl_h, w, title, cols, items, self.container_cell,
                             right=f"{n_on}/{len(rows)} running")
            if not [it for it in items if "_group" not in it]:
                msg = ("no containers" if self.show_all or self.filter else "no running containers (a shows all)") + (" match this filter" if self.filter else "")
                self.put(y0 + 3, max(2, (w - len(msg)) // 2), msg, self.pal(DIM))
                self.put(y0 + 4, max(2, (w - 44) // 2), "try:  docker run -d -p 8080:80 nginx       ", self.pal(FAINT))
        else:
            sel_rows = [it for it in items if "_group" not in it]
            for r in sel_rows:
                if r["id"] == self.sel_key[0]:
                    sel = r
            if sel is None and sel_rows:
                sel = sel_rows[min(self.sel[0], len(sel_rows) - 1)]
        self.current = sel
        ev_w = min(52, w // 3) if w >= 120 and not self.zoom else 0
        dy = y0 + tbl_h
        self.regions["detail"] = (dy, y1)
        self.draw_detail(dy, 0, det_h, w - ev_w, sel)
        if ev_w:
            self.draw_events(dy, w - ev_w, det_h, ev_w)

    def container_cell(self, r, key, y, x, w, bg):
        running = r["state"] == "running"
        if key == "dot":
            icon, color = self.state_icon(r)
            self.put(y, x, icon, self.pal(color, bg) | BOLD)
        elif key == "name":
            tree = r.get("_tree", "")
            x += self.put(y, x, tree, self.pal(FAINT, bg))
            self.put(y, x, r["name"], self.pal(TEXT if running else SUB, bg) | BOLD, x + w - len(tree))
        elif key == "image":
            self.put(y, x, r["image"], self.pal(DIM, bg), x + w)
        elif key == "cpu":
            if running:
                hist = self.hist_of(r["id"])["cpu"]
                self.graph(y, x, 8, 1, hist, max(100.0, max(hist or [0])), G_LOAD, bg)
                cpu = self.ease("c.cpu." + r["id"], r["cpu"])
                self.put(y, x + 9, f"{cpu:5.1f}%", self.pal(grad(G_LOAD, cpu / 100), bg))
            else:
                self.put(y, x, "·", self.pal(FAINT, bg))
        elif key == "mem":
            if running:
                pct = self.ease("c.mem." + r["id"], r["mem"] / r["mem_limit"] * 100 if r["mem_limit"] else 0)
                self.hbar(y, x, 7, max(pct, 2 if r["mem"] else 0), G_MEM, bg)
                self.put(y, x + 8, f"{human(r['mem']):>7}", self.pal(TEXT, bg))
            else:
                self.put(y, x, "·", self.pal(FAINT, bg))
        elif key == "net":
            if running:
                self.segs(y, x, [("↓", TEAL), (f"{human(r['rx_rate']):>6} ", TEXT), ("↑", PEACH),
                                 (f"{human(r['tx_rate']):>6}", TEXT)], x + w, bg)
            else:
                self.put(y, x, "·", self.pal(FAINT, bg))
        elif key == "status":
            color = GREEN if running else RED if r["exit"] not in (None, 0) else DIM
            if r["health"] == "unhealthy":
                color = RED
            self.put(y, x, status_text(r["status"]), self.pal(color, bg), x + w)
        elif key == "ports":
            self.put(y, x, r["ports"] or "—", self.pal(PEACH if r["ports"] else FAINT, bg), x + w)

    def draw_detail(self, y, x, h, w, r):
        self.box(y, x, h, w, right="z enlarge" if not self.zoom else "z / esc back")
        if not r:
            self.put(y + h // 2, x + max(2, (w - 30) // 2), "no container selected      ", self.pal(DIM))
            self.stop_logs()
            return
        icon, color = self.state_icon(r)
        tx = x + 2
        tx += self.put(y, tx, f" {icon} ", self.pal(color) | BOLD)
        tx += self.put(y, tx, r["name"] + " ", self.pal(TEXT) | BOLD)
        tx += 1
        for i, name in enumerate(TABS):
            lbl = f" {name} "
            attr = self.pal(BASE, BLUE) | BOLD if i == self.tab else self.pal(SUB)
            self.put(y, tx, lbl, attr, x + w - 1)
            self.hit(y, tx, tx + len(lbl), lambda i=i: self.set_tab(i))
            tx += len(lbl) + 1
        iy, ix, ih, iw = y + 1, x + 2, h - 2, w - 4
        if self.tab != 1:
            self.stop_logs()
        [self.tab_metrics, self.tab_logs, self.tab_info, self.tab_top][self.tab](r, iy, ix, ih, iw)

    def tab_metrics(self, r, y, x, h, w):
        if r["state"] != "running":
            self.segs(y + 1, x, [("container ", DIM), (status_text(r["status"]), RED if r["exit"] else SUB, True)])
            if r["exit"] not in (None, 0):
                self.segs(y + 2, x, [("exit code ", DIM), (f"{r['exit']} — {EXIT_INFO.get(r['exit'], 'error')}", RED)])
            self.segs(y + 4, x, [("s", BLUE, True), (" starts it  ·  ", DIM), ("l", BLUE, True), (" shows the last logs to find out why it stopped", DIM)])
            return
        hist = self.hist_of(r["id"])
        ncols = 3 if w >= 90 else 2 if w >= 56 else 1
        gw = (w - (ncols - 1) * 3) // ncols
        gh = max(1, h - 2)
        ncpu = os.cpu_count() or 1
        panels = []
        cpu = self.ease("d.cpu", r["cpu"])
        panels.append(([("CPU ", SUB, True), (f"{cpu:.1f}%", grad(G_LOAD, cpu / 100), True),
                        (f"  ≈ {cpu / 100:.2f} of {ncpu} cores", DIM)],
                       lambda gx, gy: self.graph(gy, gx, gw, gh, hist["cpu"], max(100.0, max(hist["cpu"] or [0]) * 1.2), G_LOAD)))
        lim = r["mem_limit"]
        # scale with a 64M floor and a round value: steady usage becomes a low band, not a tower
        mscale = nice_ceil(max(64 * 1024 ** 2, max(hist["mem"] or [0]) * 1.25) / 1024 ** 2) * 1024 ** 2
        panels.append(([("MEMORY ", SUB, True), (human(r["mem"]), MAUVE, True), (f" of {human(lim)}" if lim else "", DIM),
                        (f"  scale 0–{human(mscale)}", FAINT)],
                       lambda gx, gy: self.graph(gy, gx, gw, gh, hist["mem"], mscale, G_MEM)))

        def netg(gx, gy):
            top = (gh + 1) // 2
            self.graph(gy, gx, gw, top, hist["rx"], max(10240, max(hist["rx"] or [0]) * 1.2), G_RX)
            if gh - top:
                self.graph(gy + top, gx, gw, gh - top, hist["tx"], max(10240, max(hist["tx"] or [0]) * 1.2), G_TX, invert=True)
        panels.append(([("NET ", SUB, True), ("↓", TEAL), (human(r["rx_rate"], "/s"), TEXT, True),
                        ("  ↑", PEACH), (human(r["tx_rate"], "/s"), TEXT, True)], netg))
        for i, (label, draw) in enumerate(panels[:ncols]):
            gx = x + i * (gw + 3)
            self.segs(y, gx, label, gx + gw)
            draw(gx, y + 1)
            if i:
                for yy in range(y, y + h - 1):
                    self.put(yy, gx - 2, "│", self.pal(FAINT))
        self.segs(y + h - 1, x, [
            ("processes ", DIM), (str(r["pids"]), TEXT), ("  ·  disk read ", DIM), (human(r["blk_r"]), TEXT),
            (" written ", DIM), (human(r["blk_w"]), TEXT), ("  ·  net total ↓", DIM), (human(r["rx"]), TEXT),
            (" ↑", DIM), (human(r["tx"]), TEXT), ("  ·  ", DIM), (status_text(r["status"]), GREEN)], x + w)

    def stop_logs(self):
        if self.logs:
            self.logs.stop()
            self.logs = None

    def tab_logs(self, r, y, x, h, w):
        ins = self.cache.get(("inspect", r["id"]), lambda: api("GET", f"/containers/{r['id']}/json"), 30)
        lg = self.logs
        if lg and (lg.cid != r["id"] or (lg.done_at and time.time() - lg.done_at > 3 and r["state"] == "running")):
            self.stop_logs()
            lg = None
        if lg is None and isinstance(ins, dict):
            lg = self.logs = LogTail(r["id"], bool((ins.get("Config") or {}).get("Tty")))
            lg.start()
        following = self.detail_scroll == 0
        status = [("● following", GREEN)] if following and lg and not lg.done_at else \
                 [("■ end of stream", DIM)] if lg and lg.done_at else [("‖ paused", YELLOW), ("  End jumps back", DIM)]
        self.segs(y, x, [("$ ", MAUVE), (f"docker logs -f --tail 300 {r['name']}", DIM)], x + w - 22)
        self.segs(y, x + w - 20, status, x + w)
        if not lg:
            self.put(y + 2, x, "loading…", self.pal(DIM))
            return
        if lg.error:
            self.put(y + 2, x, f"error: {lg.error}", self.pal(RED), x + w)
        lines = list(lg.lines)
        body = h - 1
        max_scroll = max(0, len(lines) - body)
        self.detail_scroll = min(self.detail_scroll, max_scroll)
        end = len(lines) - self.detail_scroll
        view = lines[max(0, end - body):end]
        if not lines and not lg.error:
            self.put(y + 2, x, "(no log output yet)", self.pal(FAINT))
        for i, (stream, ts, msg) in enumerate(view):
            yy = y + 1 + i
            cx = x
            cx += self.put(yy, cx, "▎" if stream == 2 else " ", self.pal(RED if stream == 2 else FAINT))
            if ts and w > 60:
                cx += self.put(yy, cx, time.strftime("%H:%M:%S ", time.localtime(ts)), self.pal(FAINT))
            self.log_line(yy, cx, msg, x + w)
        if self.detail_scroll and len(lines) > body:
            self.put(y + h - 1, x + w - 18, f" ↑ {self.detail_scroll} lines ", self.pal(BASE, YELLOW))

    LEVELS = [(re.compile(r"\b(ERROR|ERR|FATAL|CRIT(ICAL)?|PANIC|EMERG|error|fatal|panic|Exception|Traceback)\b"), RED),
              (re.compile(r"\b(WARN(ING)?|warn(ing)?)\b"), YELLOW),
              (re.compile(r"\b(DEBUG|TRACE|debug|trace)\b"), DIM)]
    HTTP_CODE = re.compile(r'(?<=" )([1-5]\d\d)(?= )')

    def log_line(self, y, x, msg, maxx):
        color = TEXT
        for rx, c in self.LEVELS:
            if rx.search(msg):
                color = c
                break
        m = self.HTTP_CODE.search(msg)
        if m:
            code = int(m.group(1))
            cc = RED if code >= 500 else YELLOW if code >= 400 else GREEN if code < 300 else BLUE
            self.segs(y, x, [(msg[:m.start()], color), (m.group(1), cc, True), (msg[m.end():], color)], maxx)
        else:
            self.put(y, x, msg, self.pal(color), maxx)

    def tab_info(self, r, y, x, h, w):
        ins = self.cache.get(("inspect", r["id"]), lambda: api("GET", f"/containers/{r['id']}/json"), 10)
        if ins is None:
            self.put(y, x, "loading…", self.pal(DIM))
            return
        if isinstance(ins, Exception):
            self.put(y, x, f"error: {ins}", self.pal(RED), x + w)
            return
        st, cfg, hc = ins.get("State") or {}, ins.get("Config") or {}, ins.get("HostConfig") or {}
        L = []

        def kv(k, *v):
            L.append([(f"{k:<14}", DIM)] + list(v))

        def section(t):
            L.append([])
            L.append([(t, MAUVE, True)])

        kv("id", (ins.get("Id", "")[:12], TEXT), ("   image ", DIM), (cfg.get("Image", ""), BLUE))
        created = parse_ts(ins.get("Created"))
        started = parse_ts(st.get("StartedAt"))
        kv("created", (f"{ago(time.time() - created)} ago" if created else "?", TEXT),
           ("   started ", DIM), (f"{ago(time.time() - started)} ago" if started else "never", TEXT))
        rp = (hc.get("RestartPolicy") or {}).get("Name") or "no"
        rp_txt = {"no": "never restarts on its own", "always": "always restarts (including at boot)",
                  "unless-stopped": "restarts unless you stopped it", "on-failure": "restarts only if it fails"}.get(rp, rp)
        kv("restart", (rp, TEXT, True), (f"  {rp_txt}", DIM), ("   restarted ", DIM), (f"{ins.get('RestartCount', 0)}×", TEXT))
        cmd = " ".join([ins.get("Path", "")] + (ins.get("Args") or []))
        kv("command", (cmd, TEXT))
        health = st.get("Health")
        if health:
            hs = health.get("Status")
            kv("health", (hs, GREEN if hs == "healthy" else RED if hs == "unhealthy" else YELLOW, True),
               (f"  failing streak {health.get('FailingStreak', 0)}", DIM))
            logs = health.get("Log") or []
            if logs:
                kv("", (clean(logs[-1].get("Output", "")).strip()[:200], DIM))
        if st.get("OOMKilled"):
            kv("", ("⚠ was killed for running out of memory (OOM)", RED, True))

        section("ports  (host → container)")
        binds = hc.get("PortBindings") or {}
        exposed = (cfg.get("ExposedPorts") or {}).keys()
        if not binds and not exposed:
            L.append([("  no ports exposed", FAINT)])
        seen = set()
        for cport, bl in sorted(binds.items()):
            cp, _, proto = cport.partition("/")
            for b in bl or []:
                hp = b.get("HostPort") or "?"
                if (hp, cport) in seen:
                    continue
                seen.add((hp, cport))
                line = [(f"  {hp}", PEACH, True), (" → ", DIM), (f"{cp}/{proto}", PEACH),
                        (f"   port {hp} on this server leads to port {cp} in the container", DIM)]
                if proto == "tcp":
                    line += [("   open ", DIM), (f"http://{self.ip}:{hp}", BLUE, True)]
                L.append(line)
        for p in sorted(exposed):
            if p not in binds:
                L.append([(f"  {p}", SUB), ("   exposed but not published: only containers on the same network can reach it", DIM)])

        section("networks")
        for name, n in ((ins.get("NetworkSettings") or {}).get("Networks") or {}).items():
            L.append([(f"  {name:<20}", BLUE, True), ("ip ", DIM), (n.get("IPAddress") or "—", TEXT),
                      ("   gateway ", DIM), (n.get("Gateway") or "—", TEXT),
                      ("   aliases ", DIM), (", ".join((n.get("Aliases") or [])[:3]) or "—", SUB)])

        section("storage  (source → inside the container)")
        mounts = ins.get("Mounts") or []
        if not mounts:
            L.append([("  nothing mounted: everything the container writes is lost on docker rm", FAINT)])
        for m in mounts:
            src = m.get("Name") if m.get("Type") == "volume" else m.get("Source", "")
            if re.fullmatch(r"[0-9a-f]{64}", src or ""):
                src = src[:12] + "… (anonymous)"
            L.append([(f"  {m.get('Type', ''):<7}", MAUVE), (src or "", TEXT), (" → ", DIM), (m.get("Destination", ""), TEAL),
                      ("  read-write" if m.get("RW", True) else "  read-only", DIM)])

        section("environment variables")
        for e in cfg.get("Env") or []:
            k, _, val = e.partition("=")
            if re.search(r"PASS|SECRET|TOKEN|KEY|PWD|CREDENTIAL", k, re.I):
                val = "•••••••• (hidden)"
            L.append([(f"  {k}", SUB), ("=", FAINT), (val, TEXT)])

        section("how to recreate this container (approximate)")
        for i, part in enumerate(wrap(docker_run_cmd(ins), w - 4)):
            L.append([("  $ " if i == 0 else "    ", MAUVE), (part, TEXT)])

        self.detail_scroll = min(self.detail_scroll, max(0, len(L) - h))
        for i, line in enumerate(L[self.detail_scroll:self.detail_scroll + h]):
            self.segs(y + i, x, line, x + w)
        if len(L) > h:
            self.put(y + h - 1, x + w - 22, f" PgDn/wheel: {len(L) - h - self.detail_scroll} more ", self.pal(BASE, SUB))

    def tab_top(self, r, y, x, h, w):
        self.segs(y, x, [("$ ", MAUVE), (f"docker top {r['name']}", DIM),
                         ("   processes running inside the container, as seen from the host", FAINT)], x + w)
        if r["state"] != "running":
            self.put(y + 2, x, "container stopped: no processes", self.pal(DIM))
            return
        args = quote("-eo pid,user,pcpu,rss,etime,args")
        top = self.cache.get(("top", r["id"]), lambda: api("GET", f"/containers/{r['id']}/top?ps_args={args}"), 2.5)
        if top is None:
            self.put(y + 2, x, "loading…", self.pal(DIM))
            return
        if isinstance(top, Exception):
            self.put(y + 2, x, f"error: {top}", self.pal(RED), x + w)
            return
        widths = [8, 10, 7, 9, 12, 0]
        hdrs = ["PID", "USER", "%CPU", "MEMORY", "TIME", "COMMAND"]
        cx = x
        for hd, cw in zip(hdrs, widths):
            self.put(y + 1, cx, hd, self.pal(DIM) | BOLD)
            cx += cw
        procs = sorted(top.get("Processes") or [], key=lambda p: -float(p[2] or 0))
        self.detail_scroll = min(self.detail_scroll, max(0, len(procs) - (h - 2)))
        for i, p in enumerate(procs[self.detail_scroll:self.detail_scroll + h - 2]):
            yy = y + 2 + i
            pcpu = float(p[2] or 0)
            vals = [(p[0], TEXT), (p[1], SUB), (f"{pcpu:.1f}", grad(G_LOAD, pcpu / 100)),
                    (human(int(p[3] or 0) * 1024), MAUVE), (p[4], DIM), (" ".join(p[5:]), TEXT)]
            cx = x
            for (v, c), cw in zip(vals, widths):
                self.put(yy, cx, v, self.pal(c), cx + cw - 1 if cw else x + w)
                cx += cw

    # ------------------------------------------------------------ events
    def draw_events(self, y, x, h, w):
        live = self.feed.connected
        pulse = grad([FAINT, GREEN], (math.sin(self.frame / 3) + 1) / 2) if live else RED
        self.box(y, x, h, w, "events", right="C clear · ● live" if live else "disconnected", rcolor=pulse)
        items = list(self.feed.items)[:h - 2]
        if not items:
            self.put(y + 2, x + 2, "no events yet", self.pal(DIM), x + w - 1)
            self.put(y + 3, x + 2, "start or stop a container", self.pal(FAINT), x + w - 1)
            self.put(y + 4, x + 2, "and watch it show up here", self.pal(FAINT), x + w - 1)
        now = time.time()
        for i, (t, icon, color, name, desc, typ, aid) in enumerate(items):
            yy = y + 1 + i
            fresh = now - t < 4
            bg = SEL_BG if fresh and (self.frame // 3) % 2 == 0 else None
            if bg:
                self.put(yy, x + 1, " " * (w - 2), self.pal(TEXT, bg))
            same_day = time.strftime("%d", time.localtime(t)) == time.strftime("%d")
            stamp = time.strftime("%H:%M:%S" if same_day else "%d/%m %H:%M", time.localtime(t))
            self.segs(yy, x + 2, [(stamp + " ", FAINT), (icon + " ", color, True), (name + " ", TEXT, fresh),
                                  (desc, color if fresh else SUB)], x + w - 1, bg)

    # ------------------------------------------------------------ other views
    def side_split(self, y0, y1, n=0):
        """table on top, details below (with events on the right if it fits)."""
        area = y1 - y0
        det_h = area if self.zoom else area - max(7, min(n + 4, area - 10, int(area * 0.6)))
        ev_w = min(52, self.w // 3) if self.w >= 120 and not self.zoom else 0
        return area - det_h, det_h, ev_w

    def view_images(self, y0, y1, rows):
        data = self.cache.get(("images",), lambda: api("GET", "/images/json"), 15)
        used = {}
        for r in rows:
            used[r["image_id"]] = used.get(r["image_id"], 0) + 1
        items = []
        for img in data if isinstance(data, list) else []:
            tags = img.get("RepoTags") or []
            if not tags:
                digests = img.get("RepoDigests") or []
                tags = [digests[0].split("@")[0] + ":<none>"] if digests else ["<none>:<none>"]
            items.append({"id": img["Id"], "short": img["Id"].split(":")[-1][:12], "tag": tags[0], "more": len(tags) - 1,
                          "size": img.get("Size", 0), "created": img.get("Created", 0), "used": used.get(img["Id"], 0),
                          "dangling": tags[0] == "<none>:<none>"})
        if self.filter:
            items = [i for i in items if self.filter.lower() in i["tag"].lower()]
        items.sort(key=lambda i: (not i["used"], -i["size"]))
        self.max_size = max([i["size"] for i in items] or [1])
        if self.goto_img:
            ref, run = self.goto_img
            want = ref if not ref.startswith("docker.io/") else ref.split("/", 1)[1]
            want = want[len("library/"):] if want.startswith("library/") else want
            hit = next((i for i in items if i["tag"] == want), None)
            if hit:
                self.goto_img, self.filter = None, ""
                self.sel_key[1] = hit["id"]
                if run:
                    self.open_run(hit)
        tbl_h, det_h, ev_w = self.side_split(y0, y1, len(items))
        total = sum(i["size"] for i in items)
        cols = [("dot", "", 1), ("tag", "REPOSITORY:TAG", 0), ("id", "ID", 12), ("size", "SIZE", 22),
                ("created", "CREATED", 13), ("used", "IN USE", 14)]
        sel = None
        if tbl_h >= 4:
            sel = self.table(y0, 0, tbl_h, self.w, "images", cols, items, self.image_cell,
                             right=f"{len(items)} image{'s' if len(items) != 1 else ''} · {human(total)}  ·  p pull · r run · x delete · P prune")
            self.loading(y0, data, items)
        elif items:
            sel = items[min(self.sel[1], len(items) - 1)]
        self.current = sel
        dy = y0 + tbl_h
        self.regions["detail"] = (dy, y1)
        self.draw_image_detail(dy, 0, det_h, self.w - ev_w, sel)
        if ev_w:
            self.draw_events(dy, self.w - ev_w, det_h, ev_w)

    def loading(self, y0, data, items):
        if data is None:
            self.put(y0 + 3, 4, "loading…", self.pal(DIM))
        elif isinstance(data, Exception):
            self.put(y0 + 3, 4, f"error: {data}", self.pal(RED))
        elif not items:
            self.put(y0 + 3, 4, "nothing here" + (" for this filter" if self.filter else ""), self.pal(DIM))

    def image_cell(self, it, key, y, x, w, bg):
        if key == "dot":
            icon, c = ("●", GREEN) if it["used"] else ("✕", RED) if it["dangling"] else ("○", DIM)
            self.put(y, x, icon, self.pal(c, bg) | BOLD)
        elif key == "tag":
            repo, _, tag = it["tag"].rpartition(":")
            self.segs(y, x, [(repo, TEXT, True), (":" + tag, BLUE), (f" +{it['more']} tags" if it["more"] else "", DIM)], x + w, bg)
        elif key == "id":
            self.put(y, x, it["short"], self.pal(DIM, bg))
        elif key == "size":
            self.hbar(y, x, 12, it["size"] / self.max_size * 100, G_SIZE, bg)
            self.put(y, x + 13, f"{human(it['size']):>8}", self.pal(TEXT, bg))
        elif key == "created":
            self.put(y, x, ago(time.time() - it["created"]) + " ago", self.pal(SUB, bg))
        elif key == "used":
            txt = f"{it['used']} container" + ("s" if it["used"] > 1 else "") if it["used"] else ("dangling" if it["dangling"] else "unused")
            self.put(y, x, txt, self.pal(GREEN if it["used"] else RED if it["dangling"] else DIM, bg))

    def draw_image_detail(self, y, x, h, w, it):
        self.box(y, x, h, w, f"layers · {it['tag']}" if it else "layers", right="z enlarge")
        if not it:
            return
        iy, ix, ih, iw = y + 1, x + 2, h - 2, w - 4
        self.segs(iy, ix, [("$ ", MAUVE), (f"docker history {it['tag'] if not it['dangling'] else it['short']}", DIM),
                           ("   each line is a Dockerfile instruction; 0B ones only change config", FAINT)], ix + iw)
        hist = self.cache.get(("history", it["id"]), lambda: api("GET", f"/images/{quote(it['id'], safe='')}/history"), 300)
        if not isinstance(hist, list):
            self.put(iy + 1, ix, "loading…" if hist is None else f"error: {hist}", self.pal(DIM))
            return
        layers = list(reversed(hist))
        big = max([lyr.get("Size", 0) for lyr in layers] or [1]) or 1
        self.detail_scroll = min(self.detail_scroll, max(0, len(layers) - (ih - 1)))
        for i, lyr in enumerate(layers[self.detail_scroll:self.detail_scroll + ih - 1]):
            yy = iy + 1 + i
            size = lyr.get("Size", 0)
            by = clean(lyr.get("CreatedBy", ""))
            by = re.sub(r"^/bin/sh -c (#\(nop\)\s*)?", "", by)
            by = re.sub(r"\s*# buildkit$", "", by).strip()
            m = re.match(r"^([A-Z]+)\s", by)
            instr = m.group(1) if m else ("RUN" if size else "")
            rest = by[len(instr):].strip() if m else by
            self.put(yy, ix, f"{self.detail_scroll + i + 1:>2}", self.pal(FAINT))
            self.hbar(yy, ix + 3, 8, size / big * 100 if size else 0, G_SIZE)
            self.put(yy, ix + 12, f"{human(size):>7}", self.pal(TEXT if size else FAINT))
            self.segs(yy, ix + 21, [(f"{instr:<11}", MAUVE, True), (rest, SUB)], ix + iw)

    def view_volumes(self, y0, y1, rows):
        def fetch():
            try:
                return api("GET", "/system/df?type=volume").get("Volumes") or []
            except RuntimeError:
                return api("GET", "/volumes").get("Volumes") or []
        data = self.cache.get(("volumes",), fetch, 15)
        users = {}
        for r in rows:
            for m in r["mounts"]:
                if m.get("Type") == "volume":
                    users.setdefault(m.get("Name"), []).append((r["name"], m.get("Destination", ""), r["state"]))
        items = []
        for v in data if isinstance(data, list) else []:
            items.append({"id": v["Name"], "name": v["Name"], "driver": v.get("Driver", ""),
                          "size": (v.get("UsageData") or {}).get("Size", -1), "users": users.get(v["Name"], []),
                          "mount": v.get("Mountpoint", ""), "created": parse_ts(v.get("CreatedAt")),
                          "anon": bool(re.fullmatch(r"[0-9a-f]{64}", v["Name"])),
                          "project": (v.get("Labels") or {}).get("com.docker.compose.project", "")})
        if self.filter:
            items = [i for i in items if self.filter.lower() in i["name"].lower()]
        items.sort(key=lambda i: (not i["users"], i["anon"], i["name"]))
        tbl_h, det_h, ev_w = self.side_split(y0, y1, len(items))
        cols = [("dot", "", 1), ("name", "NAME", 0), ("driver", "DRIVER", 8), ("size", "SIZE", 9), ("users", "USED BY", 0)]
        sel = None
        if tbl_h >= 4:
            sel = self.table(y0, 0, tbl_h, self.w, "volumes", cols, items, self.volume_cell,
                             right=f"{len(items)} volume{'s' if len(items) != 1 else ''}  ·  x delete")
            self.loading(y0, data, items)
        elif items:
            sel = items[min(self.sel[2], len(items) - 1)]
        self.current = sel
        dy = y0 + tbl_h
        self.regions["detail"] = (dy, y1)
        L = []
        if sel:
            L.append([("what it is: ", MAUVE, True), ("a folder managed by docker that keeps data outside the container —", SUB)])
            L.append([("            ", DIM), ("if the container is deleted, what is in here keeps existing.", SUB)])
            L.append([])
            L.append([("name          ", DIM), (sel["name"], TEXT, True), ("   (anonymous: created without a name by a Dockerfile VOLUME)" if sel["anon"] else "", FAINT)])
            L.append([("on the host   ", DIM), (sel["mount"], TEAL)])
            if sel["created"]:
                L.append([("created       ", DIM), (ago(time.time() - sel["created"]) + " ago", TEXT)])
            if sel["project"]:
                L.append([("compose       ", DIM), (sel["project"], MAUVE)])
            L.append([])
            L.append([("mounted in", MAUVE, True)])
            if not sel["users"]:
                L.append([("  no container uses this volume — delete it with x if you don't need the data", YELLOW)])
            for name, dest, st in sel["users"]:
                L.append([("  ● " if st == "running" else "  ○ ", GREEN if st == "running" else DIM), (name, TEXT, True),
                          ("  sees it as ", DIM), (dest, TEAL)])
        self.lines_panel(dy, 0, det_h, self.w - ev_w, "volume details", L, f"$ docker volume inspect {sel['name'][:30]}" if sel else "")
        if ev_w:
            self.draw_events(dy, self.w - ev_w, det_h, ev_w)

    def volume_cell(self, it, key, y, x, w, bg):
        if key == "dot":
            self.put(y, x, "●" if it["users"] else "○", self.pal(GREEN if it["users"] else DIM, bg) | BOLD)
        elif key == "name":
            n = it["name"][:12] + "… anonymous" if it["anon"] else it["name"]
            self.put(y, x, n, self.pal(SUB if it["anon"] else TEXT, bg) | BOLD, x + w)
        elif key == "driver":
            self.put(y, x, it["driver"], self.pal(DIM, bg))
        elif key == "size":
            self.put(y, x, f"{human(it['size']):>8}" if it["size"] >= 0 else "       ?", self.pal(TEXT, bg))
        elif key == "users":
            txt = ", ".join(u[0] for u in it["users"]) or "nobody"
            self.put(y, x, txt, self.pal(TEXT if it["users"] else FAINT, bg), x + w)

    DRIVER_INFO = {
        "bridge": "a private virtual network inside this server. Containers on it can talk to each other; the outside world only reaches them through published ports (-p).",
        "host": "the container uses the server's network directly, with no isolation and no need for -p.",
        "null": "no network at all: the container is isolated.",
        "overlay": "a network spanning several servers (swarm).",
        "macvlan": "the container gets its own address on the physical network, as if it were another machine.",
    }

    def view_networks(self, y0, y1, rows):
        data = self.cache.get(("networks",), lambda: api("GET", "/networks"), 15)
        attached = {}
        for r in rows:
            for net, ip in r["networks"].items():
                attached.setdefault(net, []).append((r, ip))
        items = []
        for n in data if isinstance(data, list) else []:
            cfgs = (n.get("IPAM") or {}).get("Config") or []
            subnet = ", ".join(c.get("Subnet", "") for c in cfgs if c.get("Subnet"))
            gw = ", ".join(c.get("Gateway", "") for c in cfgs if c.get("Gateway"))
            items.append({"id": n["Id"], "name": n["Name"], "driver": n.get("Driver", ""), "subnet": subnet, "gw": gw,
                          "members": attached.get(n["Name"], []), "internal": n.get("Internal", False),
                          "project": (n.get("Labels") or {}).get("com.docker.compose.project", "")})
        if self.filter:
            items = [i for i in items if self.filter.lower() in i["name"].lower()]
        items.sort(key=lambda i: (not i["members"], i["name"] in ("bridge", "host", "none"), i["name"]))
        tbl_h, det_h, ev_w = self.side_split(y0, y1, len(items))
        cols = [("dot", "", 1), ("name", "NAME", 0), ("driver", "DRIVER", 8), ("subnet", "SUBNET", 18),
                ("gw", "GATEWAY", 15), ("members", "CONTAINERS", 11)]
        sel = None
        if tbl_h >= 4:
            sel = self.table(y0, 0, tbl_h, self.w, "networks", cols, items, self.network_cell,
                             right=f"{len(items)} network{'s' if len(items) != 1 else ''}  ·  x remove")
            self.loading(y0, data, items)
        elif items:
            sel = items[min(self.sel[3], len(items) - 1)]
        self.current = sel
        dy = y0 + tbl_h
        self.regions["detail"] = (dy, y1)
        L = []
        if sel:
            desc = self.DRIVER_INFO.get(sel["driver"], "")
            if sel["internal"]:
                desc += " It is INTERNAL: no way out to the internet through it, and ports of containers only on it cannot be published."
            if sel["name"] == "bridge":
                desc += " On the default 'bridge' containers can NOT find each other by name — on networks you (or compose) create, they can."
            for i, part in enumerate(wrap(desc, max(20, self.w - ev_w - 20))):
                L.append([("what it is: " if i == 0 else "            ", MAUVE, True), (part.strip(), SUB)])
            L.append([])
            gwtxt = sel["gw"] or "none"
            L.append([("◆ ", MAUVE), (sel["name"], TEXT, True), (f"  {sel['driver']}", DIM),
                      (f"  {sel['subnet']}" if sel["subnet"] else "", BLUE), (f"  gateway {gwtxt}", DIM)])
            if not sel["members"]:
                L.append([("└─ ", FAINT), ("no containers connected", FAINT)])
            by_name = sel["name"] != "bridge"  # docker DNS only works on user-defined networks
            for i, (r, ip) in enumerate(sel["members"]):
                last = i == len(sel["members"]) - 1
                running = r["state"] == "running"
                L.append([("└─ " if last else "├─ ", FAINT), ("● " if running else "○ ", GREEN if running else DIM),
                          (f"{r['name']:<24}", TEXT, True), (ip or "no ip (stopped)", TEAL if ip else FAINT)])
                if sel["driver"] in ("host", "null"):
                    continue
                pipe = "      " if last else "│     "
                public, private = [], []
                for pt in sorted(r["port_list"], key=lambda pt: pt.get("PrivatePort", 0)):
                    proto = "" if pt.get("Type", "tcp") == "tcp" else "/" + pt["Type"]
                    if pt.get("PublicPort") and (pt["PublicPort"], pt["PrivatePort"]) not in [x[:2] for x in public]:
                        public.append((pt["PublicPort"], pt["PrivatePort"], proto))
                    if pt["PrivatePort"] not in [x[0] for x in private]:
                        private.append((pt["PrivatePort"], proto))
                for hp, cp, proto in public:
                    line = [(pipe, FAINT), ("public   ", PEACH, True), (f"{hp}→{cp}{proto}", PEACH),
                            ("   from anywhere: ", DIM)]
                    line += [(f"http://{self.ip}:{hp}", BLUE, True)] if not proto else [(f"{self.ip}:{hp}{proto}", BLUE, True)]
                    L.append(line)
                if private:
                    host = (r["service"] or r["name"]) if by_name else (ip or r["name"])
                    addrs = ", ".join(f"{host}:{cp}{proto}" for cp, proto in private)
                    note = "   from containers on this network" if by_name else "   by IP only (default bridge has no names)"
                    L.append([(pipe, FAINT), ("internal ", TEAL, True), (addrs, TEAL), (note, DIM)])
                elif running and not public:
                    L.append([(pipe, FAINT), ("no ports exposed", FAINT)])
        self.lines_panel(dy, 0, det_h, self.w - ev_w, "network map", L, f"$ docker network inspect {sel['name']}" if sel else "")
        if ev_w:
            self.draw_events(dy, self.w - ev_w, det_h, ev_w)

    def network_cell(self, it, key, y, x, w, bg):
        if key == "dot":
            self.put(y, x, "●" if it["members"] else "○", self.pal(GREEN if it["members"] else DIM, bg) | BOLD)
        elif key == "name":
            self.segs(y, x, [(it["name"], TEXT, True), (" (built-in)" if it["name"] in ("bridge", "host", "none") else "", FAINT),
                             (" · internal" if it["internal"] else "", YELLOW)], x + w, bg)
        elif key in ("driver", "subnet", "gw"):
            self.put(y, x, it[key] or "—", self.pal(DIM if key == "driver" else BLUE if key == "subnet" else SUB, bg), x + w)
        elif key == "members":
            n = len(it["members"])
            self.put(y, x, f"{n} container" + ("s" if n != 1 else ""), self.pal(TEXT if n else FAINT, bg))

    def lines_panel(self, y, x, h, w, title, L, cmd):
        self.box(y, x, h, w, title, right="z enlarge")
        if cmd:
            self.segs(y + 1, x + 2, [(cmd[:2], MAUVE), (cmd[2:], DIM)], x + w - 2)
        body = h - 3
        self.detail_scroll = min(self.detail_scroll, max(0, len(L) - body))
        for i, line in enumerate(L[self.detail_scroll:self.detail_scroll + body]):
            self.segs(y + 2 + i, x + 2, line, x + w - 2)

    # ------------------------------------------------------------ footer and dialogs
    def draw_footer(self):
        y, w = self.h - 1, self.w
        self.put(y, 0, " " * (w - 1), self.pal(SUB, BAR_BG))
        if self.typing:
            self.segs(y, 1, [(" / ", BASE), ], bg=MAUVE)
            self.segs(y, 5, [("filter: ", SUB), (self.filter, TEXT, True), ("▏", MAUVE), ("   enter applies · esc clears", DIM)], w - 1, BAR_BG)
            return
        if self.col.error:
            self.segs(y, 1, [(" ✗ ", BASE), ], bg=RED)
            self.segs(y, 5, [(f"docker: {self.col.error}", RED)], w - 1, BAR_BG)
            return
        if self.msg and time.time() - self.msg[3] < 6:
            text, color, cmd, _ = self.msg
            x = self.segs(y, 1, [(f" {text} ", color, True)], w - 1, BAR_BG)
            if cmd:
                self.segs(y, x + 2, [("same as ", DIM), ("$ ", MAUVE), (cmd, TEXT, True)], w - 1, BAR_BG)
            return
        keys = {
            0: [("↑↓", "move"), ("←→", "tabs"), ("s", "start/stop"), ("r", "restart"), ("p", "pause"), ("e", "shell"),
                ("x", "remove"), ("l", "logs"), ("/", "filter"), ("c/m/n", "sort"), ("a", "stopped"), ("?", "help"), ("q", "quit")],
            1: [("↑↓", "move"), ("p", "pull"), ("r", "run"), ("x", "delete"), ("P", "prune dangling"), ("/", "filter"), ("z", "enlarge"), ("?", "help"), ("q", "quit")],
            2: [("↑↓", "move"), ("x", "delete"), ("/", "filter"), ("z", "enlarge"), ("?", "help"), ("q", "quit")],
            3: [("↑↓", "move"), ("x", "remove"), ("/", "filter"), ("z", "enlarge"), ("?", "help"), ("q", "quit")],
        }[self.view]
        x = 1
        for key, desc in keys:
            if x + len(key) + len(desc) + 3 >= w - 2:
                break
            x = self.segs(y, x, [(key, BLUE, True), (" " + desc + "  ", DIM)], w - 1, BAR_BG)
        tip = "tip: " + TIPS[int(time.time() / 9) % len(TIPS)]
        if w - x > len(tip) + 4:
            self.put(y, w - len(tip) - 2, tip, self.pal(FAINT, BAR_BG))

    def modal(self, h, w, title, color=MAUVE):
        h, w = min(h, self.h - 2), min(w, self.w - 4)
        y, x = (self.h - h) // 2, (self.w - w) // 2
        for i in range(h):
            self.put(y + i, x, " " * w, self.pal(TEXT, BASE))
        a = self.pal(color, BASE)
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", a)
        for i in range(1, h - 1):
            self.put(y + i, x, "│", a)
            self.put(y + i, x + w - 1, "│", a)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", a)
        self.put(y, x + 2, f" {title} ", self.pal(BASE, color) | BOLD)
        return y, x, h, w

    def draw_confirm(self):
        q, cli, _ = self.confirm
        w = max(len(q), len(cli) + 14) + 8
        y, x, h, w = self.modal(7, w, "confirm", PEACH)
        self.segs(y + 2, x + 3, [(q, TEXT, True)], x + w - 2, BASE)
        self.segs(y + 3, x + 3, [("same as ", DIM), ("$ ", MAUVE), (cli, SUB)], x + w - 2, BASE)
        self.segs(y + 5, x + 3, [(" y ", BASE, True)], bg=GREEN)
        self.segs(y + 5, x + 7, [("yes    ", SUB)], bg=BASE)
        self.segs(y + 5, x + 14, [(" n ", BASE, True)], bg=RED)
        self.segs(y + 5, x + 18, [("no ", SUB)], bg=BASE)
        fn = self.confirm[2]
        self.hit(y + 5, x + 3, x + 11, lambda: (setattr(self, "confirm", None), fn()))
        self.hit(y + 5, x + 14, x + 22, lambda: setattr(self, "confirm", None))

    def draw_run(self):
        f = self.form
        img = f["img"]
        if f["auto"]:
            ins = self.cache.get(("image", img["id"]), lambda: api("GET", f"/images/{quote(img['id'], safe='')}/json"), 300)
            if isinstance(ins, dict) or isinstance(ins, Exception):
                f["auto"] = False
                exposed = sorted(((ins.get("Config") or {}).get("ExposedPorts") or {}) if isinstance(ins, dict) else {})
                f["exposed"] = exposed
                if exposed:
                    num, _, proto = exposed[0].partition("/")
                    f["cport"] = num + (f"/{proto}" if proto not in ("", "tcp") else "")
                    cand = 8080 if int(num) < 1024 else int(num)
                    while port_user(cand, self.rows) and cand < 65535:
                        cand += 1
                    f["host"] = str(cand)
        y, x, h, w = self.modal(17, 76, f"run {f['ref']}", GREEN)
        lx, vx, mx = x + 3, x + 15, x + w - 2
        fy = {"name": y + 2, "host": y + 3, "cport": y + 3, "mode": y + 4, "rm": y + 5, "restart": y + 6}
        focus = RUN_FIELDS[f["focus"]]
        for name, label in (("name", "name"), ("host", "ports"), ("mode", "mode"), ("rm", "--rm"), ("restart", "restart")):
            on = focus == name or (name == "host" and focus == "cport")
            yy = fy[name]
            self.segs(yy, lx - 2, [("▸ " if on else "  ", GREEN, True), (label, TEXT if on else SUB, on)], mx, BASE)
            self.hit(yy, x + 1, x + w - 1, lambda n=name: f.__setitem__("focus", RUN_FIELDS.index(n)))

        def field(yy, xx, key, placeholder):
            on = focus == key
            if f[key]:
                xx = self.segs(yy, xx, [(f[key], TEXT, True)], mx, BASE)
            elif not on:
                xx = self.segs(yy, xx, [(placeholder, FAINT)], mx, BASE)
            if on:
                xx = self.segs(yy, xx, [("▏", GREEN, True)], mx, BASE)
            return xx

        field(fy["name"], vx, "name", "(random)")
        xx = field(fy["host"], vx, "host", "host")
        xx = self.segs(fy["host"], xx + 1, [("→ ", MAUVE, True)], mx, BASE)
        self.hit(fy["host"], xx, mx, lambda: f.__setitem__("focus", RUN_FIELDS.index("cport")))
        xx = field(fy["host"], xx, "cport", "container")
        exposed = f.get("exposed")
        if f["host"]:
            who = self.host_port_user()
            self.segs(fy["host"], xx + 3, [(f"✗ {f['host']} in use by {who}", RED)] if who else [("✓ free", GREEN)], mx, BASE)
        elif exposed is not None:
            self.segs(fy["host"], xx + 3, [("not published", DIM)], mx, BASE)
        modes = [("detached", "-d"), ("interactive", "-it")]
        xx = vx
        for i, (m, flag) in enumerate(modes):
            sel = f["mode"] == i
            xx = self.segs(fy["mode"], xx, [("● " if sel else "○ ", GREEN if sel else DIM, True), (m, TEXT if sel else SUB, sel),
                                            (f" {flag}   ", DIM)], mx, BASE)
        self.segs(fy["rm"], vx, [("☑ " if f["rm"] else "☐ ", GREEN if f["rm"] else DIM, True),
                                 ("delete when it stops" if f["rm"] else "keep it after it stops", SUB)], mx, BASE)
        xx = vx
        for i, r in enumerate(RESTARTS):
            sel = f["restart"] == i
            xx = self.segs(fy["restart"], xx, [(r, BASE if sel else SUB, sel)], mx, GREEN if sel else BASE)
            xx += 2
        hint = RUN_HINTS.get(focus) or RUN_HINTS[f"{focus}{f[focus]}"]
        if focus == "cport" and exposed:
            hint = f"the port the app listens on inside the container. This image exposes {', '.join(exposed)}."
        elif focus == "cport" and exposed == []:
            hint = "the port the app listens on inside the container. This image declares no EXPOSE: check its docs."
        for i, line in enumerate(wrap(hint, w - 6)[:2]):
            self.segs(y + 8 + i, lx, [(line, SKY)], mx, BASE)
        lines = wrap(" ".join(run_argv(f)), w - 10)[:3]
        for i, line in enumerate(lines):
            self.segs(y + 11 + i, lx, [("$ " if i == 0 else "  ", MAUVE, True), (line, TEXT, True),
                                       (" \\" if i < len(lines) - 1 else "", DIM)], mx, BASE)
        if f["err"]:
            self.segs(y + h - 2, lx, [("✗ " + f["err"], RED, True)], mx, BASE)
        keys = " enter run · tab next · space/←→ change · esc cancel "
        kx = x + max(2, w - len(keys) - 3)
        self.put(y + h - 1, kx, keys, self.pal(DIM, BASE), mx)
        self.hit(y + h - 1, kx, kx + 11, self.submit_run)

    def draw_pull(self):
        pl = self.pull
        with pl["lock"]:
            layers = [(k, dict(v)) for k, v in pl["layers"].items()]
        phase = pl["phase"]
        body = max(1, len(layers)) if phase != "input" else 0
        h = min(self.h - 2, 9 + body)
        title = "pull" if phase == "input" else f"pull {pl['ref']}"
        y, x, h, w = self.modal(h, 80, title, BLUE)
        lx, mx = x + 3, x + w - 2
        if phase == "input":
            t = pl["text"]
            self.segs(y + 2, lx, [("image  ", SUB), (t, TEXT, True), ("▏", BLUE, True),
                                  ("" if t else "e.g. nginx, nginx:alpine, postgres:17", FAINT)], mx, BASE)
            if t:
                reg, path, tag, default = split_ref(t)
                user, _, name = path.rpartition("/")
                self.segs(y + 4, lx, [(reg, PEACH, True), ("/", DIM), (user + "/" if user else "", YELLOW, True),
                                      (name, TEXT, True), (":", DIM), (tag, BLUE, True)], mx, BASE)
                self.segs(y + 5, lx, [("registry", PEACH), (" · ", DIM), ("user" if user != "library" else "library = official image", YELLOW),
                                      (" · ", DIM), ("name", TEXT), (" · ", DIM), ("tag" + (" (none given → latest)" if default else ""), BLUE)], mx, BASE)
                self.segs(y + 7, lx, [("$ ", MAUVE, True), (f"docker pull {t}", TEXT, True)], mx, BASE)
            else:
                for i, line in enumerate(wrap("Downloads an image from a registry (Docker Hub by default) so you can run it. "
                                              "Pulling an image you already have updates it to the newest version of that tag.", w - 6)[:3]):
                    self.segs(y + 4 + i, lx, [(line, SKY)], mx, BASE)
            keys = " enter pull · esc cancel "
        else:
            done_st = ("Pull complete", "Already exists", "Download complete")
            total = sum(ly["size"] for _, ly in layers)
            got = sum(ly["size"] if ly["st"] in done_st or ly["st"].startswith(("Extract", "Verif")) else
                      ly["cur"] if ly["st"] == "Downloading" else 0 for _, ly in layers)
            ndone = sum(1 for _, ly in layers if ly["st"] in ("Pull complete", "Already exists"))
            pct = 100 if phase == "done" else got / total * 100 if total else 0
            el = (pl["t1"] or time.time()) - pl["t0"]
            if layers or phase == "pulling":
                self.hbar(y + 2, lx, 30, pct, G_RX, BASE)
            if layers:
                self.segs(y + 2, lx + 32, [(f"{pct:3.0f}%  ", TEXT, True), (f"{human(got)} / {human(total)}  " if total else "", SUB),
                                           (f"{ndone}/{len(layers)} layers", DIM)], mx, BASE)
            rows = h - 8 if len(layers) <= h - 8 else h - 9
            for i, (lid, ly) in enumerate(layers[:rows]):
                yy, st = y + 4 + i, ly["st"]
                if phase == "done" and st != "Already exists":
                    st = "Pull complete"   # small config layers never report it
                self.put(yy, lx, lid[:12], self.pal(DIM, BASE))
                if st == "Already exists":
                    self.segs(yy, lx + 14, [("✓ already exists", GREEN), ("  shared with an image you have", FAINT)], mx, BASE)
                    continue
                if st == "Pull complete":
                    p, c = 100, G_LOAD[:1]
                elif st in ("Downloading", "Extracting") and ly["total"]:
                    p, c = ly["cur"] / ly["total"] * 100, (G_RX if st == "Downloading" else G_MEM)
                elif st in ("Download complete", "Verifying Checksum"):
                    p, c = 100, G_RX
                else:
                    p, c = 0, G_RX
                self.hbar(yy, lx + 14, 20, p, c, BASE)
                col = GREEN if st == "Pull complete" else BLUE if st == "Downloading" else MAUVE if st == "Extracting" else SUB
                size = (f"{human(ly['cur'])} / {human(ly['total'])}" if st in ("Downloading", "Extracting") and ly["total"]
                        else human(ly["size"]) if ly["size"] else "")
                self.segs(yy, lx + 36, [(f"{st.lower():<20}", col), (size, SUB)], mx, BASE)
            if len(layers) > rows:
                self.put(y + 4 + rows, lx, f"… +{len(layers) - rows} more layers", self.pal(DIM, BASE))
            if not layers and phase == "pulling":
                self.put(y + 4, lx, SPIN[self.frame % len(SPIN)] + " contacting the registry…", self.pal(SUB, BASE))
            elif not layers and phase == "done":
                self.put(y + 4, lx, "nothing to download: every layer was already on this machine", self.pal(DIM, BASE))
            sy = y + h - 4
            if phase == "pulling":
                self.segs(sy, lx, [(SPIN[self.frame % len(SPIN)] + " ", BLUE, True), (pl["status"] or "pulling", SUB),
                                   (f"  · {el:.0f}s", DIM)], mx, BASE)
                keys = " esc cancel "
            elif phase == "done":
                up = "up to date" in pl["status"]
                self.segs(sy, lx, [("✓ ", GREEN, True), ("already had the newest version" if up else f"downloaded in {el:.0f}s", GREEN, True)], mx, BASE)
                self.segs(sy + 1, lx, [(pl["status"].replace("Status: ", ""), DIM)], mx, BASE)
                keys = " r run it · enter select it · esc close "
            else:
                err = pl["err"] or ""
                why = (" · check the name and tag; private images need docker login" if ("denied" in err or "not found" in err)
                       and "login" not in err else "")
                for i, line in enumerate(wrap("✗ " + err + why, w - 6)[:2]):
                    self.segs(sy + i, lx, [(line, RED, i == 0)], mx, BASE)
                keys = " any key closes "
            self.segs(y + h - 2, lx, [("$ ", MAUVE, True), (f"docker pull {pl['ref']}", SUB)], mx, BASE)
        self.put(y + h - 1, x + max(2, w - len(keys) - 3), keys, self.pal(DIM, BASE), mx)

    HELP = [
        [("SHORTCUTS", MAUVE, True)],
        [("  1 2 3 4   ", BLUE, True), ("containers · images · volumes · networks  (or click the top bar)", SUB)],
        [("  ↑↓ j k    ", BLUE, True), ("select             ", SUB), ("←→ tab  ", BLUE, True), ("switch the bottom tab", SUB)],
        [("  PgUp/PgDn ", BLUE, True), ("scroll logs/info   ", SUB), ("End     ", BLUE, True), ("resume following logs", SUB)],
        [("  s         ", BLUE, True), ("start / stop       ", SUB), ("r       ", BLUE, True), ("restart", SUB)],
        [("  p         ", BLUE, True), ("pause / resume     ", SUB), ("x       ", BLUE, True), ("remove (stopped container, image, volume, network)", SUB)],
        [("  e         ", BLUE, True), ("shell in container ", SUB), ("l i t   ", BLUE, True), ("logs / info / processes tabs", SUB)],
        [("  z         ", BLUE, True), ("enlarge the panel  ", SUB), ("/       ", BLUE, True), ("filter by name", SUB)],
        [("  c m n     ", BLUE, True), ("sort cpu/mem/name  ", SUB), ("a       ", BLUE, True), ("show/hide stopped", SUB)],
        [("  b         ", BLUE, True), ("solid/transparent  ", SUB), ("g       ", BLUE, True), ("block / braille graphs", SUB)],
        [("  C         ", BLUE, True), ("clear the events panel ", SUB), ("r       ", BLUE, True), ("run a container (images view)", SUB)],
        [("  p         ", BLUE, True), ("pull an image (images view)", SUB)],
        [("  mouse     ", BLUE, True), ("click selects, wheel scrolls, click the tabs", SUB)],
        [],
        [("WHAT THE NUMBERS MEAN", MAUVE, True)],
        [("  CPU %     ", TEAL, True), ("100% = one full core busy. This server has ", SUB), (f"{os.cpu_count()}", TEXT, True), (" cores,", SUB)],
        [("            ", TEAL), ("so a container can reach ", SUB), (f"{(os.cpu_count() or 1) * 100}%", TEXT, True), (" (same math as docker stats).", SUB)],
        [("  MEMORY    ", TEAL, True), ("RAM used by the container, not counting file cache. The limit is", SUB)],
        [("            ", TEAL), ("the server's RAM unless you pass --memory to docker run.", SUB)],
        [("  NET ↓ ↑   ", TEAL, True), ("↓ = data coming into the container · ↑ = data going out. Per second.", SUB)],
        [("  PORTS     ", TEAL, True), ("8080→80 = whoever hits port 8080 on the SERVER lands on port 80 in", SUB)],
        [("            ", TEAL), ("the CONTAINER. It's the -p 8080:80 of docker run (host:container).", SUB)],
        [("  load      ", TEAL, True), ("average number of processes wanting CPU over 1, 5 and 15 min. Above", SUB)],
        [("            ", TEAL), ("the number of cores means a queue: the server is overloaded.", SUB)],
        [],
        [("STATES", MAUVE, True)],
        [("  ● ", GREEN, True), ("running   ", SUB), ("● ", RED, True), ("unhealthy (healthcheck failed)   ", SUB), ("◐ ", YELLOW, True), ("starting/checking", SUB)],
        [("  ○ ", DIM, True), ("exited ok ", SUB), ("✕ ", RED, True), ("exited with error (see logs)     ", SUB), ("‖ ", BLUE, True), ("paused (processes frozen)", SUB)],
        [("  ◇ ", DIM, True), ("created but never started", SUB)],
        [],
        [("COMMON EXIT CODES", MAUVE, True)],
        [("  0   ", TEXT, True), ("finished normally        ", SUB), ("1   ", TEXT, True), ("application error", SUB)],
        [("  137 ", TEXT, True), ("killed (SIGKILL / no RAM)", SUB), (" 143 ", TEXT, True), ("stopped with SIGTERM (docker stop)", SUB)],
        [("  127 ", TEXT, True), ("command not found        ", SUB), ("125 ", TEXT, True), ("error in docker run itself", SUB)],
        [],
        [("Every action shows the equivalent docker command in the footer — learn by using it.", DIM)],
    ]

    def draw_help(self):
        y, x, h, w = self.modal(len(self.HELP) + 4, 92, f"dockerax {VERSION} · help", MAUVE)
        body = h - 4
        self.help_scroll = min(self.help_scroll, max(0, len(self.HELP) - body))
        for i, line in enumerate(self.HELP[self.help_scroll:self.help_scroll + body]):
            self.segs(y + 2 + i, x + 3, line, x + w - 2, BASE)
        self.put(y + h - 1, x + w - 26, " any key closes ", self.pal(DIM, BASE))


def main(scr):
    curses.curs_set(0)
    scr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    col = Collector()
    col.start()
    ui = UI(scr, col)
    ui.current = None
    scr.timeout(1000 // FPS)
    try:
        while True:
            ui.draw()
            k = scr.getch()
            if k == -1:
                continue
            if k == curses.KEY_RESIZE:
                curses.update_lines_cols()
                continue
            if not ui.handle_key(k):
                break
    finally:
        ui.stop_logs()


if __name__ == "__main__":
    if not os.access(SOCK, os.R_OK | os.W_OK):
        raise SystemExit(f"no access to {SOCK} (is the user in the docker group?)")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(main)
