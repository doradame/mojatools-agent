# mojatools-agent

[![ci](https://github.com/doradame/mojatools-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/doradame/mojatools-agent/actions/workflows/ci.yml)

The inside-out liveness and metrics agent for the [mojatools](https://api.mojalab.com) monitoring platform.

Traditional monitoring probes your servers from the outside. That doesn't work for hosts behind a firewall or NAT. This agent runs **on** the monitored host and pushes a small heartbeat (plus optional metrics) **out** to your mojatools server over HTTPS — no inbound ports, no VPN, no firewall holes.

## What it is

- A **single Python file** (`mojatools_agent.py`), **standard library only** — nothing to `pip install`.
- Pushes liveness ("I'm alive") and, when you enable a full liveness check, host metrics: CPU, RAM, load, disk usage, listening ports, and (optionally) Docker container status.
- The server detects problems by **silence** (missed pushes) and by evaluating your thresholds/expected ports/containers against the last payload — then alerts you.
- Config (interval, thresholds, expected ports/containers) is pulled from the server at every push, so changes you make in the panel take effect on the agent automatically.

## Security model

The agent is designed to be small enough to audit in one sitting:

- **Stdlib only** — no third-party dependencies, ever. A tiny, fixed attack surface.
- **Runs unprivileged** — a dedicated system user (`mojatools-agent`, `nologin` shell). Never needs root at runtime.
- **Read-only collection** — metrics come from reading `/proc`; the agent writes only to its own state directory.
- **No code execution** — no `subprocess`, no `shell=True`, no `eval`/`exec`. Configuration pulled from the server is *data*, strictly whitelist-validated; invalid config is dropped and the last known-good config is kept.
- **TLS verification always on** — HTTPS only, with default certificate verification.
- **No auto-update** — the agent never downloads or runs new code by itself. When the server advertises a newer version it only logs "update available"; updating is your explicit decision (re-run the installer).
- **Token hygiene** — the agent token lives only in `/etc/mojatools-agent/token` (mode `0600`, owned by `mojatools-agent`) and is never logged.
- **Hardened systemd unit** — `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, writable path restricted to `/var/lib/mojatools-agent`.
- **Fail-quiet exit codes** — operational errors (network down, server unreachable) exit `0`; silence detection happens server-side. Exit `1` is reserved for "not enrolled" / bad usage.

## Install

The mojatools admin panel (**Agents → Install**) generates a one-liner for your host, with the enrollment token and the expected SHA-256 of the pinned agent release already filled in:

```sh
curl -fsSL https://raw.githubusercontent.com/doradame/mojatools-agent/v1.0.1/install.sh | sudo sh -s -- \
  --server https://api.mojalab.com \
  --enroll-token <ONE_TIME_TOKEN> \
  --expected-sha256 <SHA256_FROM_PANEL>
```

The installer:

1. Downloads the pinned `mojatools_agent.py` release and **verifies its SHA-256** before installing anything.
2. Creates the unprivileged `mojatools-agent` system user.
3. Enrolls the host against your server (one-time token).
4. Installs and starts a hardened systemd timer.

`--enroll-token` is required only on first install. On re-runs against an already-enrolled host (upgrade mode) it is optional — the existing enrollment is preserved.

`--agent-url URL` overrides the download URL of the agent file (used by the panel one-liner; defaults to the pinned GitHub release).

Requirements: Linux with systemd, Python 3.8+, curl, and root for the install step only.

### Docker container metrics (opt-in)

Add `--enable-docker` to also report container names and statuses via the Docker unix socket:

```sh
... sudo sh -s -- --server ... --enroll-token ... --expected-sha256 ... --enable-docker
```

> **Warning:** this adds the `mojatools-agent` user to the `docker` group, which is **effectively root-equivalent** on the host. Enable it only if you actually need container metrics. Without it, the `containers` field is simply reported as `null`.

## How it works

- A **systemd timer** fires the agent every minute (`OnUnitActiveSec=1min`, with a 30 s randomized delay).
- The agent **self-throttles**: it pushes only when the server-provided interval has elapsed (min 60 s, default 300 s), so the 1-minute tick is a cheap no-op when nothing is due. A file lock prevents overlapping runs.
- Every push doubles as a **config pull**: the response carries the effective checks (interval, thresholds, expected ports, expected containers), which the agent validates and persists. Change the frequency in the panel and the agent aligns within one interval.
- The **server** does the alerting: if no push arrives within the check interval plus its grace period, the liveness check fails and you get notified.

## Config files

| Path | Purpose | Permissions |
|------|---------|-------------|
| `/etc/mojatools-agent/agent.json` | `server_url`, `agent_id`, `hostname` (written at enrollment) | `0640 root:mojatools-agent` |
| `/etc/mojatools-agent/token` | the agent bearer token | `0600 mojatools-agent` |
| `/var/lib/mojatools-agent/state.json` | last push time + last validated server config | `0640 mojatools-agent` |

The agent code itself lives at `/opt/mojatools-agent/mojatools_agent.py` (owned by root, not writable by the agent user).

## Update

There is intentionally **no auto-update**. When the server advertises a newer `agent_version_latest`, the agent logs a line to the journal:

```
agent update available: X.Y.Z (current A.B.C) - re-run the installer to update
```

To update, re-run the installer one-liner from the panel — it downloads the new pinned release, verifies the checksum, and restarts the timer. Your enrollment (token and config) is preserved.

Check status any time with:

```sh
systemctl status mojatools-agent.timer
journalctl -u mojatools-agent.service -n 50
```

## Uninstall

```sh
sudo systemctl disable --now mojatools-agent.timer mojatools-agent.service
sudo rm /etc/systemd/system/mojatools-agent.timer /etc/systemd/system/mojatools-agent.service
sudo systemctl daemon-reload
sudo userdel mojatools-agent
sudo rm -rf /etc/mojatools-agent /var/lib/mojatools-agent /opt/mojatools-agent
```

Then revoke the agent in the mojatools admin panel so its token can no longer push.

## Development

- Python 3.8+, standard library only. Dev tooling (bandit, shellcheck) runs in CI, not on target hosts.
- Run the test suite:

  ```sh
  python3 -m unittest discover -s tests -v
  ```

- Repo layout:

  ```
  mojatools_agent.py   # the agent (single file)
  install.sh           # installer (POSIX sh), embeds the systemd units
  systemd/             # the unit files, for review (kept identical to install.sh)
  tests/               # unittest suite
  .github/workflows/   # CI: unittest matrix + bandit + shellcheck
  ```

## Release process

1. Bump `VERSION` in `mojatools_agent.py`.
2. Update `CHANGELOG.md`.
3. Tag the release: `git tag vX.Y.Z && git push --tags`.
4. Compute checksums: `sha256sum mojatools_agent.py install.sh > SHA256SUMS`.
5. Publish the GitHub release (attach `SHA256SUMS`).
6. Bump the installer tag pinned in the README one-liner and in the server-side `AGENT_INSTALLER_URL` config at every release.
7. On the mojatools server, update `AGENT_INSTALLER_URL`, `AGENT_EXPECTED_SHA256`, and `AGENT_VERSION_LATEST` so the panel one-liner pins the new release.

## License

MIT — see [LICENSE](LICENSE). Security reports: see [SECURITY.md](SECURITY.md).
