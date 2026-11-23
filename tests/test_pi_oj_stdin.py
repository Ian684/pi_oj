"""No-Docker tests for the per-job file-based stdin in RUNNER_WRAPPER."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def runner_source():
    root = ast.parse((ROOT / 'backend/tasks.py').read_text(encoding='utf8'))
    for entry in root.body:
        if isinstance(entry, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'RUNNER_WRAPPER' for t in entry.targets):
            return ast.literal_eval(entry.value)
    raise AssertionError('RUNNER_WRAPPER missing')


@unittest.skipUnless(os.name == 'posix', 'Linux/POSIX environment required')
class FileStdinTests(unittest.TestCase):
    def run_runner(self, code, stdin_content):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.runner.py').write_text(runner_source(), encoding='utf8')
            (root / '.stdin').write_bytes(stdin_content)
            env = dict(os.environ)
            env['OJ_STDIN_FILE'] = str(root / '.stdin')
            result = subprocess.run([sys.executable, str(root / '.runner.py'), '--',
                    sys.executable, '-c', code], cwd=root, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            return result.returncode, (root / '.stdout').read_bytes(), (root / '.stderr').read_bytes()

    def test_stdin_is_read_from_file(self):
        result, out, err = self.run_runner('import sys; print(sys.stdin.read())', b'abc\n')
        self.assertEqual(result, 0, err)
        self.assertEqual(out, b'abc\n\n')

    def test_program_ignoring_large_stdin_does_not_block(self):
        result, out, err = self.run_runner("print('ok')", b'Z' * 1_000_000)
        self.assertEqual(result, 0, err)
        self.assertEqual(out, b'ok\n')

    def test_existing_runner_stdin_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.runner.py').write_text(runner_source(), encoding='utf8')
            env = dict(os.environ)
            env.pop('OJ_STDIN_FILE', None)
            run = subprocess.run([sys.executable, str(root / '.runner.py'), '--',
                    sys.executable, '-c', 'import sys; print(sys.stdin.read())'],
                    cwd=root, env=env, input=b'fallback', capture_output=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((root / '.stdout').read_bytes(), b'fallback\n')


if __name__ == '__main__':
    unittest.main()
