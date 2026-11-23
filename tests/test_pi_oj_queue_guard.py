"""Queue guard unit tests. No Redis server required."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from fastapi import HTTPException
import redis

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pi_oj_queue_guard_test', REPO / 'backend/queue_guard.py')
queue_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue_guard)


class QueueGuardTests(unittest.TestCase):
    def fake_redis(self, depth):
        client = mock.Mock()
        client.llen.return_value = depth
        return client

    def test_allows_under_limit(self):
        conn = self.fake_redis(19)
        with mock.patch.dict(os.environ, {'PI_OJ_MAX_QUEUE_DEPTH': '20'}), \
             mock.patch('redis.Redis.from_url', return_value=conn):
            queue_guard.ensure_queue_capacity()
        conn.close.assert_called_once()

    def test_rejects_at_limit(self):
        with mock.patch.dict(os.environ, {'PI_OJ_MAX_QUEUE_DEPTH': '20'}), \
             mock.patch('redis.Redis.from_url', return_value=self.fake_redis(20)):
            with self.assertRaises(HTTPException) as ctx:
                queue_guard.ensure_queue_capacity()
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.headers['Retry-After'], '5')

    def test_fails_closed_when_redis_unavailable(self):
        with mock.patch('redis.Redis.from_url', side_effect=redis.ConnectionError):
            with self.assertRaises(HTTPException) as ctx:
                queue_guard.ensure_queue_capacity()
        self.assertEqual(ctx.exception.status_code, 503)

    def test_rejects_invalid_setting(self):
        with mock.patch.dict(os.environ, {'PI_OJ_MAX_QUEUE_DEPTH': '0'}):
            with self.assertRaises(RuntimeError):
                queue_guard.ensure_queue_capacity()


if __name__ == '__main__':
    unittest.main()
