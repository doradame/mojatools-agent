#!/usr/bin/env python3
"""mojatools-agent - inside-out liveness & metrics agent for mojatools.

Single file, Python standard library only. Designed to be auditable:
no third-party dependencies, no subprocess/shell=True, no eval/exec,
read-only metric collection from /proc, TLS-verified HTTPS only.

Subcommands:
  run      collect + push (invoked by the systemd timer, self-throttling)
  enroll   one-time enrollment (invoked by install.sh)
  version  print version
"""
import fcntl
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

VERSION = "1.0.0"

CONFIG_PATH = "/etc/mojatools-agent/agent.json"
TOKEN_PATH = "/etc/mojatools-agent/token"
STATE_DIR = "/var/lib/mojatools-agent"
STATE_PATH = os.path.join(STATE_DIR, "state.json")
LOCK_PATH = os.path.join(STATE_DIR, "agent.lock")

HTTP_TIMEOUT = 30
MAX_BODY_BYTES = 65536
RETRY_DELAYS = (5, 15, 45)
MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 86400
DEFAULT_INTERVAL_S = 300

PSEUDO_FS = frozenset({
    "tmpfs", "proc", "sysfs", "devpts", "devtmpfs", "cgroup", "cgroup2",
    "overlay", "squashfs", "autofs", "mqueue", "shm", "securityfs",
    "debugfs", "tracefs", "pstore", "bpf", "configfs", "fusectl",
    "hugetlbfs", "ramfs", "nsfs",
})
SKIP_MOUNT_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/snap")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


def log(msg):
    # stderr is captured by journald; never log secrets here.
    sys.stderr.write(f"mojatools-agent[{os.getpid()}]: {msg}\n")
    sys.stderr.flush()


def utcnow():
    return datetime.now(timezone.utc)


# ---------- config / state ----------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json_atomic(path, data, mode=0o640):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_token():
    with open(TOKEN_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


# ---------- HTTP ----------

def http_post(server, path, payload, token=None):
    """POST JSON over verified TLS. Returns (status, parsed_json)."""
    url = server.rstrip("/") + path
    if not url.startswith("https://"):
        raise ValueError("server URL must be https://")
    body = json.dumps(payload).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("payload too large")
    headers = {"Content-Type": "application/json",
               "User-Agent": f"mojatools-agent/{VERSION}"}
    if token:
        headers["X-AGENT-TOKEN"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = ssl.create_default_context()  # certificate verification ON
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
        raw = resp.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError("response too large")
        return resp.status, json.loads(raw.decode("utf-8"))


def post_with_retries(server, path, payload, token=None):
    last_err = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            return http_post(server, path, payload, token)
        except urllib.error.HTTPError as e:
            # 4xx/5xx with a response: definitive, do not retry.
            body = e.read(512).decode("utf-8", "replace")
            raise RuntimeError(f"server rejected push: HTTP {e.code} {body[:200]}")
        except Exception as e:  # network-level errors: retry
            last_err = e
            log(f"push attempt {attempt + 1} failed: {type(e).__name__}: {e}")
    raise RuntimeError(f"push failed after retries: {last_err}")


# ---------- collectors (read-only) ----------

def _parse_cpu_times(line):
    """'cpu  user nice system idle iowait ...' -> (total_jiffies, idle_jiffies)."""
    vals = [int(x) for x in line.split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def _cpu_percent(t1, i1, t2, i2):
    dt, di = t2 - t1, i2 - i1
    if dt <= 0:
        return None
    return round(100.0 * (1.0 - di / dt), 2)


def collect_cpu_percent(proc_root="/proc", sample_s=0.5):
    path = os.path.join(proc_root, "stat")
    with open(path, "r", encoding="utf-8") as f:
        t1, i1 = _parse_cpu_times(f.readline())
    time.sleep(sample_s)
    with open(path, "r", encoding="utf-8") as f:
        t2, i2 = _parse_cpu_times(f.readline())
    return _cpu_percent(t1, i1, t2, i2)


def collect_memory(proc_root="/proc"):
    info = {}
    with open(os.path.join(proc_root, "meminfo"), "r", encoding="utf-8") as f:
        for line in f:
            key, _, rest = line.partition(":")
            parts = rest.strip().split()
            if parts:
                info[key] = int(parts[0])  # kB
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    if not total:
        return None, None
    return round(100.0 * (total - avail) / total, 2), total // 1024


def collect_load(proc_root="/proc"):
    with open(os.path.join(proc_root, "loadavg"), "r", encoding="utf-8") as f:
        load1 = float(f.read().split()[0])
    return load1, (os.cpu_count() or 1)


def collect_disks(proc_root="/proc"):
    disks = []
    with open(os.path.join(proc_root, "mounts"), "r", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 3:
                continue
            mount, fstype = parts[1], parts[2]
            if fstype in PSEUDO_FS or mount.startswith(SKIP_MOUNT_PREFIXES):
                continue
            try:
                st = os.statvfs(mount)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            if total <= 0:
                continue
            disks.append({"mount": mount,
                          "percent": round(100.0 * (total - avail) / total, 2)})
    return disks[:64]


def collect_listening_ports(proc_root="/proc"):
    ports = set()
    for name in ("net/tcp", "net/tcp6"):
        try:
            with open(os.path.join(proc_root, name), "r", encoding="utf-8") as f:
                lines = f.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) > 3 and parts[3] == "0A":  # 0A = LISTEN
                try:
                    ports.add(int(parts[1].rsplit(":", 1)[1], 16))
                except (ValueError, IndexError):
                    continue
    return sorted(ports)[:1000]


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._socket_path)
        self.sock = s


def collect_docker_containers(socket_path="/var/run/docker.sock", timeout=5):
    """Container name + status only (no image/env/labels). None if unavailable."""
    if not os.path.exists(socket_path):
        return None
    try:
        conn = _UnixHTTPConnection(socket_path, timeout)
        conn.request("GET", "/containers/json")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        data = json.loads(resp.read(1 << 20).decode("utf-8"))
        return [{"name": (c.get("Names") or ["?"])[0].lstrip("/"),
                 "status": str(c.get("State", "unknown"))[:32]} for c in data][:200]
    except Exception:
        return None


def collect_metrics(proc_root="/proc", docker_socket="/var/run/docker.sock"):
    ram_percent, ram_total_mb = collect_memory(proc_root)
    load1, cores = collect_load(proc_root)
    return {
        "cpu_percent": collect_cpu_percent(proc_root),
        "ram_percent": ram_percent,
        "ram_total_mb": ram_total_mb,
        "load1": load1,
        "cores": cores,
        "disks": collect_disks(proc_root),
        "listening_ports": collect_listening_ports(proc_root),
        "containers": collect_docker_containers(docker_socket),
    }


# ---------- server config (pull) ----------

def validate_server_config(data):
    """Strict whitelist validation of the config pulled from the server.
    Data, never code: unknown keys dropped; bad types raise ValueError and
    the caller keeps the last known good config."""
    if not isinstance(data, dict):
        raise ValueError("server config is not an object")
    clean = {"server_time": str(data.get("server_time", ""))[:40],
             "agent_version_latest": str(data.get("agent_version_latest", ""))[:32],
             "checks": []}
    checks = data.get("checks")
    if not isinstance(checks, list):
        raise ValueError("server config: 'checks' must be a list")
    for c in checks[:20]:
        if not isinstance(c, dict):
            raise ValueError("server config: check entry must be an object")
        try:
            ctype = c.get("type")
            if ctype not in ("liveness_light", "liveness_full"):
                raise ValueError(f"server config: unknown check type {ctype!r}")
            entry = {
                "check_id": int(c["check_id"]),
                "type": ctype,
                "interval_seconds": max(MIN_INTERVAL_S, min(
                    int(c.get("interval_seconds", DEFAULT_INTERVAL_S)), MAX_INTERVAL_S)),
                "thresholds": {},
                "expected_ports": [],
                "expected_containers": [],
            }
            thresholds = c.get("thresholds") or {}
            if not isinstance(thresholds, dict):
                raise ValueError("server config: thresholds must be an object")
            for k in ("disk_percent", "ram_percent", "cpu_percent", "load_per_core"):
                if k in thresholds:
                    entry["thresholds"][k] = float(thresholds[k])
            ports = c.get("expected_ports") or []
            if not all(isinstance(p, int) and 1 <= p <= 65535 for p in ports):
                raise ValueError("server config: expected_ports must be valid ports")
            entry["expected_ports"] = sorted(set(ports))[:100]
            names = c.get("expected_containers") or []
            if not all(isinstance(n, str) and 0 < len(n) <= 128 for n in names):
                raise ValueError("server config: expected_containers must be names")
            entry["expected_containers"] = list(names)[:50]
        except (KeyError, TypeError) as e:
            raise ValueError(f"server config: malformed check entry: {e}")
        clean["checks"].append(entry)
    return clean


def effective_mode(server_config):
    checks = (server_config or {}).get("checks", [])
    return "full" if any(c.get("type") == "liveness_full" for c in checks) else "light"


def effective_interval(server_config):
    intervals = [c["interval_seconds"] for c in (server_config or {}).get("checks", [])]
    return min(intervals) if intervals else DEFAULT_INTERVAL_S


# ---------- main logic ----------

def should_push(state, interval_s, now=None):
    now = now or utcnow()
    last = (state or {}).get("last_push_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    if last_dt.tzinfo is None and now.tzinfo is not None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    elif now.tzinfo is None and last_dt.tzinfo is not None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - last_dt).total_seconds() >= interval_s


def cmd_run():
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)  # previous run still in progress
    cfg = load_json(CONFIG_PATH, {})
    if not cfg.get("server_url") or not cfg.get("agent_id"):
        log("agent not enrolled (run the installer first)")
        sys.exit(1)
    state = load_json(STATE_PATH, {})
    server_cfg = state.get("server_config") or {}
    if not should_push(state, effective_interval(server_cfg)):
        sys.exit(0)
    payload = {"hostname": cfg.get("hostname") or socket.gethostname(),
               "agent_version": VERSION}
    if effective_mode(server_cfg) == "full":
        payload["metrics"] = collect_metrics()
    try:
        _, response = post_with_retries(cfg["server_url"], "/agent/v1/push",
                                        payload, token=read_token())
    except Exception as e:
        log(str(e))
        sys.exit(0)  # silence is detected server-side; never fail the timer
    try:
        state["server_config"] = validate_server_config(response)
    except ValueError as e:
        log(f"ignoring invalid server config: {e}")
    state["last_push_at"] = utcnow().isoformat()
    save_json_atomic(STATE_PATH, state)
    latest = (state.get("server_config") or {}).get("agent_version_latest")
    if latest and latest != VERSION:
        log(f"agent update available: {latest} (current {VERSION}) - "
            "re-run the installer to update")
    sys.exit(0)


def cmd_enroll(server, enroll_token, hostname):
    if not HOSTNAME_RE.match(hostname or ""):
        log("invalid hostname")
        sys.exit(1)
    _, data = http_post(server, "/agent/v1/enroll",
                        {"enrollment_token": enroll_token, "hostname": hostname,
                         "agent_version": VERSION})
    os.makedirs(STATE_DIR, exist_ok=True)
    save_json_atomic(CONFIG_PATH, {"server_url": server.rstrip("/"),
                                   "agent_id": data["agent_id"],
                                   "hostname": hostname})
    tmp = TOKEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data["agent_token"] + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_PATH)
    log(f"enrolled as agent {data['agent_id']} on {server}")
    sys.exit(0)


def main(argv):
    if len(argv) >= 2 and argv[1] == "enroll":
        server = token = hostname = None
        args = argv[2:]
        for i, a in enumerate(args):
            if a == "--server" and i + 1 < len(args):
                server = args[i + 1]
            elif a == "--enroll-token" and i + 1 < len(args):
                token = args[i + 1]
            elif a == "--hostname" and i + 1 < len(args):
                hostname = args[i + 1]
        if not (server and token and hostname):
            log("usage: mojatools_agent.py enroll --server URL --enroll-token T --hostname H")
            sys.exit(2)
        cmd_enroll(server, token, hostname)
    elif len(argv) >= 2 and argv[1] == "version":
        print(VERSION)
        sys.exit(0)
    else:
        cmd_run()


if __name__ == "__main__":
    main(sys.argv)
