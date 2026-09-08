"""
================================================================================
 PARKING LOT  --  Low Level Design
================================================================================

THE PROBLEM
-----------
Design a multi-floor parking lot. Vehicles arrive at an entry gate, get a spot
and a ticket. Later they leave through an exit gate, pay a fare, and free the
spot.

REQUIREMENTS WE ARE DESIGNING FOR
---------------------------------
  R1. Multiple floors, each with many spots.
  R2. Spots are typed (bike / car / truck). A vehicle needs a spot it fits in.
  R3. Entry gate issues a ticket; exit gate computes a fare and frees the spot.
  R4. Fare rules must be swappable (hourly today, weekend-surge tomorrow)
      WITHOUT editing the parking lot code.
  R5. Two cars arriving at the same instant must never get the same spot.

DESIGN PATTERNS USED  (jump to the numbered tags in the code below)
-------------------------------------------------------------------
  [1] STRATEGY  -- PricingStrategy: each pricing rule is its own class.
  [2] FACTORY   -- PriceFactory: picks the right strategy, hides the choice.
  [3] SINGLETON -- ParkingLot: one shared lot that every gate talks to.
  [4] FACADE    -- Gate: a thin front door over the ParkingLot's operations.

THE ONE BIG IDEA
----------------
Gates do NOT own parking logic. They delegate to the ParkingLot. That is why
you can add a 5th entry gate tomorrow and change nothing else.

RUN IT
------
    python 01_parking_lot/parking_lot.py
================================================================================
"""

import math
import threading
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, Optional


# =============================================================================
#  SECTION 1 -- VALUE TYPES (enums + plain data holders)
# =============================================================================
# Start every LLD by naming the *nouns*. These are the nouns of a parking lot.
# Enums instead of raw strings: a typo like "CARR" becomes an error at the
# point you write it, not a silent no-match three layers deeper.


class VehicleType(Enum):
    """What kind of vehicle, and how much room it needs."""

    BIKE = "BIKE"
    CAR = "CAR"
    TRUCK = "TRUCK"


class PaymentStatus(Enum):
    """Lifecycle of the money side of a ticket."""

    PENDING = "PENDING"  # vehicle is still parked, fare not yet due
    PAID = "PAID"  # fare settled at the exit gate


class Vehicle:
    """The thing being parked. Deliberately dumb -- it holds no logic."""

    def __init__(self, number: str, vehicle_type: VehicleType):
        self.number = number  # e.g. "KA01AB1234"
        self.vehicle_type = vehicle_type

    def __repr__(self) -> str:
        return f"Vehicle({self.number}, {self.vehicle_type.value})"


class ParkingSpot:
    """
    One painted rectangle on the floor.

    NOTE ON `occupied`: this is mutable state shared between threads. It is
    only ever read/written while holding ParkingLot._lock (see SECTION 4).
    """

    def __init__(self, spot_id: str, allowed: VehicleType):
        self.spot_id = spot_id
        self.allowed = allowed  # which vehicle type may use this spot
        self.occupied = False

    def __repr__(self) -> str:
        state = "occupied" if self.occupied else "free"
        return f"Spot({self.spot_id}, {self.allowed.value}, {state})"


class Ticket:
    """
    The receipt handed out at entry. It is the *only* thing the customer
    carries, so it must contain everything the exit gate needs to find the
    spot again: floor id + spot id.
    """

    def __init__(
        self,
        ticket_id: str,
        floor_id: str,
        spot_id: str,
        vehicle: Vehicle,
        entry_time: float,
    ):
        self.ticket_id = ticket_id
        self.floor_id = floor_id
        self.spot_id = spot_id
        self.vehicle = vehicle
        self.entry_time = entry_time
        self.status = PaymentStatus.PENDING

    def __repr__(self) -> str:
        return f"Ticket({self.ticket_id}, spot={self.spot_id}, {self.status.value})"


# =============================================================================
#  SECTION 2 -- [1] STRATEGY PATTERN: pricing
# =============================================================================
# WHY A PATTERN HERE AT ALL?
#   The naive version is `if is_subscriber: fare = 0 else: fare = hours * rate`
#   inside ParkingLot. Then marketing asks for weekend pricing, then EV
#   pricing, then happy-hour pricing -- and ParkingLot grows a swamp of ifs.
#
#   Strategy says: make each rule a class behind one interface, and let the
#   caller hold a reference to "some pricing rule". Adding WeekendPricing then
#   means adding a file, not editing ParkingLot. That is the Open/Closed
#   Principle in one move: OPEN to extension, CLOSED to modification.


class PricingStrategy(ABC):
    """The contract every pricing rule must satisfy."""

    @abstractmethod
    def calculate(
        self, vehicle_type: VehicleType, entry_time: float, exit_time: float
    ) -> float:
        """Return the fare in rupees for one completed stay."""
        raise NotImplementedError


class HourlyPricing(PricingStrategy):
    """Pay-per-hour, rounded UP, with a 1-hour minimum."""

    # Per-hour rate by vehicle type. A dict beats an if/elif chain: adding a
    # new vehicle type is a one-line data change, not a code change.
    RATE_PER_HOUR: Dict[VehicleType, float] = {
        VehicleType.BIKE: 10.0,
        VehicleType.CAR: 20.0,
        VehicleType.TRUCK: 30.0,
    }

    def calculate(
        self, vehicle_type: VehicleType, entry_time: float, exit_time: float
    ) -> float:
        seconds = max(0.0, exit_time - entry_time)

        # CEILING DIVISION. The original wrote this as `-(-x // 3600)`, the
        # classic integer trick -- correct, but write-only code. math.ceil
        # says the same thing in English. Parking 61 minutes bills 2 hours.
        hours = math.ceil(seconds / 3600)

        # Minimum billable duration: a 3-minute stay still costs one hour.
        billable_hours = max(1, hours)
        return billable_hours * self.RATE_PER_HOUR[vehicle_type]


class MonthlyPassPricing(PricingStrategy):
    """
    Subscriber already paid up front for the month, so this stay costs 0.

    NOTE (fixed from the original): returning a bare 0 with no explanation
    looks like a bug to the next reader. The zero is *intentional* -- the
    money was collected elsewhere. Saying so is the whole fix.
    """

    def calculate(
        self, vehicle_type: VehicleType, entry_time: float, exit_time: float
    ) -> float:
        return 0.0


class FreeFirstHourPricing(PricingStrategy):
    """
    Example of how cheap a new rule is once Strategy is in place: first hour
    free, hourly after that. Written by adding a class -- nothing else in this
    file had to change.
    """

    def __init__(self) -> None:
        self._hourly = HourlyPricing()

    def calculate(
        self, vehicle_type: VehicleType, entry_time: float, exit_time: float
    ) -> float:
        if exit_time - entry_time <= 3600:
            return 0.0
        return self._hourly.calculate(vehicle_type, entry_time + 3600, exit_time)


# =============================================================================
#  SECTION 3 -- [2] FACTORY PATTERN: choosing a strategy
# =============================================================================
# Strategy answers "how do I compute a fare?". Factory answers "WHICH rule
# applies to this customer?". Keeping those two questions in separate classes
# means the selection policy can change without touching any pricing maths.


class PriceFactory:
    """Picks the pricing strategy that applies to a given stay."""

    @staticmethod
    def calculate_fare(
        vehicle_type: VehicleType,
        entry_time: float,
        exit_time: float,
        is_subscriber: bool,
    ) -> float:
        strategy: PricingStrategy = (
            MonthlyPassPricing() if is_subscriber else HourlyPricing()
        )
        return strategy.calculate(vehicle_type, entry_time, exit_time)


# =============================================================================
#  SECTION 4 -- THE CORE: floors and the lot itself
# =============================================================================


class ParkingFloor:
    """One physical floor. Owns its spots and knows how to search them."""

    def __init__(self, floor_id: str):
        self.floor_id = floor_id
        self.spots: Dict[str, ParkingSpot] = {}

    def add_spot(self, spot: ParkingSpot) -> None:
        self.spots[spot.spot_id] = spot

    def find_available_spot(self, vehicle_type: VehicleType) -> Optional[ParkingSpot]:
        """
        First free spot of the exact matching type, or None.

        DESIGN NOTE -- a deliberate simplification:
        A CAR will not be given a TRUCK spot here, even if only truck spots are
        free. Real lots usually allow "fits in anything at least this big".
        To do that you would give VehicleType an ordering and search from the
        smallest spot the vehicle fits in upward. Left simple on purpose so the
        search stays one obvious loop -- see 03_amazon_locker for the
        "smallest thing that fits" version.

        NOTE: this is NOT thread-safe by itself. It returns a spot that is
        still free *right now*; the caller must be holding the lot's lock and
        must mark it occupied before releasing it.
        """
        for spot in self.spots.values():
            if not spot.occupied and spot.allowed == vehicle_type:
                return spot
        return None

    def __repr__(self) -> str:
        free = sum(1 for s in self.spots.values() if not s.occupied)
        return f"Floor({self.floor_id}, {free}/{len(self.spots)} free)"


class ParkingLot:
    """
    [3] SINGLETON -- there is exactly one physical lot, so there is exactly one
    ParkingLot object. Every gate shares it.

    HOW THE SINGLETON IS DONE HERE, AND WHY:
      The common textbook version guards __init__ and hands out instances from
      a static get_instance(). That version has a hole: the very first direct
      call `ParkingLot()` succeeds and creates an object that never gets
      registered as *the* instance -- so you silently end up with two lots.

      We close the hole in __new__ instead. __new__ runs before __init__ and
      is the only place that can decide "return the existing object". Now
      `ParkingLot()` and `ParkingLot.get_instance()` are the same thing and
      cannot disagree.

    WHY THREAD SAFETY MATTERS HERE:
      "find a free spot" then "mark it occupied" is two steps. If two threads
      interleave between those steps they both see the same free spot and both
      park in it. That class of bug is called a race condition, and it is the
      #1 thing an interviewer probes for in this problem. One lock around the
      whole find-and-claim sequence makes the pair atomic.
    """

    _instance: Optional["ParkingLot"] = None
    _singleton_lock = threading.Lock()  # guards creation of the instance

    def __new__(cls) -> "ParkingLot":
        # Double-checked locking: the fast path (already created) never takes
        # the lock; only the first-ever construction does.
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._init_state()
                    cls._instance = instance
        return cls._instance

    def _init_state(self) -> None:
        """
        Real initialisation. Kept OUT of __init__ on purpose: __init__ runs on
        every `ParkingLot()` call, even when __new__ returned the existing
        object -- which would wipe the lot's state every time a gate looked it
        up. This method runs exactly once.
        """
        self.floors: Dict[str, ParkingFloor] = {}
        self.active_tickets: Dict[str, Ticket] = {}
        self._ticket_counter = 0
        self._lock = threading.Lock()  # guards floors/spots/tickets

    @classmethod
    def get_instance(cls) -> "ParkingLot":
        """Explicit accessor. Reads better at call sites than `ParkingLot()`."""
        return cls()

    @classmethod
    def _reset_for_demo(cls) -> None:
        """Test/demo helper -- throws away the singleton so a fresh run is clean."""
        cls._instance = None

    def add_floor(self, floor: ParkingFloor) -> None:
        with self._lock:
            self.floors[floor.floor_id] = floor

    def park_vehicle(self, vehicle: Vehicle) -> Optional[Ticket]:
        """
        Find a spot, claim it, issue a ticket. Returns None if the lot is full.

        Everything happens under one lock so that find-then-claim is atomic.
        """
        with self._lock:
            for floor in self.floors.values():
                spot = floor.find_available_spot(vehicle.vehicle_type)
                if spot is None:
                    continue  # this floor is full for this type, try the next

                spot.occupied = True  # claim it BEFORE releasing the lock
                self._ticket_counter += 1
                ticket = Ticket(
                    ticket_id=f"T{self._ticket_counter}",
                    floor_id=floor.floor_id,
                    spot_id=spot.spot_id,
                    vehicle=vehicle,
                    entry_time=time.time(),
                )
                self.active_tickets[ticket.ticket_id] = ticket
                print(f"  [LOT] Parked {vehicle.number} at "
                      f"{floor.floor_id}/{spot.spot_id} -> {ticket.ticket_id}")
                return ticket

            print(f"  [LOT] No spot available for {vehicle}")
            return None

    def unpark_vehicle(self, ticket_id: str, is_subscriber: bool = False) -> float:
        """
        Free the spot and return the fare owed.

        RAISES ValueError on an unknown ticket.

        WHY RAISE INSTEAD OF RETURNING -1 (fixed from the original):
          A -1 return is a "magic sentinel". Every caller must remember to
          check for it, and the one caller who forgets will happily add -1 to
          the day's revenue. An exception cannot be ignored by accident. Rule
          of thumb: return a value for expected outcomes, raise for broken
          preconditions. A missing ticket is a broken precondition.
        """
        with self._lock:
            ticket = self.active_tickets.get(ticket_id)
            if ticket is None:
                raise ValueError(f"Unknown or already-used ticket: {ticket_id!r}")

            # Free the physical spot using the breadcrumbs on the ticket.
            self.floors[ticket.floor_id].spots[ticket.spot_id].occupied = False

            fare = PriceFactory.calculate_fare(
                vehicle_type=ticket.vehicle.vehicle_type,
                entry_time=ticket.entry_time,
                exit_time=time.time(),
                is_subscriber=is_subscriber,
            )

            # FIXED: the original never moved the ticket out of PENDING, so
            # PaymentStatus was dead code. Mark it paid, then retire it.
            ticket.status = PaymentStatus.PAID
            del self.active_tickets[ticket_id]

            print(f"  [LOT] Unparked {ticket_id} -> fare Rs.{fare:.2f}")
            return fare

    def availability_report(self) -> str:
        with self._lock:
            return " | ".join(str(f) for f in self.floors.values())


# =============================================================================
#  SECTION 5 -- [4] FACADE: the gates
# =============================================================================
# A gate is a piece of hardware with a barrier and a printer. It owns NO
# parking logic -- it just forwards to the lot. That separation is what lets
# you add gates freely, and it is what an interviewer means by "who owns this
# responsibility?".


class Gate:
    """Base class: every gate has an id and a handle on the one lot."""

    def __init__(self, gate_id: str):
        self.gate_id = gate_id
        self.lot = ParkingLot.get_instance()


class EntryGate(Gate):
    def park_vehicle(self, vehicle: Vehicle) -> Optional[Ticket]:
        print(f"[{self.gate_id}] {vehicle.number} requesting entry")
        return self.lot.park_vehicle(vehicle)


class ExitGate(Gate):
    def unpark_vehicle(self, ticket_id: str, is_subscriber: bool = False) -> float:
        print(f"[{self.gate_id}] processing {ticket_id}")
        return self.lot.unpark_vehicle(ticket_id, is_subscriber)


# =============================================================================
#  SECTION 6 -- RUNNABLE DEMO
# =============================================================================
# Reading a design is one thing; watching it run is another. Every file in this
# repo ends with a demo you can execute and step through.


def _demo() -> None:
    ParkingLot._reset_for_demo()
    lot = ParkingLot.get_instance()

    # --- Build a tiny lot: 1 floor, 2 car spots, 1 bike spot ---------------
    ground = ParkingFloor("F1")
    ground.add_spot(ParkingSpot("C1", VehicleType.CAR))
    ground.add_spot(ParkingSpot("C2", VehicleType.CAR))
    ground.add_spot(ParkingSpot("B1", VehicleType.BIKE))
    lot.add_floor(ground)

    entry = EntryGate("ENTRY-1")
    exit_gate = ExitGate("EXIT-1")

    print("=" * 70)
    print("SCENARIO 1: happy path -- park then unpark")
    print("=" * 70)
    ticket = entry.park_vehicle(Vehicle("KA01AB1234", VehicleType.CAR))
    print(f"  availability: {lot.availability_report()}")
    exit_gate.unpark_vehicle(ticket.ticket_id)
    print(f"  availability: {lot.availability_report()}")

    print()
    print("=" * 70)
    print("SCENARIO 2: lot fills up -- the 3rd car is turned away")
    print("=" * 70)
    t1 = entry.park_vehicle(Vehicle("CAR-A", VehicleType.CAR))
    t2 = entry.park_vehicle(Vehicle("CAR-B", VehicleType.CAR))
    t3 = entry.park_vehicle(Vehicle("CAR-C", VehicleType.CAR))  # -> None
    print(f"  third car got a ticket? {t3 is not None}")
    print(f"  availability: {lot.availability_report()}")

    print()
    print("=" * 70)
    print("SCENARIO 3: the SINGLETON really is one object")
    print("=" * 70)
    print(f"  ParkingLot() is ParkingLot.get_instance() -> "
          f"{ParkingLot() is ParkingLot.get_instance()}")
    print(f"  gate's lot is the same object            -> {entry.lot is lot}")

    print()
    print("=" * 70)
    print("SCENARIO 4: bad ticket raises instead of returning -1")
    print("=" * 70)
    try:
        exit_gate.unpark_vehicle("T-DOES-NOT-EXIST")
    except ValueError as err:
        print(f"  caught as expected: {err}")

    print()
    print("=" * 70)
    print("SCENARIO 5: swapping the pricing STRATEGY changes nothing else")
    print("=" * 70)
    now = time.time()
    two_hours_ago = now - 2 * 3600
    for strategy in (HourlyPricing(), MonthlyPassPricing(), FreeFirstHourPricing()):
        fare = strategy.calculate(VehicleType.CAR, two_hours_ago, now)
        print(f"  {strategy.__class__.__name__:<24} 2h car stay -> Rs.{fare:.2f}")

    # Clean up so the demo leaves no vehicles behind.
    exit_gate.unpark_vehicle(t1.ticket_id)
    exit_gate.unpark_vehicle(t2.ticket_id, is_subscriber=True)


if __name__ == "__main__":
    _demo()
