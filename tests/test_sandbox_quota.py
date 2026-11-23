"""Unit tests for Pi OJ bounded-tmpfs mount verification. No Docker required."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock
import tempfile

MODULE_PATH = Path(__file__).resolve().parent.parent / "backend" / "sandbox_quota.py"
spec = importlib.util.spec_from_file_location("sandbox_quota_under_test", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def mountline(fs="tmpfs", mount="/sandboxes"):
    return f"301 211 0:84 / {mount} rw,relatime - {fs} tmpfs rw,size=536870912\n"


class MountInfoTests(TestCase):
    def test_tmpfs_exact_mount(self):
        self.assertEqual(mod.mount_type_from_mountinfo(mountline(), "/sandboxes"), "tmpfs")

    def test_non_tmpfs_rejected(self):
        self.assertEqual(mod.mount_type_from_mountinfo(mountline("ext4"), "/sandboxes"), "ext4")

    def test_parent_not_substitute(self):
        self.assertIsNone(mod.mount_type_from_mountinfo(mountline(mount="/"), "/sandboxes"))

    def test_escaped_mountpath(self):
        self.assertEqual(
            mod.mount_type_from_mountinfo(mountline(mount=r"/my\040path"), "/my path"),
            "tmpfs",
        )

    def test_reject_missing_quota(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"PI_OJ_REQUIRE_SANDBOX_TMPFS": "1"}):
            path = Path(tmp) / "mountinfo"
            path.write_text(mountline("ext4"), encoding="utf-8")
            with self.assertRaises(mod.SandboxQuotaUnavailable):
                mod.check_sandbox_quota(mountinfo_path=str(path))

    def test_reject_too_large_tmpfs(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"PI_OJ_REQUIRE_SANDBOX_TMPFS": "1"}), mock.patch.object(mod.os, "statvfs", return_value=SimpleNamespace(f_blocks=2048, f_frsize=1048576)):
            path = Path(tmp) / "mountinfo"
            path.write_text(mountline(), encoding="utf-8")
            with self.assertRaises(mod.SandboxQuotaUnavailable):
                mod.check_sandbox_quota(mountinfo_path=str(path), max_mib=1024)

    def test_accept_bounded_tmpfs(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"PI_OJ_REQUIRE_SANDBOX_TMPFS": "1"}), mock.patch.object(mod.os, "statvfs", return_value=SimpleNamespace(f_blocks=512, f_frsize=1048576)):
            path = Path(tmp) / "mountinfo"
            path.write_text(mountline(), encoding="utf-8")
            self.assertEqual(mod.check_sandbox_quota(mountinfo_path=str(path)), 512 * 1024 * 1024)

    def test_dev_override_is_explicit(self):
        with mock.patch.dict(os.environ, {"PI_OJ_REQUIRE_SANDBOX_TMPFS": "0"}):
            self.assertEqual(mod.check_sandbox_quota(mountinfo_path="/does/not/exist"), 0)
