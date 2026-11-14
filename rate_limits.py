"""PostgreSQL admission adapter and historical local limiter for comparison tests."""

import threading
import time
from collections import deque

import psycopg

TEMPORARY_DATABASE_ERRORS = (
    psycopg.OperationalError,
    psycopg.errors.QueryCanceled,
    psycopg.errors.LockNotAvailable,
)


class DatabaseRateLimits:
    def __init__(self, get_store):
        self.get_store = get_store

    def admit(self, kind, identity, *, token=None, peer=None):
        # Account identity is re-derived from bearer in PostgreSQL, never trusted
        # from a UUID/query/header. Anonymous callers pass the actual socket peer.
        try:
            return self.get_store().admit(token if kind == "account" else None, peer or identity)
        except TEMPORARY_DATABASE_ERRORS:
            # Fail closed. No endpoint work runs if admission cannot commit.
            return None


class RateLimits:
    def __init__(self, *, clock=time.monotonic, account_limit=120, anonymous_limit=240, maximum_buckets=256):
        self.clock = clock
        self.account_limit = account_limit
        self.anonymous_limit = anonymous_limit
        self.maximum_buckets = maximum_buckets
        self.buckets = {}
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.buckets.clear()

    def admit(self, kind, identity, **_):
        if kind not in {"account", "anonymous"}:
            raise ValueError("Unknown limiter scope")
        key = (kind, identity)
        with self.lock:
            now = self.clock()
            for old_key in list(self.buckets):
                queue = self.buckets[old_key]
                while queue and now - queue[0] >= 60:
                    queue.popleft()
                if not queue:
                    del self.buckets[old_key]
            limit = self.account_limit if kind == "account" else self.anonymous_limit
            queue = self.buckets.get(key)
            if queue is None:
                # Never evict an active identity: eviction could reset its quota.
                if sum(old_key[0] == kind for old_key in self.buckets) >= self.maximum_buckets:
                    return False
                queue = self.buckets[key] = deque(maxlen=limit)
            if len(queue) >= limit:
                return False
            queue.append(now)
            return True
