# Pi OJ security architecture and remaining risks

This is a private single-owner Online Judge. Its resource limits and Basic Auth are **defense-in-depth, not a security guarantee for arbitrary internet users**.

## Protection currently implemented

- Local jobs run as a non-root user inside Docker with network disabled, restricted CPU/memory/PID limits, dropped Linux capabilities and a read-only container root filesystem.
- A bounded host tmpfs can limit **total shared** sandbox temporary-file storage. The judge denies local runs when `PI_OJ_REQUIRE_SANDBOX_TMPFS=1` and it cannot detect a suitably sized tmpfs mount.
- Basic Auth and best-effort rate/queue limits help protect a small private instance.

## Unresolved high-impact risk: Docker Socket

`worker` mounts `/var/run/docker.sock` and currently runs as UID 0 so it can launch evaluation containers. A process with access to a rootful Docker API may be able to control the host, regardless of `:ro` mount flags on the socket. Do **not** expose the Docker socket or Docker API over the public network. Changing `user: "0:0"` alone does not remove the Docker API authority.

Recommended migration for public use:

1. Keep FastAPI / Redis on a separate low-trust service tier with no Docker Socket.
2. Move the judge execution component to a dedicated VM or machine that has no sensitive host data, secrets, or access to unrelated workloads. The VM remains disposable and network-restricted.
3. Consider a dedicated rootless Docker daemon for that judge environment and validate cgroup memory/CPU/PID limits, filesystem mounts, image access and cleanup on the exact Linux/ARM64 host. Rootless mode reduces some host privileges but is **not** a substitute for an isolated host or VM.
4. Give the judge only the job fields it needs, and avoid copying UVA / Codeforces / AI credentials into the sandbox execution tier.
5. Review threat models for malicious submissions, subprocesses, file-exhaustion, kernel attack surface, queue abuse, Docker API compromise, and availability failures.

## Storage caveats

The shared tmpfs capacity is **not a per-submission quota**. One job can fill it, and memory pressure can still affect the host. For true per-submission limits, isolate each submission with a separately bounded filesystem or a suitable project-quota-capable filesystem; that is a separate architectural change needing end-to-end tests.

## Deployment checklist

- [ ] `.env` is private and uses unique strong credentials.
- [ ] Web binds to loopback or uses HTTPS and authenticated proxy access.
- [ ] Shared sandbox tmpfs is mounted persistently and verified **inside the worker**.
- [ ] Local judging fails closed if the mount is missing or oversized.
- [ ] Redis and Docker APIs are not publicly accessible.
- [ ] AC/WA/CE/RE/TLE/OLE/MLE workflows and cleanup verified on ARM64 Raspberry Pi.
- [ ] Backups, logs, emergency shutdown and system updates are in place.
- [ ] Arbitrary public submissions are not permitted until Docker Socket/worker isolation is addressed.
