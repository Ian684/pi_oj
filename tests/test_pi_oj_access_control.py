"""Basic access and anti-abuse smoke checks (no Docker required)."""
import base64
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

backend_dir = Path(__file__).resolve().parents[1] / "backend"
if not backend_dir.is_dir():  # Docker image installs backend modules directly into /app
    backend_dir = Path("/app")
sys.path.insert(0, str(backend_dir))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from access_control import check_basic_authorization, install_access_control
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False


@unittest.skipUnless(HAS_DEPS, "fastapi/httpx missing; run inside web container")
class AccessTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"PI_OJ_BASIC_USER": "tester", "PI_OJ_BASIC_PASSWORD": "correct-key"})
        self.env.start()
        self.app = FastAPI()
        install_access_control(self.app)

        @self.app.get("/api/health")
        def health():
            return {"ok": True}

        @self.app.get("/private")
        def private():
            return {"ok": True}

        @self.app.post("/api/ai/help")
        def ai():
            return {"ok": True}

        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.env.stop()

    def auth(self):
        token = base64.b64encode(b"tester:correct-key").decode("ascii")
        return {"Authorization": "Basic " + token}

    def test_public_health_only(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/private").status_code, 401)
        self.assertIn("Basic", self.client.get("/private").headers["www-authenticate"])

    def test_correct_login(self):
        self.assertEqual(self.client.get("/private", headers=self.auth()).status_code, 200)

    def test_rejects_wrong_password(self):
        self.assertFalse(check_basic_authorization(
            "Basic " + base64.b64encode(b"tester:bad").decode(), "tester", "correct-key"
        ))
        self.assertFalse(check_basic_authorization("Basic !!!!", "tester", "correct-key"))
        self.assertFalse(check_basic_authorization("Bearer x", "tester", "correct-key"))

    def test_ai_quota(self):
        for _ in range(15):
            self.assertEqual(self.client.post("/api/ai/help", headers=self.auth()).status_code, 200)
        response = self.client.post("/api/ai/help", headers=self.auth())
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)

    def test_no_secret_configuration_refuses_boot(self):
        with patch.dict(os.environ, {"PI_OJ_BASIC_USER": "", "PI_OJ_BASIC_PASSWORD": ""}):
            with self.assertRaises(RuntimeError):
                install_access_control(FastAPI())


if __name__ == "__main__":
    unittest.main()
