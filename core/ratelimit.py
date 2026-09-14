"""Per-owner sliding-window rate limit.

There was a global queue depth cap and a global concurrency semaphore, but
nothing stopped one visitor from occupying both GPU slots back-to-back with
rapid requests. No Redis at this scale: one process, in-memory deque per
owner, pruned lazily on each check so it never grows unbounded.
"""
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 60
MAX_PER_WINDOW = 12          # generous for real use, tight for a script
MAX_TRACKED_OWNERS = 5000    # bound memory; oldest-idle evicted past this


class RateLimiter:
    def __init__(self, window: float = WINDOW_SECONDS, limit: int = MAX_PER_WINDOW):
        self.window = window
        self.limit = limit
        self._hits: dict[str, deque] = defaultdict(deque)
        self._last_seen: dict[str, float] = {}

    def check(self, owner: str) -> tuple[bool, int]:
        """Returns (allowed, seconds_until_next_slot_if_blocked)."""
        now = time.time()
        q = self._hits[owner]
        while q and now - q[0] > self.window:
            q.popleft()
        self._last_seen[owner] = now
        if len(q) >= self.limit:
            retry_after = int(self.window - (now - q[0])) + 1
            return False, retry_after
        q.append(now)
        if len(self._hits) > MAX_TRACKED_OWNERS:
            self._evict_idle()
        return True, 0

    def _evict_idle(self) -> None:
        stale = sorted(self._last_seen, key=self._last_seen.get)[:500]
        for owner in stale:
            self._hits.pop(owner, None)
            self._last_seen.pop(owner, None)
