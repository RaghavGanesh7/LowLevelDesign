"""
================================================================================
 AMAZON LOCKER  --  Low Level Design
================================================================================

THE PROBLEM
-----------
Design the pickup-locker system. A courier drops a package into a locker; the
customer gets a one-time code and later opens that locker to collect it.

NOTE ON PROVENANCE
------------------
The upstream repo's Python file for this problem was EMPTY. This is a faithful
port of the C++ version that was there, plus the fixes called out inline.

REQUIREMENTS WE ARE DESIGNING FOR
---------------------------------
  R1. Lockers come in sizes. A package must go in a locker it fits in.
  R2. Booking generates a one-time code (OTP) and notifies the customer.
  R3. Pickup requires the right code. Wrong codes must not open anything.
  R4. Uncollected packages expire so the locker is not blocked forever.
  R5. Notification channel (email / SMS / push) must be swappable.

DESIGN PATTERNS USED  (jump to the numbered tags in the code below)
-------------------------------------------------------------------
  [1] STRATEGY  -- NotificationService: email vs SMS vs "log it" for tests.
  [2] STRATEGY  -- OTPGenerator: random in production, fixed in the demo.
  [3] FACADE    -- LockerController: one class the outside world talks to.

THE ONE BIG IDEA
----------------
"Find the SMALLEST locker the package fits in", not "find a locker of exactly
this size". Exact-match wastes capacity: a small package is refused while three
large lockers stand empty. Best-fit allocation is the interesting part of this
problem and it is the part the original got wrong.

RUN IT
------
    python 03_amazon_locker/amazon_locker.py
================================================================================
"""

import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional


# =============================================================================
#  SECTION 1 -- SIZES: why IntEnum and not Enum
# =============================================================================


class LockerSize(IntEnum):
    """
    Locker/package sizes.

    IntEnum, NOT Enum, and that choice is the whole design.

    An IntEnum member compares with <, <=, > like a number. That single fact
    turns "does this package fit?" into `package.size <= locker.size` and
    "which locker is the tightest fit?" into a plain `min()`. With a regular
    Enum you would need a separate size-ordering table and every comparison
    would be a dictionary lookup.

    LESSON: when your enum has a natural ORDER, encode the order in the type.
    """

    SMALL = 1
    MEDIUM = 2
    LARGE = 3


class LockerStatus(IntEnum):
    AVAILABLE = 0
    OCCUPIED = 1


# =============================================================================
#  SECTION 2 -- DOMAIN OBJECTS
# =============================================================================


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    phone: str = ""


@dataclass
class Package:
    package_id: str
    size: LockerSize


@dataclass
class Locker:
    locker_id: str
    size: LockerSize
    status: LockerStatus = LockerStatus.AVAILABLE

    def is_available(self) -> bool:
        return self.status is LockerStatus.AVAILABLE

    def occupy(self) -> None:
        self.status = LockerStatus.OCCUPIED

    def release(self) -> None:
        # Named release(), not free(). `free` is a builtin-ish word in C and a
        # confusing verb in Python where it collides with "is it free?".
        self.status = LockerStatus.AVAILABLE

    def fits(self, package: Package) -> bool:
        """A package fits any locker at least as big as itself."""
        return package.size <= self.size


@dataclass
class Booking:
    booking_id: str
    otp: str
    locker: Locker
    package: Package
    customer: Customer
    created_at: float = field(default_factory=time.time)
    failed_attempts: int = 0

    def is_expired(self, ttl_seconds: float) -> bool:
        """
        R4: uncollected packages expire.

        ADDED (missing from the original): without expiry a customer who never
        shows up blocks that locker forever, and a fleet of lockers slowly
        grinds to zero capacity. Any design with a limited resource needs an
        answer to "what if the holder never returns?".
        """
        return (time.time() - self.created_at) > ttl_seconds


# =============================================================================
#  SECTION 3 -- [2] STRATEGY: OTP generation and checking
# =============================================================================


class OTPGenerator(ABC):
    @abstractmethod
    def generate(self) -> str: ...


class RandomOTPGenerator(OTPGenerator):
    """
    Production generator.

    FIXED: the original returned the literal string "1234" for every booking.
    That is not an OTP, it is a shared password -- anyone could open anyone
    else's locker on the first guess.

    `secrets`, not `random`: random.* is a Mersenne Twister seeded from the
    clock and is predictable from a handful of outputs. `secrets` draws from
    the OS CSPRNG. Rule: anything a stranger should not be able to guess uses
    `secrets`.
    """

    def __init__(self, digits: int = 6):
        self._digits = digits

    def generate(self) -> str:
        upper_bound = 10 ** self._digits
        return str(secrets.randbelow(upper_bound)).zfill(self._digits)


class FixedOTPGenerator(OTPGenerator):
    """Test double so the demo below has a predictable code to type."""

    def __init__(self, otp: str = "123456"):
        self._otp = otp

    def generate(self) -> str:
        return self._otp


class OTPVerifier:
    """Compares a submitted OTP against the stored one."""

    @staticmethod
    def verify(expected: str, submitted: str) -> bool:
        # secrets.compare_digest, not ==.
        #
        # `==` on strings returns as soon as it finds a differing character, so
        # how LONG it takes leaks how many leading characters were right. An
        # attacker can time a few thousand requests and recover the code digit
        # by digit -- a "timing attack". compare_digest always looks at every
        # byte, so the timing tells you nothing.
        return secrets.compare_digest(expected, submitted)


# =============================================================================
#  SECTION 4 -- [1] STRATEGY: notifications
# =============================================================================
# The controller must not care HOW the customer is told. It calls send(); the
# strategy decides whether that is an email, an SMS, or a line in a log file.


class NotificationService(ABC):
    @abstractmethod
    def send_otp(self, customer: Customer, otp: str, locker_id: str) -> None: ...


class EmailNotification(NotificationService):
    def send_otp(self, customer: Customer, otp: str, locker_id: str) -> None:
        print(f"  [EMAIL -> {customer.email}] Locker {locker_id}, code {otp}")


class SMSNotification(NotificationService):
    def send_otp(self, customer: Customer, otp: str, locker_id: str) -> None:
        print(f"  [SMS -> {customer.phone}] Locker {locker_id}, code {otp}")


class MultiChannelNotification(NotificationService):
    """
    COMPOSITE: a notifier made of notifiers. It implements the same interface
    it holds, so the controller cannot tell the difference between "one
    channel" and "all of them" -- and does not need to.
    """

    def __init__(self, *channels: NotificationService):
        self._channels = channels

    def send_otp(self, customer: Customer, otp: str, locker_id: str) -> None:
        for channel in self._channels:
            channel.send_otp(customer, otp, locker_id)


# =============================================================================
#  SECTION 5 -- ALLOCATION: the interesting part
# =============================================================================


class LockerStation:
    """One physical bank of lockers at one location."""

    def __init__(self, station_id: str):
        self.station_id = station_id
        self._lockers: List[Locker] = []

    def add_locker(self, locker: Locker) -> None:
        self._lockers.append(locker)

    def find_locker_for(self, package: Package) -> Optional[Locker]:
        """
        BEST FIT: the smallest available locker the package fits in.

        FIXED -- this is the headline bug in the original:
            if locker.is_available() and locker.size == package.size

        Exact-match means a SMALL package is refused while LARGE lockers sit
        empty. Worse, it is silently wasteful rather than loudly broken, so it
        survives code review.

        Best-fit instead:
          - filter to lockers that FIT (>= package size), not that MATCH
          - among those, take the SMALLEST, so big lockers stay free for big
            packages

        Why smallest and not first? Handing a LARGE locker to a SMALL package
        when a SMALL one is free means the next large package gets refused.
        Greedy best-fit avoids that.

        Cost: O(n) over the lockers. For a real station with thousands of
        lockers you would keep one heap or free-list per size and pop from the
        smallest non-empty one -- O(log n). The shape of the answer is the
        same; only the lookup changes.
        """
        candidates = [
            locker
            for locker in self._lockers
            if locker.is_available() and locker.fits(package)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda locker: locker.size)

    def report(self) -> str:
        free = sum(1 for locker in self._lockers if locker.is_available())
        return f"Station({self.station_id}: {free}/{len(self._lockers)} free)"


# =============================================================================
#  SECTION 6 -- [3] FACADE: the controller
# =============================================================================


class LockerController:
    """
    The one class the courier app and the customer app talk to.

    A Facade hides a subsystem behind a small, task-shaped API. Callers say
    "book this package" and "let me pick it up"; they never touch lockers,
    OTPs or notifiers directly. That is what lets you swap any of those out.
    """

    #: how long a package may sit before the booking is reclaimed
    DEFAULT_TTL_SECONDS = 3 * 24 * 3600  # 3 days
    #: wrong-OTP attempts before the booking is locked out
    MAX_OTP_ATTEMPTS = 3

    def __init__(
        self,
        station: LockerStation,
        notifier: NotificationService,
        otp_generator: Optional[OTPGenerator] = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ):
        # Every collaborator is injected. Nothing here is constructed with
        # `new`, so every one of them can be replaced in a test.
        self._station = station
        self._notifier = notifier
        self._otp_generator = otp_generator or RandomOTPGenerator()
        self._ttl_seconds = ttl_seconds
        self._bookings: Dict[str, Booking] = {}

    def book_locker(
        self, booking_id: str, package: Package, customer: Customer
    ) -> Optional[Booking]:
        """Courier drops a package off. Returns the Booking, or None if full."""
        # ADDED: reject a duplicate id instead of silently overwriting it. The
        # original clobbered the old booking, orphaning an occupied locker that
        # nothing could ever release.
        if booking_id in self._bookings:
            print(f"  [CTRL] booking id {booking_id} already in use")
            return None

        locker = self._station.find_locker_for(package)
        if locker is None:
            print(f"  [CTRL] no locker fits package {package.package_id} "
                  f"({package.size.name})")
            return None

        locker.occupy()
        otp = self._otp_generator.generate()
        booking = Booking(booking_id, otp, locker, package, customer)
        self._bookings[booking_id] = booking

        print(f"  [CTRL] {package.package_id} ({package.size.name}) -> "
              f"locker {locker.locker_id} ({locker.size.name})")
        self._notifier.send_otp(customer, otp, locker.locker_id)
        return booking

    def pickup(self, booking_id: str, submitted_otp: str) -> bool:
        """Customer collects. Returns True if the locker opened."""
        booking = self._bookings.get(booking_id)
        if booking is None:
            print("  [CTRL] booking not found")
            return False

        if booking.is_expired(self._ttl_seconds):
            print("  [CTRL] booking expired -- package returned to sender")
            self._release(booking_id)
            return False

        if booking.failed_attempts >= self.MAX_OTP_ATTEMPTS:
            print("  [CTRL] too many wrong codes -- booking locked, contact support")
            return False

        if not OTPVerifier.verify(booking.otp, submitted_otp):
            # ADDED: the original let you guess forever. With no attempt limit
            # even a 6-digit OTP falls to a script in minutes.
            booking.failed_attempts += 1
            remaining = self.MAX_OTP_ATTEMPTS - booking.failed_attempts
            print(f"  [CTRL] invalid code -- {remaining} attempt(s) left")
            return False

        print(f"  [CTRL] locker {booking.locker.locker_id} opened. "
              f"{booking.customer.name} collected {booking.package.package_id}")
        self._release(booking_id)
        return True

    def reclaim_expired(self) -> int:
        """
        Housekeeping sweep -- a cron job would call this.

        ADDED: expiry is useless without something that acts on it. A rule
        nobody enforces is a comment, not a rule.
        """
        expired = [
            booking_id
            for booking_id, booking in self._bookings.items()
            if booking.is_expired(self._ttl_seconds)
        ]
        for booking_id in expired:
            print(f"  [CTRL] reclaiming expired booking {booking_id}")
            self._release(booking_id)
        return len(expired)

    def _release(self, booking_id: str) -> None:
        """Free the locker and retire the booking. One place, so no leaks."""
        booking = self._bookings.pop(booking_id)
        booking.locker.release()


# =============================================================================
#  SECTION 7 -- RUNNABLE DEMO
# =============================================================================


def _demo() -> None:
    station = LockerStation("BLR-01")
    station.add_locker(Locker("L1", LockerSize.SMALL))
    station.add_locker(Locker("L2", LockerSize.MEDIUM))
    station.add_locker(Locker("L3", LockerSize.LARGE))

    controller = LockerController(
        station=station,
        notifier=MultiChannelNotification(EmailNotification(), SMSNotification()),
        otp_generator=FixedOTPGenerator("123456"),  # predictable for the demo
    )

    alice = Customer("C1", "Alice", "alice@example.com", "+91-99999-11111")
    bob = Customer("C2", "Bob", "bob@example.com", "+91-99999-22222")

    print("=" * 70)
    print("SCENARIO 1: best-fit allocation")
    print("=" * 70)
    print(f"  {station.report()}")
    controller.book_locker("B1", Package("P1", LockerSize.SMALL), alice)
    print("  ^ SMALL package took the SMALL locker, not the first one it fits")
    print(f"  {station.report()}")

    print()
    print("=" * 70)
    print("SCENARIO 2: no exact size free -> falls UP to a bigger locker")
    print("=" * 70)
    controller.book_locker("B2", Package("P2", LockerSize.SMALL), bob)
    print("  ^ SMALL lockers are gone, so it used the MEDIUM one.")
    print("    The original code would have refused this package outright.")
    print(f"  {station.report()}")

    print()
    print("=" * 70)
    print("SCENARIO 3: wrong code, then the right one")
    print("=" * 70)
    controller.pickup("B1", "000000")
    controller.pickup("B1", "123456")
    print(f"  {station.report()}")

    print()
    print("=" * 70)
    print("SCENARIO 4: attempt limit stops brute force")
    print("=" * 70)
    for guess in ("111111", "222222", "333333", "123456"):
        print(f"  trying {guess}:")
        controller.pickup("B2", guess)
    print("  ^ the LAST guess was the correct code, and it was still refused --")
    print("    the booking was already locked out.")

    print()
    print("=" * 70)
    print("SCENARIO 5: a package too big for anything free")
    print("=" * 70)
    controller.book_locker("B3", Package("P3", LockerSize.LARGE), alice)
    controller.book_locker("B4", Package("P4", LockerSize.LARGE), bob)
    print(f"  {station.report()}")

    print()
    print("=" * 70)
    print("SCENARIO 6: expiry reclaims an abandoned locker")
    print("=" * 70)
    short_lived = LockerController(
        station=LockerStation("TMP"),
        notifier=EmailNotification(),
        otp_generator=FixedOTPGenerator("999999"),
        ttl_seconds=0,  # everything is expired the moment it is created
    )
    short_lived._station.add_locker(Locker("X1", LockerSize.SMALL))
    short_lived.book_locker("B9", Package("P9", LockerSize.SMALL), alice)
    print(f"  before sweep: {short_lived._station.report()}")
    short_lived.reclaim_expired()
    print(f"  after sweep : {short_lived._station.report()}")


if __name__ == "__main__":
    _demo()
