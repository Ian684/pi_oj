"""Sandbox wrapper regression tests, no Docker needed.

From the Pi OJ repository root:
    python -m unittest discover -s tests -p 'test_pi_oj_runner.py' -v
Linux required for the process group kill tests.
"""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parent.parent


def load_runner():
    module = ast.parse((REPO / "backend/tasks.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "RUNNER_WRAPPER"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("RUNNER_WRAPPER not found")


@unittest.skipUnless(os.name == "posix", "Linux/POSIX process-group behavior")
class RunnerTests(unittest.TestCase):
    def run_program(self, code, stdout_limit=1024, stderr_limit=1024):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = root / ".runner.py"
            runner.write_text(load_runner(), encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "OJ_STDOUT_CAPTURE_LIMIT": str(stdout_limit),
                "OJ_STDERR_CAPTURE_LIMIT": str(stderr_limit),
            })
            completed = subprocess.run(
                [sys.executable, str(runner), "--", sys.executable, "-c", code],
                cwd=root, env=env, timeout=5, capture_output=True,
            )
            return (
                completed.returncode,
                (root / ".stdout").read_bytes(),
                (root / ".stderr").read_bytes(),
                (root / ".runner_meta").read_text(encoding="utf-8"),
            )

    def test_normal_output(self):
        status, out, err, meta = self.run_program("print('hello')")
        self.assertEqual(status, 0)
        self.assertEqual(out, b"hello\n")
        self.assertEqual(err, b"")
        self.assertIn("stdout_truncated=0", meta)

    def test_output_exactly_at_limit(self):
        status, out, _, meta = self.run_program("import os; os.write(1,b'x'*1024)")
        self.assertEqual(status, 0)
        self.assertEqual(len(out), 1024)
        self.assertIn("stdout_truncated=0", meta)

    def test_large_finite_stdout(self):
        status, out, _, meta = self.run_program("import os; os.write(1,b'x'*200000)")
        self.assertEqual(status, 122)
        self.assertEqual(len(out), 1024)
        self.assertIn("stdout_truncated=1", meta)

    def test_large_stderr(self):
        status, _, err, meta = self.run_program("import os; os.write(2,b'e'*8192)", stderr_limit=256)
        self.assertEqual(status, 122)
        self.assertEqual(len(err), 256)
        self.assertIn("stderr_truncated=1", meta)

    def test_infinite_output_is_stopped(self):
        status, out, _, meta = self.run_program(
            "import os\nwhile True: os.write(1,b'z'*8192)"
        )
        self.assertEqual(status, 122)
        self.assertEqual(len(out), 1024)
        self.assertIn("stdout_truncated=1", meta)


if __name__ == "__main__":
    unittest.main()
