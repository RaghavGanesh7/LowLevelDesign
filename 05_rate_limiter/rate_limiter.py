"""
================================================================================
 RATE LIMITER  --  Low Level Design
================================================================================

THE PROBLEM
-----------
Allow a user at most N requests per T seconds; reject the rest. Different user
tiers get different limits and may use different algorithms.

NOTE ON PROVENANCE
------------------
The upstream repo's Python file for this problem was EMPTY. This is a port of
the C++ version, with the algorithm bugs listed inline fixed.

REQUIREMENTS WE ARE DESIGNING FOR
---------------------------------
  R1. Per-user counting, not global.
  R2. Several algorithms, swappable at configuration time.
  R3. Per-tier configuration (free: 3/min, premium: 100/min).
  R4. Thread-safe -- a rate limiter that miscounts under load is worthless.

DESIGN PATTERNS USED  (jump to the numbered tags in the code below)
-------------------------------------------------------------------
  [1] STRATEGY -- RateLimiter: one class per algorithm, one interface.
  [2] FACTORY  -- RateLimiterFactory: build a limiter from a config enum.
  [3] FACADE   -- RateLimiterService: tier -> limiter routing for callers.

THE FOUR ALGORITHMS, IN ONE TABLE
---------------------------------
  Algorithm               Memory/user   Burst?   Boundary bug?   Exact?
  ----------------------  ------------  -------  --------------  ------
  Token Bucket            O(1)          yes      no              no
  Fixed Window            O(1)          no       YES (2x spike)  no
  Sliding Window Log      O(N requests) no       no              YES
  Sliding Window Counter  O(1)          no       no              approx

  Read that table top to bottom and you have the whole trade-off space:
  you are trading MEMORY for ACCURACY, and choosing whether bursts are a
  feature or a bug.

RUN IT
------
    python 05_rate_limiter/rate_limiter.py
================================================================================
"""

import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import Deque, Dict


# =============================================================================
#  SECTION 1 -- CONFIG AND USERS
# =============================================================================


class UserTier(Enum):
    FREE = auto()
    PREMIUM = auto()


class RateLimitType(Enum):
    TOKEN_BUCKET = auto()
    FIXED_WINDOW = auto()
    SLIDING_WINDOW_LOG = auto()
    SLIDING_WINDOW_COUNTER = auto()


@dataclass(frozen=True)
class RateLimitConfig:
    """
    "max_requests per window_seconds".

    frozen=True makes it immutable and hashable. Config that can be mutated
    after a limiter is built is a source of impossible-to-reproduce bugs --
    somebody's debug code sets max_requests=999 and it never gets reset.
    """

    max_requests: int
    window_seconds: float

    def __post_init__(self) -> None:
        if self.max_requests <= 0 or self.window_seconds <= 0:
            raise ValueError("max_requests and window_seconds must be positive")


@dataclass(frozen=True)
class User:
    user_id: str
    tier: UserTier


# =============================================================================
#  SECTION 2 -- [1] STRATEGY: the common interface
# =============================================================================


class RateLimiter(ABC):
    """
    Base class for every algorithm. One method: may this request through?

    ON THE CLOCK -- a fix worth understanding:
      The original used time(nullptr), i.e. WALL-CLOCK time. Wall clock can
      jump: NTP corrections, daylight saving, a sysadmin running `date`.
      A backwards jump makes `now - window_start` negative and every user gets
      a free reset; a forward jump expires everyone's budget at once.

      time.monotonic() only ever increases and is immune to all of that. Rule:
      wall clock for DISPLAYING a moment, monotonic clock for MEASURING a
      duration. This is a duration.

      Second fix: monotonic() returns a float with sub-millisecond resolution.
      The original's integer seconds meant a 100ms window was unrepresentable.
    """

    def __init__(self, config: RateLimitConfig, limiter_type: RateLimitType):
        self.config = config
        self.limiter_type = limiter_type
        self._lock = threading.Lock()

    @abstractmethod
    def allow_request(self, user_id: str) -> bool:
        """True if the request is permitted (and it has been counted)."""

    @staticmethod
    def now() -> float:
        return time.monotonic()


# =============================================================================
#  SECTION 3 -- ALGORITHM 1: TOKEN BUCKET
# =============================================================================


class TokenBucket(RateLimiter):
    """
    Every user has a bucket that holds `max_requests` tokens. A request costs
    one token. Tokens refill continuously at max_requests/window_seconds.

        capacity 3, window 60s  ->  refill rate 0.05 tokens/second

    WHY IT IS THE DEFAULT CHOICE:
      It ALLOWS BURSTS. A user who has been idle accumulates a full bucket and
      can fire all 3 requests at once, then is throttled to the steady rate.
      That matches how real clients behave -- a page load makes 5 API calls at
      once and then goes quiet -- and it is why AWS, Stripe and nginx all use
      a token bucket.

    TWO BUGS FIXED FROM THE ORIGINAL C++:

      BUG 1 -- refill was quantised to whole windows:
          refill = (elapsed / window) * maxRequests     // integer division!
        With window=60, `elapsed/60` is 0 for the first 59 seconds, so refill
        is 0. The bucket sat empty for a full minute and then jumped straight
        back to full. That is not a token bucket, that is a fixed window with
        extra steps. Real refill is CONTINUOUS: at t=30s of a 60s/3 bucket you
        should have 1.5 tokens back.

      BUG 2 -- the leftover time was thrown away:
          lastRefill[uid] = t;   // even when refill rounded down to 0
        Resetting the clock discards the fraction of a window that had already
        elapsed. Under steady traffic the remainder is discarded on every call
        and the user is permanently throttled below their configured rate --
        a slow drift that only shows up in production.

      THE FIX: keep tokens as a FLOAT and add rate * elapsed. No rounding, so
      nothing to lose.
    """

    def __init__(self, config: RateLimitConfig, limiter_type: RateLimitType):
        super().__init__(config, limiter_type)
        self._tokens: Dict[str, float] = {}
        self._last_refill: Dict[str, float] = {}
        # tokens added per second
        self._refill_rate = config.max_requests / config.window_seconds

    def _refill(self, user_id: str, now: float) -> float:
        """
        Bring the bucket up to date and return the token count.

        Pulled out into its own method so allow_request() and tokens_left()
        cannot disagree. Callers must already hold self._lock.
        """
        # First sighting: hand out a full bucket.
        if user_id not in self._tokens:
            self._tokens[user_id] = float(self.config.max_requests)
            self._last_refill[user_id] = now

        # Continuous refill, capped at capacity.
        elapsed = now - self._last_refill[user_id]
        self._tokens[user_id] = min(
            float(self.config.max_requests),
            self._tokens[user_id] + elapsed * self._refill_rate,
        )
        self._last_refill[user_id] = now
        return self._tokens[user_id]

    def allow_request(self, user_id: str) -> bool:
        with self._lock:
            tokens = self._refill(user_id, self.now())
            if tokens >= 1.0:
                self._tokens[user_id] = tokens - 1.0
                return True
            return False

    def tokens_left(self, user_id: str) -> float:
        """
        Peek at the current balance.

        It refills first. A tokens_left() that just read the stored number
        would report 0 for a bucket that has been quietly refilling for the
        last 30 seconds -- an observability method that lies about the thing
        it observes.
        """
        with self._lock:
            return self._refill(user_id, self.now())


# =============================================================================
#  SECTION 4 -- ALGORITHM 2: FIXED WINDOW
# =============================================================================


class FixedWindow(RateLimiter):
    """
    Chop time into fixed blocks. Count requests in the current block; reset the
    counter when a new block starts.

    Simplest thing that works. One integer per user.

    THE BOUNDARY PROBLEM -- know this, it is the standard follow-up question:
      Limit is 5 per minute. A user sends 5 requests at 11:59:59 and 5 more at
      12:00:01. Both bursts are legal -- they are in different windows -- but
      the user just made 10 requests in 2 SECONDS. Fixed window permits up to
      2x the intended rate across any boundary.

      Sliding Window Log (Section 5) removes this exactly.
      Sliding Window Counter (Section 6) removes it approximately, for O(1)
      memory. Sliding Window Counter exists precisely because of this bug.
    """

    def __init__(self, config: RateLimitConfig, limiter_type: RateLimitType):
        super().__init__(config, limiter_type)
        self._counts: Dict[str, int] = {}
        self._window_start: Dict[str, float] = {}

    def allow_request(self, user_id: str) -> bool:
        with self._lock:
            now = self.now()
            start = self._window_start.get(user_id)

            if start is None or (now - start) >= self.config.window_seconds:
                self._counts[user_id] = 0
                self._window_start[user_id] = now

            if self._counts[user_id] < self.config.max_requests:
                self._counts[user_id] += 1
                return True
            return False


# =============================================================================
#  SECTION 5 -- ALGORITHM 3: SLIDING WINDOW LOG
# =============================================================================


class SlidingWindowLog(RateLimiter):
    """
    Store the TIMESTAMP of every allowed request. On each new request, throw
    away everything older than the window and count what is left.

    PERFECTLY ACCURATE. "At most N in ANY window of T seconds" is exactly what
    it enforces -- no boundary artefact, no approximation.

    THE COST, AND IT IS THE WHOLE STORY:
      O(N) memory PER USER, where N is the limit. A limit of 10,000/hour on
      1 million users is 10 billion timestamps. That is not a rate limiter,
      that is a time-series database.

      Use it when N is small or accuracy is legally required (payments,
      auth attempts). Otherwise use the counter in Section 6.

    WHY deque AND NOT list:
      Expiry pops from the FRONT. list.pop(0) shifts every remaining element:
      O(n). collections.deque is a doubly-linked structure with O(1) popleft.
      With a 10k limit that is the difference between usable and not.
    """

    def __init__(self, config: RateLimitConfig, limiter_type: RateLimitType):
        super().__init__(config, limiter_type)
        self._logs: Dict[str, Deque[float]] = {}

    def allow_request(self, user_id: str) -> bool:
        with self._lock:
            now = self.now()
            timestamps = self._logs.setdefault(user_id, deque())

            # Evict everything that has slid out of the window.
            cutoff = now - self.config.window_seconds
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) < self.config.max_requests:
                timestamps.append(now)
                return True
            return False


# =============================================================================
#  SECTION 6 -- ALGORITHM 4: SLIDING WINDOW COUNTER
# =============================================================================


class SlidingWindowCounter(RateLimiter):
    """
    The practical compromise: fixed-window memory, near-sliding accuracy.

    Keep TWO counters -- this window and the previous one -- and estimate the
    true sliding count by weighting the previous window by how much of it is
    still inside the sliding view:

        elapsed = how far we are into the current window
        weight  = 1 - elapsed / window          # 1.0 at the start, 0.0 at the end
        estimate = previous_count * weight + current_count

    WORKED EXAMPLE (limit 100/min, we are 30s into the current minute):
        previous minute saw 80, current has 20 so far
        weight   = 1 - 30/60 = 0.5
        estimate = 80*0.5 + 20 = 60   ->  under 100, allow

    As the current window advances the previous one's contribution fades out
    smoothly, so there is no cliff at the boundary and no 2x spike.

    WHERE IT IS WRONG: it assumes the previous window's requests were spread
    EVENLY. If all 80 landed in the last second of that minute, the true
    sliding count is 100, not 60. In practice the error is a couple of percent
    and Cloudflare famously runs on this. You are buying 99% of the accuracy
    for 0.001% of the memory.

    FIXED from the original: the C++ truncated the weighted term to an int
    (`(int)(prevCount * weight)`), which systematically UNDER-counts and lets
    a trickle of extra requests through. Keep it as a float and compare floats.
    """

    def __init__(self, config: RateLimitConfig, limiter_type: RateLimitType):
        super().__init__(config, limiter_type)
        self._current: Dict[str, int] = {}
        self._previous: Dict[str, int] = {}
        self._window_start: Dict[str, float] = {}

    def allow_request(self, user_id: str) -> bool:
        with self._lock:
            now = self.now()
            window = self.config.window_seconds

            if user_id not in self._window_start:
                self._window_start[user_id] = now
                self._current[user_id] = 0
                self._previous[user_id] = 0

            elapsed = now - self._window_start[user_id]

            if elapsed >= window:
                # Roll forward. If more than a whole window has passed with no
                # traffic, the "previous" window is stale too -- zero it, or a
                # user who went quiet for an hour would still be paying for
                # requests they made an hour ago.
                if elapsed >= 2 * window:
                    self._previous[user_id] = 0
                else:
                    self._previous[user_id] = self._current[user_id]
                self._current[user_id] = 0
                self._window_start[user_id] = now
                elapsed = 0.0

            weight = 1.0 - (elapsed / window)
            estimate = self._previous[user_id] * weight + self._current[user_id]

            if estimate < self.config.max_requests:
                self._current[user_id] += 1
                return True
            return False


# =============================================================================
#  SECTION 7 -- [2] FACTORY
# =============================================================================


class RateLimiterFactory:
    """
    Turns a (type, config) pair into a limiter.

    The dict-based dispatch beats an if/elif ladder: registering a new
    algorithm is one entry, and there is no chance of forgetting a branch.
    """

    _REGISTRY = {
        RateLimitType.TOKEN_BUCKET: TokenBucket,
        RateLimitType.FIXED_WINDOW: FixedWindow,
        RateLimitType.SLIDING_WINDOW_LOG: SlidingWindowLog,
        RateLimitType.SLIDING_WINDOW_COUNTER: SlidingWindowCounter,
    }

    @classmethod
    def create(
        cls, limiter_type: RateLimitType, config: RateLimitConfig
    ) -> RateLimiter:
        try:
            limiter_class = cls._REGISTRY[limiter_type]
        except KeyError:  # pragma: no cover -- unreachable while the enum is closed
            raise ValueError(f"No limiter registered for {limiter_type}") from None
        return limiter_class(config, limiter_type)


# =============================================================================
#  SECTION 8 -- [3] FACADE: the service
# =============================================================================


class RateLimiterService:
    """
    What the API gateway calls. It maps a user's TIER to a limiter and asks it.

    Callers never see which algorithm is in play -- that is the point. You can
    move the free tier from fixed-window to token-bucket by editing one line of
    configuration, with no change to any calling code.
    """

    def __init__(self, verbose: bool = True):
        self._limiters: Dict[UserTier, RateLimiter] = {}
        self._verbose = verbose

    def register(self, tier: UserTier, limiter: RateLimiter) -> "RateLimiterService":
        self._limiters[tier] = limiter
        return self

    def allow_request(self, user: User) -> bool:
        limiter = self._limiters.get(user.tier)
        if limiter is None:
            # FAIL OPEN: no rule configured -> let it through.
            #
            # This is a POLICY DECISION and you should say which way you went
            # and why. Fail-open keeps the product working when the limiter
            # config is missing; fail-closed protects the backend but can take
            # the whole site down over a config typo. For rate limiting,
            # fail-open is the usual call -- for AUTHENTICATION it is the
            # opposite, and choosing wrong there is a security incident.
            return True

        allowed = limiter.allow_request(user.user_id)
        if self._verbose:
            verdict = "ALLOW" if allowed else "BLOCK"
            print(f"  [{verdict}] {user.user_id} ({user.tier.name})")
        return allowed


# =============================================================================
#  SECTION 9 -- RUNNABLE DEMO
# =============================================================================


def _demo() -> None:
    print("=" * 70)
    print("SCENARIO 1: free tier = 3 per 60s (token bucket)")
    print("=" * 70)
    service = RateLimiterService()
    service.register(
        UserTier.FREE,
        RateLimiterFactory.create(
            RateLimitType.TOKEN_BUCKET, RateLimitConfig(3, 60)
        ),
    ).register(
        UserTier.PREMIUM,
        RateLimiterFactory.create(
            RateLimitType.SLIDING_WINDOW_LOG, RateLimitConfig(10, 60)
        ),
    )

    free_user = User("alice", UserTier.FREE)
    for _ in range(5):
        service.allow_request(free_user)

    print()
    print("  premium user, same 5 requests:")
    premium_user = User("bob", UserTier.PREMIUM)
    for _ in range(5):
        service.allow_request(premium_user)

    print()
    print("=" * 70)
    print("SCENARIO 2: token bucket REFILLS continuously (the fixed bug)")
    print("=" * 70)
    # 4 tokens per 1 second => 4 tokens/sec. Drain it, wait, watch it come back.
    bucket = TokenBucket(RateLimitConfig(4, 1.0), RateLimitType.TOKEN_BUCKET)
    drained = sum(1 for _ in range(4) if bucket.allow_request("carol"))
    print(f"  burst of 4 on an empty-of-nothing bucket: {drained} allowed")
    print(f"  immediately after, allowed? {bucket.allow_request('carol')}")
    print(f"  tokens left: {bucket.tokens_left('carol'):.2f}")
    time.sleep(0.5)  # half a window -> 2 tokens back
    print(f"  after 0.5s (half a window): {bucket.tokens_left('carol'):.2f} tokens")
    print(f"  allowed now? {bucket.allow_request('carol')}")
    print("  ^ the original code would still be at 0 here -- it only refilled")
    print("    in whole-window jumps.")

    print()
    print("=" * 70)
    print("SCENARIO 3: the FIXED WINDOW boundary bug, demonstrated")
    print("=" * 70)
    fixed = FixedWindow(RateLimitConfig(3, 0.4), RateLimitType.FIXED_WINDOW)
    allowed_late = sum(1 for _ in range(3) if fixed.allow_request("dave"))
    time.sleep(0.45)  # cross the boundary
    allowed_early = sum(1 for _ in range(3) if fixed.allow_request("dave"))
    print(f"  end of window 1: {allowed_late} allowed")
    print(f"  start of window 2: {allowed_early} allowed")
    print(f"  -> {allowed_late + allowed_early} requests in ~0.45s, "
          f"with a limit of 3 per 0.4s")

    print()
    print("=" * 70)
    print("SCENARIO 4: sliding window log has NO boundary bug")
    print("=" * 70)
    sliding = SlidingWindowLog(RateLimitConfig(3, 0.4), RateLimitType.SLIDING_WINDOW_LOG)
    allowed_late = sum(1 for _ in range(3) if sliding.allow_request("erin"))
    time.sleep(0.25)  # NOT a full window
    allowed_early = sum(1 for _ in range(3) if sliding.allow_request("erin"))
    print(f"  first burst: {allowed_late} allowed")
    print(f"  0.25s later: {allowed_early} allowed (still inside the window)")
    time.sleep(0.2)  # now the first burst has aged out
    print(f"  0.45s after the first burst: "
          f"{sum(1 for _ in range(3) if sliding.allow_request('erin'))} allowed")

    print()
    print("=" * 70)
    print("SCENARIO 5: all four algorithms, same config, same traffic")
    print("=" * 70)
    config = RateLimitConfig(5, 60)
    for limiter_type in RateLimitType:
        limiter = RateLimiterFactory.create(limiter_type, config)
        verdicts = "".join(
            "." if limiter.allow_request("frank") else "x" for _ in range(8)
        )
        print(f"  {limiter_type.name:<24} {verdicts}   (. = allowed, x = blocked)")

    print()
    print("=" * 70)
    print("SCENARIO 6: thread safety -- 20 threads, limit 50, 5 requests each")
    print("=" * 70)
    limiter = TokenBucket(RateLimitConfig(50, 60), RateLimitType.TOKEN_BUCKET)
    results = []
    results_lock = threading.Lock()

    def hammer() -> None:
        local = [limiter.allow_request("shared-user") for _ in range(5)]
        with results_lock:
            results.extend(local)

    threads = [threading.Thread(target=hammer) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    print(f"  100 requests attempted, {sum(results)} allowed (expected exactly 50)")
    print("  ^ without the lock this number would drift above 50 under load.")


if __name__ == "__main__":
    _demo()
