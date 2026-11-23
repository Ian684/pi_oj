"""Fail-closed guard for Pi OJ's host-provided sandbox tmpfs.

This protects local judging from accidental use of an unbounded host bind
mount. It does NOT implement a per-submission quota or harden Docker Socket.
"""

import os
from pathlib import Path


class SandboxQuotaUnavailable(RuntimeError):
    """The requested sandbox storage guard is not configured safely."""


def _decode_mount_path(path: str) -> str:
    # Mountinfo escapes whitespace and backslashes with octal notation.
    for code, char in (("040", " "), ("011", "\t"), ("012", "\n"), ("134", "\\")):
        path = path.replace("\\" + code, char)
    return path


def mount_type_from_mountinfo(mountinfo: str, target: str) -> str | None:
    """Return filesystem type for an exact mount point in /proc/self/mountinfo."""
    found = None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        left_fields, right_fields = left.split(), right.split()
        if len(left_fields) < 5 or len(right_fields) < 1:
            continue
        if _decode_mount_path(left_fields[4]) == target:
            found = right_fields[0]
    return found


def check_sandbox_quota(
    target: str = "/sandboxes",
    mountinfo_path: str = "/proc/self/mountinfo",
    max_mib: int | None = None,
) -> int:
    """Return usable total bytes or raise if an exact bounded tmpfs is absent."""
    if os.getenv("PI_OJ_REQUIRE_SANDBOX_TMPFS", "1") == "0":
        # Explicit local development escape hatch. NEVER use for public judging.
        return 0

    if max_mib is None:
        raw = os.getenv("PI_OJ_SANDBOX_MAX_MIB", "1024")
        try:
            max_mib = int(raw)
        except ValueError as exc:
            raise SandboxQuotaUnavailable("PI_OJ_SANDBOX_MAX_MIB must be an integer") from exc
    if not (32 <= max_mib <= 65536):
        raise SandboxQuotaUnavailable("PI_OJ_SANDBOX_MAX_MIB must be 32..65536")

    try:
        mountinfo = Path(mountinfo_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SandboxQuotaUnavailable("Cannot inspect Linux sandbox mounts") from exc

    fstype = mount_type_from_mountinfo(mountinfo, target)
    if fstype != "tmpfs":
        raise SandboxQuotaUnavailable(
            f"{target} must be an explicitly mounted tmpfs (got {fstype or 'no mount'}). "
            "On the Linux Docker host, install the persistent quota before starting the worker."
        )
    try:
        stats = os.statvfs(target)
    except OSError as exc:
        raise SandboxQuotaUnavailable(f"Cannot stat {target}") from exc

    capacity = stats.f_blocks * stats.f_frsize
    if capacity <= 0 or capacity > max_mib * 1024 * 1024:
        raise SandboxQuotaUnavailable(
            f"Unsafe sandbox capacity {capacity} bytes; expected 1..{max_mib} MiB"
        )
    return capacity
