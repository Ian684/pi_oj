"""Fail-closed, best-effort Celery Redis queue backpressure for a private Pi OJ.

This bounds *waiting* tasks, not active/prefetched tasks, and is not an
atomic global quota for multiple API processes. Rate limiting remains in
access_control.py.
"""

import os

import redis
from fastapi import HTTPException


def ensure_queue_capacity() -> None:
    try:
        limit = int(os.getenv("PI_OJ_MAX_QUEUE_DEPTH", "20"))
    except ValueError as exc:
        raise RuntimeError("PI_OJ_MAX_QUEUE_DEPTH must be an integer") from exc
    if limit < 1 or limit > 10_000:
        raise RuntimeError("PI_OJ_MAX_QUEUE_DEPTH must be between 1 and 10000")

    broker_url = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    queue_name = os.getenv("PI_OJ_CELERY_QUEUE_NAME", "celery")
    if not queue_name or any(ch.isspace() for ch in queue_name):
        raise RuntimeError("PI_OJ_CELERY_QUEUE_NAME must be one Redis key")
    try:
        conn = redis.Redis.from_url(
            broker_url, socket_connect_timeout=1.0, socket_timeout=1.0
        )
        try:
            depth = conn.llen(queue_name)
        finally:
            conn.close()
    except (redis.RedisError, ValueError) as exc:
        raise HTTPException(503, detail="評測佇列暫時無法使用，請稍後再試") from exc
    if depth >= limit:
        raise HTTPException(429, detail="評測佇列已滿，請稍後再試", headers={"Retry-After": "5"})
