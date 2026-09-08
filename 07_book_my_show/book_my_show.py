"""
================================================================================
 BOOKMYSHOW  --  Low Level Design
================================================================================

THE PROBLEM
-----------
Design movie ticket booking. A user picks seats for a show, holds them while
they pay, and either confirms or loses the hold.

REQUIREMENTS WE ARE DESIGNING FOR
---------------------------------
  R1. Theatres have screens; screens have seats; shows run on a screen.
  R2. TWO USERS MUST NEVER GET THE SAME SEAT. This is the entire problem.
  R3. A user who starts booking and wanders off must not hold seats forever.
  R4. Payment failure releases the seats immediately.
  R5. Multi-seat bookings are all-or-nothing.

DESIGN PATTERNS USED  (jump to the numbered tags in the code below)
-------------------------------------------------------------------
  [1] STRATEGY  -- LockProvider: in-memory now, Redis in production.
  [2] FACADE    -- BookingService: the API the app calls.
  [3] STATE     -- BookingStatus: an explicit booking lifecycle.

THE ONE BIG IDEA -- THE TWO-PHASE SEAT HOLD
-------------------------------------------
Seat selection and payment are separated by 30-300 seconds of a human typing
card details. You cannot hold a database transaction open that long, and you
cannot leave the seat free or two people will pay for it.

The answer is a SOFT LOCK WITH A TTL:

  select seats -> acquire lock (TTL 5 min) -> user pays -> confirm
                        |                                     |
                   lock expires                          seat becomes
                   -> seat free again                     PERMANENTLY booked

The lock is temporary; the booking is permanent. Confusing those two is the
bug the original code had -- see the big comment in Section 5.

RUN IT
------
    python 07_book_my_show/book_my_show.py
================================================================================
"""

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional


# =============================================================================
#  SECTION 1 -- [3] EXPLICIT STATES
# =============================================================================
# FIXED: the original used bare strings -- "pending", "paid", "available".
# A typo like "conirmed" then silently means "not confirmed" and no tool can
# catch it. Enums make the set of legal values closed and checkable.


class SeatStatus(Enum):
    AVAILABLE = auto()  # anyone may lock it
    BOOKED = auto()     # permanently sold; no lock can ever be taken again


class BookingStatus(Enum):
    PENDING = auto()    # seats held, payment not yet done
    CONFIRMED = auto()  # paid; seats are BOOKED
    CANCELLED = auto()  # payment failed or user cancelled; locks released


class PaymentStatus(Enum):
    UNPAID = auto()
    PAID = auto()
    FAILED = auto()


# =============================================================================
#  SECTION 2 -- DOMAIN OBJECTS
# =============================================================================


@dataclass
class Movie:
    movie_id: str
    title: str
    duration_minutes: int


@dataclass
class Seat:
    """
    One seat in one screen.

    NOTE: `status` is about being SOLD, not about being held. Holds live in the
    LockProvider. Keeping the two apart is what makes the lifecycle honest:
      - lock present, status AVAILABLE  -> someone is paying right now
      - status BOOKED                   -> sold, forever
    """

    seat_id: str
    price: float
    status: SeatStatus = SeatStatus.AVAILABLE


@dataclass
class Screen:
    screen_id: str
    seats: Dict[str, Seat] = field(default_factory=dict)


@dataclass
class Theatre:
    theatre_id: str
    name: str
    screens: Dict[str, Screen] = field(default_factory=dict)


@dataclass
class Show:
    """
    A (movie, screen, time) triple.

    IMPORTANT: seat availability is per SHOW, not per SCREEN. Seat A1 can be
    sold for the 6pm show and free for the 9pm show. That is why every lock key
    below is built from show_id AND seat_id, and why `booked_seats` lives on
    the Show rather than on the Seat.

    The `Seat.status` field is therefore the per-show view -- in a real system
    each Show would hold its own seat instances (or a ShowSeat join table).
    Kept as one set on the Show here so the relationship is visible in one
    place.
    """

    show_id: str
    movie: Movie
    theatre: Theatre
    screen: Screen
    start_time: str
    end_time: str
    booked_seats: set = field(default_factory=set)  # seat_ids sold for THIS show


@dataclass
class Booking:
    booking_id: str
    user_id: str
    show: Show
    seats: List[Seat]
    amount: float
    booking_status: BookingStatus = BookingStatus.PENDING
    payment_status: PaymentStatus = PaymentStatus.UNPAID
    created_at: float = field(default_factory=time.time)


# =============================================================================
#  SECTION 3 -- [1] STRATEGY: the lock provider
# =============================================================================


class LockProvider(ABC):
    """
    A distributed-lock interface.

    WHY THIS IS AN INTERFACE AND NOT JUST A DICT:
      In a single process a dict plus a mutex is enough. In production there
      are 20 booking servers behind a load balancer, and a lock in one
      server's memory means nothing to the other 19. You need a lock that
      lives OUTSIDE the process -- Redis SET NX PX, or a DB row with a
      uniqueness constraint.

      Putting that behind an interface now means the swap is one line later.
      The in-memory implementation is not a toy -- it is the test double.
    """

    @abstractmethod
    def try_lock(self, key: str, owner_id: str, ttl_seconds: float) -> bool:
        """Atomically take the lock if free. False if someone else holds it."""

    @abstractmethod
    def unlock(self, key: str, owner_id: str) -> bool:
        """Release, but ONLY if `owner_id` is the current holder."""

    @abstractmethod
    def is_locked(self, key: str) -> bool: ...


class InMemoryLockProvider(LockProvider):
    """Single-process implementation. Correct here; not distributed."""

    @dataclass
    class _Entry:
        owner_id: str
        expires_at: float

    def __init__(self) -> None:
        self._store: Dict[str, "InMemoryLockProvider._Entry"] = {}
        self._mutex = threading.Lock()

    def try_lock(self, key: str, owner_id: str, ttl_seconds: float) -> bool:
        with self._mutex:
            entry = self._store.get(key)
            # A lock past its expiry is not a lock. Treating it as free here is
            # what makes abandoned bookings self-heal with no cleanup job.
            if entry is not None and entry.expires_at > time.monotonic():
                return False
            self._store[key] = self._Entry(owner_id, time.monotonic() + ttl_seconds)
            return True

    def unlock(self, key: str, owner_id: str) -> bool:
        """
        FIXED: the original's unlock took no owner and deleted whatever was
        there. That is a real distributed-systems footgun:

          1. User A locks seat A1 with a 5-minute TTL.
          2. A's request stalls for 6 minutes. The lock expires.
          3. User B locks A1 and starts paying.
          4. A's request finally resumes and calls unlock("A1").
          5. A has just released B's lock. Now a third user can take the seat
             that B is actively paying for.

        Checking the owner before deleting closes that window. This is the same
        reason Redis's own Redlock docs insist you delete with a Lua
        compare-and-delete rather than a plain DEL.
        """
        with self._mutex:
            entry = self._store.get(key)
            if entry is None or entry.owner_id != owner_id:
                return False
            del self._store[key]
            return True

    def is_locked(self, key: str) -> bool:
        with self._mutex:
            entry = self._store.get(key)
            return entry is not None and entry.expires_at > time.monotonic()


# =============================================================================
#  SECTION 4 -- PAYMENT (a stub, behind an interface)
# =============================================================================


class PaymentGateway(ABC):
    @abstractmethod
    def charge(self, booking_id: str, amount: float) -> bool: ...


class AlwaysSucceedsGateway(PaymentGateway):
    def charge(self, booking_id: str, amount: float) -> bool:
        print(f"    [pay] charged {amount:.2f} for {booking_id}")
        return True


class AlwaysFailsGateway(PaymentGateway):
    def charge(self, booking_id: str, amount: float) -> bool:
        print(f"    [pay] DECLINED for {booking_id}")
        return False


# =============================================================================
#  SECTION 5 -- [2] FACADE: the booking service
# =============================================================================


class BookingService:
    """
    The heart of the design. Read create_booking and confirm_booking together.
    """

    DEFAULT_LOCK_TTL = 300.0  # 5 minutes to finish paying

    def __init__(
        self,
        lock_provider: LockProvider,
        payment_gateway: Optional[PaymentGateway] = None,
        lock_ttl_seconds: float = DEFAULT_LOCK_TTL,
    ):
        self._locks = lock_provider
        self._gateway = payment_gateway or AlwaysSucceedsGateway()
        self._ttl = lock_ttl_seconds
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._bookings: Dict[str, Booking] = {}

    @staticmethod
    def _lock_key(show_id: str, seat_id: str) -> str:
        # show_id is in the key because availability is per show (see Show).
        return f"show:{show_id}:seat:{seat_id}"

    def _next_booking_id(self) -> str:
        with self._counter_lock:
            self._counter += 1
            return f"BK{self._counter}"

    # -- phase 1: hold the seats ---------------------------------------------

    def create_booking(self, user_id: str, show: Show, seats: List[Seat]) -> Booking:
        """
        Take a temporary hold on every requested seat, then return a PENDING
        booking. Raises if any seat is unavailable.

        ALL-OR-NOTHING (R5): if seat 3 of 4 cannot be locked, the three already
        taken are released before we raise. Without that rollback a failed
        booking would strand seats for the full TTL -- and a user retrying in a
        loop would sterilise the whole screen.

        WHY WE CHECK `status is BOOKED` FIRST -- this is the bug fix:
          The original relied on the LOCK alone to keep a sold seat sold. But
          locks expire (that is their entire purpose). So:

            t=0    user A locks A1, pays, booking confirmed. Lock still held.
            t=300  the lock's TTL runs out. Nothing renews it.
            t=301  user B calls create_booking for A1. try_lock succeeds,
                   because the lock is gone. B pays. B gets a seat that A
                   already owns.

          DOUBLE BOOKING, roughly five minutes after every successful sale.
          The lock is a TEMPORARY hold; the sale is PERMANENT. Permanent facts
          belong in durable state (`Seat.status` / `Show.booked_seats`), never
          in something with a TTL.
        """
        if not seats:
            raise ValueError("no seats selected")

        acquired: List[str] = []
        try:
            for seat in seats:
                # Permanent check first -- cheap, and no lock can override it.
                if seat.seat_id in show.booked_seats:
                    raise SeatUnavailableError(
                        f"seat {seat.seat_id} is already sold for this show"
                    )

                key = self._lock_key(show.show_id, seat.seat_id)
                if not self._locks.try_lock(key, user_id, self._ttl):
                    raise SeatUnavailableError(
                        f"seat {seat.seat_id} is being booked by someone else"
                    )
                acquired.append(key)
        except SeatUnavailableError:
            # Roll back the partial hold before propagating.
            for key in acquired:
                self._locks.unlock(key, user_id)
            raise

        booking = Booking(
            booking_id=self._next_booking_id(),
            user_id=user_id,
            show=show,
            seats=list(seats),
            amount=sum(seat.price for seat in seats),
        )
        self._bookings[booking.booking_id] = booking
        return booking

    # -- phase 2: pay and commit ---------------------------------------------

    def confirm_booking(self, booking: Booking) -> bool:
        """
        Charge the card. On success the seats become permanently BOOKED; on
        failure the holds are released at once (R4).
        """
        if booking.booking_status is not BookingStatus.PENDING:
            raise ValueError(
                f"{booking.booking_id} is {booking.booking_status.name}, "
                f"not PENDING"
            )

        # A hold that expired while the user dithered means the seats may
        # already belong to somebody else. Fail before taking their money.
        for seat in booking.seats:
            key = self._lock_key(booking.show.show_id, seat.seat_id)
            if not self._locks.is_locked(key):
                booking.booking_status = BookingStatus.CANCELLED
                print(f"    [book] hold expired on {seat.seat_id} -- "
                      f"booking cancelled before charging")
                return False

        if not self._gateway.charge(booking.booking_id, booking.amount):
            booking.payment_status = PaymentStatus.FAILED
            booking.booking_status = BookingStatus.CANCELLED
            self._release_locks(booking)
            return False

        # COMMIT. Write the permanent fact BEFORE releasing the hold, so there
        # is no instant where the seat is neither held nor sold.
        for seat in booking.seats:
            seat.status = SeatStatus.BOOKED
            booking.show.booked_seats.add(seat.seat_id)

        booking.payment_status = PaymentStatus.PAID
        booking.booking_status = BookingStatus.CONFIRMED

        # Now the hold is redundant -- the permanent record is what protects
        # the seat. Releasing it keeps the lock store from growing forever.
        self._release_locks(booking)
        return True

    def cancel_booking(self, booking: Booking) -> bool:
        """
        Cancel a PENDING booking (user pressed back).

        ADDED: the original had no way to abandon a booking, so seats stayed
        held for the whole TTL even when the user explicitly gave up.

        Note this deliberately refuses to cancel a CONFIRMED booking -- undoing
        a sale means refunds and is a different workflow with different rules.
        """
        if booking.booking_status is not BookingStatus.PENDING:
            return False
        booking.booking_status = BookingStatus.CANCELLED
        self._release_locks(booking)
        return True

    def _release_locks(self, booking: Booking) -> None:
        for seat in booking.seats:
            self._locks.unlock(
                self._lock_key(booking.show.show_id, seat.seat_id), booking.user_id
            )

    def available_seats(self, show: Show) -> List[str]:
        return sorted(
            seat_id
            for seat_id in show.screen.seats
            if seat_id not in show.booked_seats
            and not self._locks.is_locked(self._lock_key(show.show_id, seat_id))
        )


class SeatUnavailableError(RuntimeError):
    """
    Raised when a requested seat cannot be held.

    A named exception type, not a bare RuntimeError: callers can catch exactly
    this and show "those seats just went" without also swallowing genuine bugs.
    """


# =============================================================================
#  SECTION 6 -- RUNNABLE DEMO
# =============================================================================


def _build_show(seat_count: int = 6) -> Show:
    seats = {
        f"A{index}": Seat(f"A{index}", price=200.0 + 10 * index)
        for index in range(1, seat_count + 1)
    }
    screen = Screen("SC1", seats)
    theatre = Theatre("T1", "PVR Forum", {"SC1": screen})
    return Show(
        show_id="SH1",
        movie=Movie("M1", "Inception", 148),
        theatre=theatre,
        screen=screen,
        start_time="2026-09-10T18:00",
        end_time="2026-09-10T20:28",
    )


def _demo() -> None:
    print("=" * 70)
    print("SCENARIO 1: happy path -- hold, pay, confirm")
    print("=" * 70)
    show = _build_show()
    service = BookingService(InMemoryLockProvider())
    booking = service.create_booking("alice", show, [show.screen.seats["A1"],
                                                     show.screen.seats["A2"]])
    print(f"  {booking.booking_id}: {booking.booking_status.name}, "
          f"amount {booking.amount:.2f}")
    service.confirm_booking(booking)
    print(f"  {booking.booking_id}: {booking.booking_status.name} / "
          f"{booking.payment_status.name}")
    print(f"  still available: {service.available_seats(show)}")

    print()
    print("=" * 70)
    print("SCENARIO 2: THE DOUBLE-BOOKING BUG -- the headline fix")
    print("=" * 70)
    show = _build_show()
    # 0.2s TTL so we can watch a lock expire without waiting 5 minutes.
    service = BookingService(InMemoryLockProvider(), lock_ttl_seconds=0.2)
    first = service.create_booking("alice", show, [show.screen.seats["A1"]])
    service.confirm_booking(first)
    print(f"  Alice CONFIRMED A1 (booking {first.booking_id})")

    time.sleep(0.3)  # the hold's TTL elapses
    print("  ...0.3s later, the lock's TTL has expired...")
    try:
        service.create_booking("bob", show, [show.screen.seats["A1"]])
        print("  Bob ALSO booked A1  <-- this is the original code's behaviour")
    except SeatUnavailableError as error:
        print(f"  Bob refused: {error}")
        print("  ^ because the SALE is recorded in show.booked_seats, which")
        print("    has no TTL. The lock expiring cannot un-sell a seat.")

    print()
    print("=" * 70)
    print("SCENARIO 3: payment failure releases the seats immediately")
    print("=" * 70)
    show = _build_show()
    service = BookingService(InMemoryLockProvider(), AlwaysFailsGateway())
    booking = service.create_booking("alice", show, [show.screen.seats["A3"]])
    print(f"  A3 available while Alice holds it? "
          f"{'A3' in service.available_seats(show)}")
    service.confirm_booking(booking)
    print(f"  booking is {booking.booking_status.name} / "
          f"{booking.payment_status.name}")
    print(f"  A3 available again? {'A3' in service.available_seats(show)}")

    print()
    print("=" * 70)
    print("SCENARIO 4: multi-seat bookings are ALL-OR-NOTHING")
    print("=" * 70)
    show = _build_show()
    service = BookingService(InMemoryLockProvider())
    service.create_booking("alice", show, [show.screen.seats["A5"]])
    print("  Alice is holding A5.")
    before = service.available_seats(show)
    try:
        service.create_booking(
            "bob", show,
            [show.screen.seats["A4"], show.screen.seats["A5"],
             show.screen.seats["A6"]],
        )
    except SeatUnavailableError as error:
        print(f"  Bob's 3-seat request failed: {error}")
    after = service.available_seats(show)
    print(f"  seats free before Bob tried: {before}")
    print(f"  seats free after  Bob tried: {after}")
    print("  ^ identical. A4 and A6 were locked then rolled back, not stranded.")

    print()
    print("=" * 70)
    print("SCENARIO 5: 10 threads race for the SAME seat")
    print("=" * 70)
    show = _build_show()
    service = BookingService(InMemoryLockProvider())
    winners: List[str] = []
    winners_lock = threading.Lock()
    start_gate = threading.Barrier(10)

    def race(user_id: str) -> None:
        start_gate.wait()  # release all 10 threads at the same instant
        try:
            booking = service.create_booking(user_id, show, [show.screen.seats["A1"]])
            if service.confirm_booking(booking):
                with winners_lock:
                    winners.append(user_id)
        except SeatUnavailableError:
            pass

    threads = [threading.Thread(target=race, args=(f"user{n}",)) for n in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    print(f"  10 users, 1 seat, {len(winners)} winner: {winners}")
    print("  ^ try_lock is atomic, so exactly one thread can win. Without it,")
    print("    several would read 'available' before any of them wrote 'booked'.")

    print()
    print("=" * 70)
    print("SCENARIO 6: an abandoned hold self-heals when the TTL runs out")
    print("=" * 70)
    show = _build_show()
    service = BookingService(InMemoryLockProvider(), lock_ttl_seconds=0.2)
    service.create_booking("ghost", show, [show.screen.seats["A2"]])
    print(f"  A2 free while held?      {'A2' in service.available_seats(show)}")
    time.sleep(0.3)
    print(f"  A2 free after TTL lapse? {'A2' in service.available_seats(show)}")
    print("  ^ no cleanup job needed: an expired lock simply is not a lock.")


if __name__ == "__main__":
    _demo()
