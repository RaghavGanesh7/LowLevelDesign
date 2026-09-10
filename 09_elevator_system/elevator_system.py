"""
================================================================================
 ELEVATOR SYSTEM  --  Low Level Design
================================================================================

THE PROBLEM
-----------
A building with F floors and N elevator cars. People press UP/DOWN buttons in
the hallway; people inside a car press floor buttons. Decide which car answers
each hall call, and in what order each car makes its stops.

NOTE ON PROVENANCE
------------------
This design is NEW in this fork -- it is not one of the eight from upstream.
It is here because it is the classic LLD question that the other eight do not
cover: a *scheduling* problem, where the interesting bug is not a race or a
rounding error but a policy that is merely plausible.

REQUIREMENTS WE ARE DESIGNING FOR
---------------------------------
  R1. Two kinds of request, and they are NOT the same thing:
        hall call = (floor, direction)   "I am on 5 and I want to go DOWN"
        car call  = floor                "take me to 5"
  R2. A car answers a hall call only if it will be travelling in the
      requested direction when it gets there.
  R3. Dispatch policy is swappable -- it is the part a building operator tunes.
  R4. Cars can be taken out of service without stopping the building.
  R5. Thread-safe: every button in the building is a concurrent caller.
  R6. Deterministic and testable -- no sleeps, no wall clock.

DESIGN PATTERNS USED  (jump to the numbered tags in the code below)
-------------------------------------------------------------------
  [1] STATE     -- Elevator: what a car may legally do depends on its motion
                   and its doors. Doors open is a state, not a sleep.
  [2] STRATEGY  -- DispatchStrategy: three policies, one interface.
  [3] OBSERVER  -- ElevatorObserver: displays and metrics watch the same
                   event stream; the car knows about neither.
  [4] FACADE    -- ElevatorSystem: the building's control panel.

THE ONE BIG IDEA
----------------
  *The nearest car is not the soonest car.*

  Every naive dispatcher scores cars by |car_floor - call_floor|, because
  distance is the number sitting right there. But the passenger is not waiting
  for a distance, they are waiting for a TIME, and a car one floor away that is
  accelerating away from you with eight stops queued is minutes further off
  than an idle car three floors below.

  The fix is to score in *time-to-arrive*, which needs two things distance
  alone throws away: which way the car is going, and how much work it already
  has. That is why R1 insists a hall call carries a direction -- a request
  without a direction cannot be scheduled well, only guessed at.

SCHEDULING, IN ONE TABLE
------------------------
  Policy         Picks by         Fails when
  -------------  ---------------  ----------------------------------------
  Nearest Car    distance         the near car is busy or driving away
  Least Busy     queue length     the idle car is 20 floors away
  Directional    travel time      traffic saturates the fleet (see below)

  SCENARIO 5 benchmarks all three on seeded random traffic. Directional wins
  on average wait, worst-case wait and total travel -- except under
  saturation, where every car is busy all the time and no policy can help.
  That is worth knowing in its own right: when a system is saturated you are
  out of scheduling wins and the next lever is capacity, not cleverness.

RUN IT
------
    python 09_elevator_system/elevator_system.py
================================================================================
"""

import random
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, List, Optional, Sequence, Set, Tuple


# =============================================================================
#  SECTION 1 -- THE VOCABULARY
# =============================================================================


class Direction(Enum):
    UP = auto()
    DOWN = auto()
    IDLE = auto()

    def opposite(self) -> "Direction":
        if self is Direction.UP:
            return Direction.DOWN
        if self is Direction.DOWN:
            return Direction.UP
        return Direction.IDLE

    @property
    def step(self) -> int:
        """+1, -1 or 0 -- so movement code never writes an if/else."""
        return {Direction.UP: 1, Direction.DOWN: -1, Direction.IDLE: 0}[self]


class DoorState(Enum):
    OPEN = auto()
    CLOSED = auto()


@dataclass(frozen=True)
class HallCall:
    """
    A hallway button press: a (floor, direction) PAIR.

    WHY THIS IS A TYPE AND NOT AN int -- the single most important line in the
    file. Model a hall call as a bare floor number and you have thrown away the
    only fact that makes the request schedulable:

        Car is at 8 heading DOWN. Someone on 5 wants to go UP.
        As a floor:  "a stop at 5" -- the car stops on its way down, opens its
                     doors at a passenger who wanted to go the other way, and
                     carries them the wrong direction.
        As a pair:   "5 UP" -- the car passes 5, finishes its down sweep, and
                     picks them up on the way back. Which is what every real
                     elevator you have ever ridden does.

    frozen=True makes it hashable, so calls live in a `set` and pressing UP
    thirty times in a busy lobby is thirty presses of ONE call. That
    deduplication is free here and is a bug you have to remember not to write
    if the request is a list entry instead.
    """

    floor: int
    direction: Direction

    def __post_init__(self) -> None:
        if self.direction is Direction.IDLE:
            raise ValueError("a hall call must be UP or DOWN, never IDLE")

    def __str__(self) -> str:
        arrow = "^" if self.direction is Direction.UP else "v"
        return f"{self.floor}{arrow}"


class EventType(Enum):
    HALL_CALL_PLACED = auto()
    CAR_CALL_PLACED = auto()
    MOVED = auto()
    STOPPED = auto()          # doors opened, and these calls were served
    DOORS_CLOSED = auto()
    WENT_IDLE = auto()


@dataclass(frozen=True)
class ElevatorEvent:
    """
    One thing that happened, at one tick. The Observers ([3]) consume these.

    The events carry the tick rather than a timestamp because the whole
    simulation is tick-driven -- see SECTION 6 on why there is no clock here.
    """

    tick: int
    type: EventType
    car_id: str
    floor: int
    direction: Direction = Direction.IDLE
    served: Tuple[HallCall, ...] = ()


# =============================================================================
#  SECTION 2 -- [1] STATE: one elevator car
# =============================================================================


class Elevator:
    """
    One car. It knows how to move itself; it does NOT know how requests are
    assigned to it -- that is the dispatcher's job (SECTION 3).

    That split is the whole architecture. `Elevator.step()` is the mechanical
    truth of a lift (LOOK: keep going this way while there is a reason to,
    then turn around) and never changes. The dispatcher is policy, gets tuned
    per building, and is a Strategy precisely because it is the part that
    changes.

    THE TWO QUEUES, AND WHY THEY ARE SETS
      _car_calls:  floors requested from the panel inside. Direction-free --
                   a passenger inside is already going wherever the car goes.
      _hall_calls: (floor, direction) pairs assigned to this car by the
                   dispatcher.

      Both are sets, not queues, and that is a deliberate rejection of FCFS.
      Serving stops in the order the buttons were pressed would send a car
      1 -> 9 -> 2 -> 8, which is correct, fair, and completely unusable. The
      order of stops is dictated by geometry, not by arrival time. The cost of
      that choice is that FCFS's fairness guarantee is gone, which is why real
      controllers add aging on top -- see the README.
    """

    DOOR_TICKS = 2  # how long the doors stay open. A state, never a sleep.

    def __init__(self, car_id: str, min_floor: int, max_floor: int,
                 start_floor: Optional[int] = None):
        if min_floor >= max_floor:
            raise ValueError("an elevator needs at least two floors to serve")
        self.car_id = car_id
        self.min_floor = min_floor
        self.max_floor = max_floor
        self.current_floor = start_floor if start_floor is not None else min_floor
        if not min_floor <= self.current_floor <= max_floor:
            raise ValueError(f"{car_id} cannot start at floor {self.current_floor}")
        self.direction = Direction.IDLE
        self.doors = DoorState.CLOSED
        self.in_service = True

        self._car_calls: Set[int] = set()
        self._hall_calls: Set[HallCall] = set()
        self._door_ticks_left = 0

    # -- queries the dispatcher uses to score this car ------------------------

    @property
    def car_calls(self) -> Set[int]:
        return set(self._car_calls)

    @property
    def hall_calls(self) -> Set[HallCall]:
        return set(self._hall_calls)

    def pending_floors(self) -> Set[int]:
        """Every floor this car still owes someone a stop at."""
        return self._car_calls | {call.floor for call in self._hall_calls}

    @property
    def load(self) -> int:
        """How much work is queued. The dispatcher's tie-breaker."""
        return len(self.pending_floors())

    def is_idle(self) -> bool:
        return (not self.pending_floors()
                and self.doors is DoorState.CLOSED
                and self._door_ticks_left == 0)

    # -- accepting work -------------------------------------------------------

    def accept_hall_call(self, call: HallCall) -> None:
        self._hall_calls.add(call)

    def accept_car_call(self, floor: int) -> None:
        if not self.min_floor <= floor <= self.max_floor:
            raise ValueError(f"floor {floor} is outside {self.min_floor}..{self.max_floor}")
        # Pressing the floor you are already standing on with the doors open is
        # a no-op, not a stop to make later. Without this the car closes its
        # doors and immediately reopens them, which looks like a bug to anyone
        # standing in it.
        if floor == self.current_floor and self.doors is DoorState.OPEN:
            return
        self._car_calls.add(floor)

    def take_out_of_service(self) -> List[HallCall]:
        """
        R4. Returns the hall calls this car was holding so the SYSTEM can
        reassign them. A car going out of service must never take the waiting
        passengers with it -- dropping them is the failure mode where somebody
        waits on floor 12 forever for a lift that is parked in the basement.

        Car calls are NOT returned: they belong to people who have already got
        out, so they die with the trip.
        """
        self.in_service = False
        orphaned = list(self._hall_calls)
        self._hall_calls.clear()
        self._car_calls.clear()
        self.direction = Direction.IDLE
        return orphaned

    # -- the LOOK algorithm ---------------------------------------------------

    def _stops_ahead(self) -> bool:
        """Is there any pending stop strictly further on in our direction?"""
        if self.direction is Direction.UP:
            return any(f > self.current_floor for f in self.pending_floors())
        if self.direction is Direction.DOWN:
            return any(f < self.current_floor for f in self.pending_floors())
        return False

    def _hall_call_here(self, direction: Direction) -> Optional[HallCall]:
        call = HallCall(self.current_floor, direction)
        return call if call in self._hall_calls else None

    def _should_stop_here(self) -> bool:
        """
        The heart of the design. We stop at the floor we are standing on if:

          a) somebody inside asked for it, or
          b) somebody in the hall asked to go the way we are going, or
          c) we are about to turn around anyway (nothing ahead of us), so the
             opposite-direction call here is now a same-direction call.

        Rule (c) is the one people miss. Without it a car sweeping up to the
        top stop sails past the person on the top floor waiting to come DOWN,
        turns around one floor above them, and comes back for a stop it was
        already standing at. With it, the ends of a sweep are where the car
        flips direction and picks up the reversal traffic -- which is what
        LOOK means and why it is not just "go to the nearest button".
        """
        if self.current_floor in self._car_calls:
            return True
        if self._hall_call_here(self.direction):
            return True
        if not self._stops_ahead() and self._hall_call_here(self.direction.opposite()):
            return True
        return False

    def _serve_here(self) -> Tuple[HallCall, ...]:
        """Open the doors and clear the calls this stop satisfies."""
        served: List[HallCall] = []
        self._car_calls.discard(self.current_floor)

        same = self._hall_call_here(self.direction)
        if same is not None:
            self._hall_calls.remove(same)
            served.append(same)
        elif not self._stops_ahead():
            # Case (c): this is the end of the sweep, so take the reversal
            # call and flip. Flipping HERE, before the doors open, is what
            # makes the direction indicator above the doors truthful -- the
            # passenger stepping in sees "v" and knows this car is going down.
            other = self._hall_call_here(self.direction.opposite())
            if other is not None:
                self._hall_calls.remove(other)
                served.append(other)
                self.direction = other.direction

        self.doors = DoorState.OPEN
        self._door_ticks_left = self.DOOR_TICKS
        return tuple(served)

    def step(self, tick: int) -> List[ElevatorEvent]:
        """
        Advance this car by one tick. At most one floor of travel per tick.

        The whole method is a state machine, in priority order: doors beat
        motion (you cannot move with the doors open -- that is the interlock
        that keeps this from being a guillotine), motion beats idling.
        """
        if not self.in_service:
            return []

        # STATE: doors open. Nothing else can happen until they close.
        if self._door_ticks_left > 0:
            self._door_ticks_left -= 1
            if self._door_ticks_left == 0:
                self.doors = DoorState.CLOSED
                return [ElevatorEvent(tick, EventType.DOORS_CLOSED,
                                      self.car_id, self.current_floor, self.direction)]
            return []

        # STATE: nothing to do.
        if not self.pending_floors():
            if self.direction is not Direction.IDLE:
                self.direction = Direction.IDLE
                return [ElevatorEvent(tick, EventType.WENT_IDLE,
                                      self.car_id, self.current_floor)]
            return []

        # STATE: idle but with work -- choose a direction to commit to.
        if self.direction is Direction.IDLE:
            self.direction = self._direction_of_nearest_stop()

        # Are we already standing at a floor we owe a stop to?
        if self._should_stop_here():
            served = self._serve_here()
            return [ElevatorEvent(tick, EventType.STOPPED, self.car_id,
                                  self.current_floor, self.direction, served)]

        # Nothing ahead: turn around. Reversing costs this tick -- a real car
        # decelerates, and charging nothing for a reversal is the modelling
        # error that makes a simulated lift outperform a physical one.
        if not self._stops_ahead():
            self.direction = self.direction.opposite()
            return []

        self.current_floor += self.direction.step
        events = [ElevatorEvent(tick, EventType.MOVED, self.car_id,
                                self.current_floor, self.direction)]
        if self._should_stop_here():
            served = self._serve_here()
            events.append(ElevatorEvent(tick, EventType.STOPPED, self.car_id,
                                        self.current_floor, self.direction, served))
        return events

    def _direction_of_nearest_stop(self) -> Direction:
        """
        Committing an idle car to a direction. If the call is on our own floor
        we adopt ITS direction, so that `_should_stop_here` sees a
        same-direction call and opens the doors on this tick rather than
        picking a heading at random and driving away from the passenger.
        """
        if self.current_floor in self.pending_floors():
            for direction in (Direction.UP, Direction.DOWN):
                if self._hall_call_here(direction):
                    return direction
            return Direction.UP  # a car call on this very floor: direction is moot
        nearest = min(self.pending_floors(),
                      key=lambda f: (abs(f - self.current_floor), f))
        return Direction.UP if nearest > self.current_floor else Direction.DOWN

    # -- display --------------------------------------------------------------

    def __str__(self) -> str:
        arrow = {Direction.UP: "^", Direction.DOWN: "v", Direction.IDLE: "-"}[self.direction]
        doors = "[ ]" if self.doors is DoorState.CLOSED else "[|]"
        if not self.in_service:
            return f"{self.car_id} floor {self.current_floor:>2}  OUT OF SERVICE"
        queued = ",".join(str(c) for c in sorted(self._hall_calls, key=lambda c: c.floor))
        return (f"{self.car_id} floor {self.current_floor:>2} {arrow} {doors} "
                f"car={sorted(self._car_calls)} hall=[{queued}]")


# =============================================================================
#  SECTION 3 -- [2] STRATEGY: who answers the call?
# =============================================================================


class DispatchStrategy(ABC):
    """
    Given a hall call and the cars, pick one. Returns None if nothing can
    serve it (every car out of service), and the caller decides what that
    means -- this class does not get to invent a policy for a broken building.
    """

    name = "abstract"

    @abstractmethod
    def choose(self, call: HallCall, cars: Sequence[Elevator]) -> Optional[Elevator]:
        ...

    @staticmethod
    def _available(cars: Sequence[Elevator]) -> List[Elevator]:
        return [car for car in cars if car.in_service]


class NearestCarDispatcher(DispatchStrategy):
    """
    Score = |car floor - call floor|. The obvious answer, and wrong.

    It is wrong because distance is not time. This policy will happily hand a
    call to a car one floor below that is accelerating upward with six stops
    queued, while an idle car two floors above does nothing. SCENARIO 3
    measures exactly that: same building, same call, one car 19 ticks of
    waiting and the other 1.

    It is in this file as the control group. Keeping the naive strategy around
    and *measurable* is worth more than deleting it -- when someone proposes it
    in a design review you can produce the number instead of an opinion.
    """

    name = "nearest-car"

    def choose(self, call: HallCall, cars: Sequence[Elevator]) -> Optional[Elevator]:
        available = self._available(cars)
        if not available:
            return None
        return min(available, key=lambda car: (abs(car.current_floor - call.floor), car.car_id))


class LeastBusyDispatcher(DispatchStrategy):
    """
    Score = queue length. Perfect load balancing, mediocre service.

    Worth understanding because it is the *other* single-number policy, and it
    fails in the mirror-image way: it will send the empty car on floor 1 to a
    call on floor 20 while a car already stopping on 19 has two stops queued.
    Balanced fleet, unhappy passenger.

    Two policies that are each obviously reasonable, that disagree, and that
    are both beaten by combining them -- that is the argument for Strategy
    being a real interface here and not premature abstraction.
    """

    name = "least-busy"

    def choose(self, call: HallCall, cars: Sequence[Elevator]) -> Optional[Elevator]:
        available = self._available(cars)
        if not available:
            return None
        return min(available, key=lambda car: (car.load, abs(car.current_floor - call.floor), car.car_id))


class DirectionalDispatcher(DispatchStrategy):
    """
    Score = how far this car must actually travel before it can pick you up,
    plus what it owes everyone else first. This is the one buildings run (the
    family is called "collective control"; the sweep itself is LOOK).

    THE COST MODEL, in two cases:

      Case 1 -- the car is idle, or is already coming toward you going your
                way. It will reach you on its current sweep:

                    cost = |car floor - call floor|

      Case 2 -- anything else: the car is going the wrong way, or away from
                you. Then it cannot turn around on the spot; it finishes what
                it is doing first. The place it turns around is its FURTHEST
                PENDING STOP in its current direction -- call that the turn
                point -- and from there it comes to you:

                    cost = |car floor - turn| + |turn - call floor|

      Both cases then add `LOAD_PENALTY * load`, because every stop already
      queued is a door cycle you will be standing there for.

    WHY THE TURN POINT AND NOT THE TOP OF THE BUILDING. The first version of
    this file penalised a wrong-way car by the height of the building, on the
    theory that it had "a sweep to finish". That is the intuitive model and it
    is measurably wrong: a car at floor 10 whose last stop is 12 turns around
    at 12, not at 40. Charging it the full span makes every moving car look
    hopeless, so the calls get scattered onto idle cars far away, and the
    policy ends up WORSE than plain nearest-car -- which is how the bug was
    found, because SCENARIO 5 measures it. The turn point is the same idea
    with the real number in it.

    It is still an estimate: it ignores that new calls will extend the sweep
    and that the car decelerates. A controller with a CPU to spare simulates
    each car's full itinerary instead. Not worth it here -- the estimate only
    has to RANK the cars correctly, and the measurement says it does.

    WHAT THIS STILL DOESN'T DO: nothing here ages a waiting call, so under
    permanent heavy traffic an unlucky call can keep losing to fresher,
    better-placed ones. Real controllers add a term that grows with wait time.
    Left out on purpose -- the README shows the term, and an interviewer
    asking "how do you know nobody starves?" is asking for it by name.
    """

    name = "directional"

    # One queued stop ~ one door cycle of extra waiting, which is where the
    # value comes from: it is a duration, not a knob someone liked the look of.
    # Swept 0 -> 5 across the SCENARIO 5 benchmark, average wait is flat within
    # noise from 0.25 to 1.0 and degrades clearly from 1.5 up (at 5.0 the
    # policy is spreading calls to cars that are nowhere near). So: anywhere in
    # the flat region works, and 1.0 is the end of it that means something
    # physical. Report the flat region, not just the winner -- a constant
    # tuned to the third decimal of one benchmark is overfitted to it.
    LOAD_PENALTY = 1.0

    def choose(self, call: HallCall, cars: Sequence[Elevator]) -> Optional[Elevator]:
        available = self._available(cars)
        if not available:
            return None
        return min(available, key=lambda car: (self._cost(call, car), car.car_id))

    def _cost(self, call: HallCall, car: Elevator) -> float:
        current = car.current_floor
        moving_toward = (
            (car.direction is Direction.UP and call.floor >= current)
            or (car.direction is Direction.DOWN and call.floor <= current)
        )
        if car.direction is Direction.IDLE or (moving_toward and car.direction is call.direction):
            travel = float(abs(current - call.floor))
        else:
            turn = self._turn_point(car)
            travel = float(abs(current - turn) + abs(turn - call.floor))
        return travel + self.LOAD_PENALTY * car.load

    @staticmethod
    def _turn_point(car: Elevator) -> int:
        """
        The floor where this car will reverse: its furthest pending stop in the
        direction it is travelling. If it has nothing ahead it is turning around
        right here, so the turn point is the floor it is on.
        """
        if car.direction is Direction.UP:
            ahead = [f for f in car.pending_floors() if f > car.current_floor]
            return max(ahead) if ahead else car.current_floor
        if car.direction is Direction.DOWN:
            ahead = [f for f in car.pending_floors() if f < car.current_floor]
            return min(ahead) if ahead else car.current_floor
        return car.current_floor


# =============================================================================
#  SECTION 4 -- [3] OBSERVER: watching without being wired in
# =============================================================================


class ElevatorObserver(ABC):
    """
    The car emits events; whoever cares subscribes. The car has no field
    pointing at a display and no field pointing at a metrics sink, so adding
    the building's lobby screen later changes nothing in SECTION 2.
    """

    @abstractmethod
    def on_event(self, event: ElevatorEvent) -> None:
        ...


class ConsoleDisplay(ElevatorObserver):
    """The indicator above the doors. Prints; that is all it does."""

    def __init__(self, indent: str = "    ") -> None:
        self.indent = indent
        self.enabled = True

    def on_event(self, event: ElevatorEvent) -> None:
        if not self.enabled:
            return
        arrow = {Direction.UP: "^", Direction.DOWN: "v", Direction.IDLE: "-"}[event.direction]
        if event.type is EventType.MOVED:
            print(f"{self.indent}t{event.tick:<3} {event.car_id} passing {event.floor} {arrow}")
        elif event.type is EventType.STOPPED:
            served = " serving " + ",".join(str(c) for c in event.served) if event.served else ""
            print(f"{self.indent}t{event.tick:<3} {event.car_id} STOP at {event.floor} {arrow}{served}")
        elif event.type is EventType.WENT_IDLE:
            print(f"{self.indent}t{event.tick:<3} {event.car_id} idle at {event.floor}")


class WaitTimeMetrics(ElevatorObserver):
    """
    Turns the same event stream into numbers: how long each hall call waited
    between the button press and the doors opening.

    This is why Observer earns its place. Without it, measuring waits means
    either the Elevator holding a metrics object (a car that knows about
    monitoring) or the demo scraping printed strings. With it, the strategy
    comparison in SCENARIO 5 is just "attach a fresh metrics observer and run
    the same trace three times".
    """

    def __init__(self) -> None:
        self._placed_at: Dict[HallCall, int] = {}
        self.waits: Dict[HallCall, int] = {}

    def on_event(self, event: ElevatorEvent) -> None:
        if event.type is EventType.HALL_CALL_PLACED:
            call = HallCall(event.floor, event.direction)
            # setdefault: a re-press of a call already waiting must not reset
            # its clock. Otherwise an impatient passenger jabbing the button
            # makes the dashboard say they were served instantly.
            self._placed_at.setdefault(call, event.tick)
        elif event.type is EventType.STOPPED:
            for call in event.served:
                placed = self._placed_at.pop(call, None)
                if placed is not None:
                    self.waits[call] = event.tick - placed

    @property
    def outstanding(self) -> int:
        return len(self._placed_at)

    def average_wait(self) -> float:
        return sum(self.waits.values()) / len(self.waits) if self.waits else 0.0

    def max_wait(self) -> int:
        return max(self.waits.values()) if self.waits else 0


# =============================================================================
#  SECTION 5 -- [4] FACADE: the building's control panel
# =============================================================================


class ElevatorSystem:
    """
    What the rest of the world talks to: press a button, advance time, read
    the state. Callers never touch an Elevator or a DispatchStrategy.

    THREAD SAFETY (R5). Every button in the building is a concurrent caller,
    and the read-then-write inside `press_hall_button` -- is this call already
    assigned? no? assign it -- is exactly the find-and-claim race from
    01_parking_lot. Two threads interleaving there send two cars to the same
    call: one arrives, opens its doors to an empty hallway, and burns a trip.
    One lock around the whole check-and-assign, held for microseconds.

    `step()` takes the same lock, so time cannot advance halfway through an
    assignment and leave a car moving on a call it has not been given yet.
    """

    def __init__(self, min_floor: int, max_floor: int, cars: Sequence[Elevator],
                 strategy: Optional[DispatchStrategy] = None):
        if min_floor >= max_floor:
            raise ValueError("a building needs at least two floors")
        if not cars:
            raise ValueError("a building needs at least one car")
        self.min_floor = min_floor
        self.max_floor = max_floor
        self.cars: List[Elevator] = list(cars)
        self.strategy = strategy or DirectionalDispatcher()
        self.tick = 0
        self._observers: List[ElevatorObserver] = []
        self._lock = threading.RLock()

    # -- wiring ---------------------------------------------------------------

    def subscribe(self, observer: ElevatorObserver) -> None:
        self._observers.append(observer)

    def _publish(self, events: Sequence[ElevatorEvent]) -> None:
        for event in events:
            for observer in self._observers:
                observer.on_event(event)

    def car(self, car_id: str) -> Elevator:
        for car in self.cars:
            if car.car_id == car_id:
                return car
        raise KeyError(f"no car {car_id!r}")

    # -- buttons --------------------------------------------------------------

    def press_hall_button(self, floor: int, direction: Direction) -> Optional[Elevator]:
        """
        Somebody in the hallway wants to go UP or DOWN. Returns the car
        assigned, or None if the building has no car in service.
        """
        if not self.min_floor <= floor <= self.max_floor:
            raise ValueError(f"floor {floor} is outside {self.min_floor}..{self.max_floor}")
        # The two impossible buttons. Physically they do not exist -- there is
        # no DOWN button in the basement -- so accepting one is accepting a
        # request no car can ever satisfy, which sits in a queue forever.
        # Reject at the boundary, where the caller still has the context to
        # know what it did wrong.
        if direction is Direction.UP and floor == self.max_floor:
            raise ValueError("no UP button on the top floor")
        if direction is Direction.DOWN and floor == self.min_floor:
            raise ValueError("no DOWN button on the bottom floor")

        call = HallCall(floor, direction)
        with self._lock:
            existing = self._assigned_car(call)
            if existing is not None:
                # Already someone's job. Thirty people pressing UP in the lobby
                # is one call, one car -- not thirty cars.
                return existing

            chosen = self.strategy.choose(call, self.cars)
            if chosen is None:
                return None
            chosen.accept_hall_call(call)
            self._publish([ElevatorEvent(self.tick, EventType.HALL_CALL_PLACED,
                                         chosen.car_id, floor, direction)])
            return chosen

    def press_car_button(self, car_id: str, floor: int) -> None:
        """Somebody inside car `car_id` wants floor `floor`."""
        with self._lock:
            car = self.car(car_id)
            if not car.in_service:
                raise ValueError(f"{car_id} is out of service")
            car.accept_car_call(floor)
            self._publish([ElevatorEvent(self.tick, EventType.CAR_CALL_PLACED,
                                         car_id, floor)])

    def take_out_of_service(self, car_id: str) -> None:
        """R4. Reassign the orphaned hall calls rather than dropping them."""
        with self._lock:
            orphaned = self.car(car_id).take_out_of_service()
            for call in orphaned:
                replacement = self.strategy.choose(call, self.cars)
                if replacement is not None:
                    replacement.accept_hall_call(call)

    # -- time -----------------------------------------------------------------

    def step(self, ticks: int = 1) -> None:
        for _ in range(ticks):
            with self._lock:
                self.tick += 1
                for car in self.cars:
                    self._publish(car.step(self.tick))

    def run_until_idle(self, max_ticks: int = 500) -> int:
        """
        Advance until every car is idle. Returns the ticks spent.

        The `max_ticks` guard is not defensive clutter: a dispatcher bug that
        assigns a call no car will ever stop for turns this into an infinite
        loop, and a test suite that hangs tells you far less than one that
        fails. Bound every loop whose exit depends on the code under test.
        """
        spent = 0
        while spent < max_ticks and not all(car.is_idle() for car in self.cars if car.in_service):
            self.step()
            spent += 1
        return spent

    def snapshot(self) -> str:
        return "\n".join(f"  {car}" for car in self.cars)

    def _assigned_car(self, call: HallCall) -> Optional[Elevator]:
        for car in self.cars:
            if call in car.hall_calls:
                return car
        return None


# =============================================================================
#  SECTION 6 -- WHY THERE IS NO CLOCK IN THIS FILE
# =============================================================================
#
# 05_rate_limiter measures real durations with time.monotonic(), because a
# rate limiter's whole contract is about wall-clock seconds. This file has no
# clock at all: time is an integer, `tick`, advanced by the caller.
#
# The alternative -- threads plus time.sleep(0.2) per floor -- would demo the
# same behaviour and be worthless as a test. It would be slow (the scenarios
# below span ~100 floor-moves, so ~20 seconds of sleeping), and worse, flaky:
# assertions about "which car got there first" would depend on the scheduler.
#
# Tick-driven simulation makes every scenario below EXACTLY reproducible, which
# is what lets SCENARIO 5 print a comparison table of three policies and have
# the numbers mean something. The rule generalises: make time an input to the
# system under test, not something it reaches out and reads.


def _build(strategy: DispatchStrategy, cars: Sequence[Tuple[str, int]],
           floors: Tuple[int, int] = (1, 12)) -> ElevatorSystem:
    """Small helper so each scenario is one line of setup."""
    low, high = floors
    return ElevatorSystem(low, high,
                          [Elevator(cid, low, high, start) for cid, start in cars],
                          strategy)


@dataclass(frozen=True)
class TrafficProfile:
    """A reproducible workload: what the building looks like and how busy."""

    label: str
    floors: int
    cars: int
    calls: int
    gap: int  # a call arrives every 1..gap ticks


def _benchmark(strategy_factory, profile: TrafficProfile,
               trials: int = 20) -> Tuple[float, float, float]:
    """
    Run `trials` seeded workloads and return (avg wait, worst wait, ticks).

    Every policy is handed seeds 0..trials-1, so they see byte-identical
    traffic and the comparison is a comparison and not a coincidence. This is
    the same reason SECTION 6 has no clock: an experiment you cannot re-run is
    an anecdote.
    """
    waits, worsts, totals = [], [], []
    for seed in range(trials):
        rng = random.Random(seed)
        cars = [Elevator(chr(ord("A") + i), 1, profile.floors,
                         rng.randint(1, profile.floors)) for i in range(profile.cars)]
        system = ElevatorSystem(1, profile.floors, cars, strategy_factory())
        metrics = WaitTimeMetrics()
        system.subscribe(metrics)

        at = 0
        for _ in range(profile.calls):
            at += rng.randint(1, profile.gap)
            while system.tick < at:
                system.step()
            floor = rng.randint(1, profile.floors)
            direction = rng.choice([Direction.UP, Direction.DOWN])
            if floor == profile.floors:
                direction = Direction.DOWN
            if floor == 1:
                direction = Direction.UP
            system.press_hall_button(floor, direction)

        totals.append(system.tick + system.run_until_idle(5000))
        # A policy that leaves someone waiting has not "won" on average wait --
        # it has just excluded its own worst cases from the average.
        assert metrics.outstanding == 0, "a call was never served"
        waits.append(metrics.average_wait())
        worsts.append(metrics.max_wait())
    return (sum(waits) / trials, sum(worsts) / trials, sum(totals) / trials)


# =============================================================================
#  SECTION 7 -- RUNNABLE DEMO
# =============================================================================


def _demo() -> None:
    print("=" * 70)
    print("SCENARIO 1: a hall call is a (floor, DIRECTION) pair -- the headline")
    print("=" * 70)
    system = _build(DirectionalDispatcher(), [("A", 10)])
    display = ConsoleDisplay()
    system.subscribe(display)

    system.press_hall_button(10, Direction.DOWN)   # get A moving down from 10
    system.press_car_button("A", 2)
    system.press_hall_button(5, Direction.UP)      # someone on 5 wants to go UP
    print("  A is at 10 and heading DOWN to 2. Someone on 5 wants to go UP:")
    system.run_until_idle()
    display.enabled = False
    print(f"  -> A passed floor 5 on the way down WITHOUT opening its doors,")
    print(f"     then came back up for it. Modelled as a bare floor number,")
    print(f"     '5' would have been served on the way down and carried that")
    print(f"     passenger to floor 2 -- the wrong way.")

    print()
    print("=" * 70)
    print("SCENARIO 2: stops are served in floor order, not press order (LOOK)")
    print("=" * 70)
    system = _build(DirectionalDispatcher(), [("A", 1)])
    metrics = WaitTimeMetrics()
    system.subscribe(metrics)
    order: List[int] = []

    class _Recorder(ElevatorObserver):
        def on_event(self, event: ElevatorEvent) -> None:
            if event.type is EventType.STOPPED:
                order.append(event.floor)

    system.subscribe(_Recorder())
    for floor in (9, 3, 7, 2):
        system.press_car_button("A", floor)
    print("  buttons pressed inside, in this order: 9, 3, 7, 2")
    system.run_until_idle()
    print(f"  stops actually made:                  {', '.join(map(str, order))}")
    print("  -> FCFS would have driven 1->9->3->7->2 = 20 floors of travel.")
    print(f"     One upward sweep does it in {max(order) - 1}.")

    print()
    print("=" * 70)
    print("SCENARIO 3: the nearest car is NOT the soonest car")
    print("=" * 70)
    print("  A is climbing to 8, 10 and 12. B is idle. Someone presses DOWN")
    print("  on floor 6 -- the floor A has just reached.")
    for strategy in (NearestCarDispatcher(), DirectionalDispatcher()):
        system = _build(strategy, [("A", 5), ("B", 7)])
        metrics = WaitTimeMetrics()
        system.subscribe(metrics)
        for floor in (8, 10, 12):
            system.press_car_button("A", floor)
        system.step()                       # let A commit to going up
        positions = ", ".join(f"{c.car_id} at {c.current_floor}" for c in system.cars)
        chosen = system.press_hall_button(6, Direction.DOWN)
        system.run_until_idle()
        wait = metrics.waits[HallCall(6, Direction.DOWN)]
        print(f"  {strategy.name:<12} ({positions}) -> car {chosen.car_id}, "
              f"waited {wait:>2} ticks")
    print("  -> nearest-car sees A standing on the very floor that called, scores")
    print("     it 0, and never asks which way A is pointing: A carries on up to")
    print("     12 and comes back. B was one floor away and going nowhere.")
    print("     The distance was right. The time was not.")

    print()
    print("=" * 70)
    print("SCENARIO 4: the end of a sweep is where a car flips direction")
    print("=" * 70)
    system = _build(DirectionalDispatcher(), [("A", 1)])
    display = ConsoleDisplay()
    system.subscribe(display)
    system.press_car_button("A", 6)
    system.press_hall_button(6, Direction.DOWN)
    print("  A sweeps up to 6; the person on 6 wants to go DOWN:")
    system.run_until_idle()
    display.enabled = False
    print("  -> A served the DOWN call at 6 in the same stop, because with")
    print("     nothing above it, 'down' IS the direction it is about to travel.")
    print("     Without that rule the car turns around one floor higher and")
    print("     comes back to a floor it was already standing on.")

    print()
    print("=" * 70)
    print("SCENARIO 5: three policies, measured on seeded random traffic")
    print("=" * 70)
    print("  One hand-picked trace proves nothing -- you can always find the")
    print("  building your favourite policy wins in. So: 20 seeded random")
    print("  workloads per profile, every policy sees the identical traffic.")
    for profile in (TrafficProfile("normal  20 floors, 3 cars, 40 calls", 20, 3, 40, 3),
                    TrafficProfile("saturated 20 floors, 3 cars, 60 calls", 20, 3, 60, 1),
                    TrafficProfile("tower   40 floors, 4 cars, 60 calls", 40, 4, 60, 2)):
        print()
        print(f"  {profile.label}")
        print(f"    {'policy':<14}{'avg wait':>10}{'worst wait':>12}{'total ticks':>13}")
        print(f"    {'-' * 49}")
        for strategy_factory in (NearestCarDispatcher, LeastBusyDispatcher, DirectionalDispatcher):
            avg, worst, ticks = _benchmark(strategy_factory, profile)
            print(f"    {strategy_factory.name:<14}{avg:>10.2f}{worst:>12.1f}{ticks:>13.1f}")
    print()
    print("  -> directional wins on all three numbers at normal load and in the")
    print("     tower. Under saturation it trades a slightly worse worst-case")
    print("     for slightly less travel -- i.e. nothing: when every car is")
    print("     already moving all the time there is no good choice left to")
    print("     make. A scheduler cannot manufacture a lift, and past that")
    print("     point the honest answer to 'tune the algorithm' is 'buy a car'.")

    print()
    print("=" * 70)
    print("SCENARIO 6: a car goes out of service mid-trip")
    print("=" * 70)
    system = _build(DirectionalDispatcher(), [("A", 1), ("B", 12)])
    metrics = WaitTimeMetrics()
    system.subscribe(metrics)
    system.press_hall_button(3, Direction.UP)
    system.press_hall_button(4, Direction.UP)
    print("  calls on 3 and 4 assigned to:",
          ", ".join(f"{c}->{system._assigned_car(c).car_id}"
                    for c in (HallCall(3, Direction.UP), HallCall(4, Direction.UP))))
    system.step(1)
    system.take_out_of_service("A")
    print("  A is taken out of service. Its waiting passengers are reassigned:")
    print("  ", ", ".join(f"{c}->{system._assigned_car(c).car_id}"
                          for c in (HallCall(3, Direction.UP), HallCall(4, Direction.UP))))
    system.run_until_idle()
    print(f"  -> both calls served, {metrics.outstanding} left waiting.")
    print("     Dropping them instead is the failure where someone waits")
    print("     forever for a lift that is parked in the basement.")

    print()
    print("=" * 70)
    print("SCENARIO 7: 30 threads on the buttons -- one call, one car")
    print("=" * 70)
    system = _build(DirectionalDispatcher(), [("A", 1), ("B", 6), ("C", 11)])
    metrics = WaitTimeMetrics()
    system.subscribe(metrics)
    presses = [(floor, Direction.UP) for floor in (2, 3, 4, 5)] * 5 + \
              [(floor, Direction.DOWN) for floor in (8, 9)] * 5
    barrier = threading.Barrier(len(presses))

    def press(floor: int, direction: Direction) -> None:
        barrier.wait()                       # maximise the overlap
        system.press_hall_button(floor, direction)

    threads = [threading.Thread(target=press, args=args) for args in presses]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    distinct = len(set(presses))
    assigned = sum(len(car.hall_calls) for car in system.cars)
    print(f"  {len(presses)} concurrent presses of {distinct} distinct calls")
    print(f"  -> {assigned} calls assigned across the fleet (expected {distinct})")
    system.run_until_idle()
    print(f"  -> all served, {metrics.outstanding} left waiting, "
          f"max wait {metrics.max_wait()} ticks")
    print("  ^ without the lock around check-and-assign, two threads racing on")
    print("    the same call send two cars, and one of them opens its doors on")
    print("    an empty hallway.")

    print()
    print("=" * 70)
    print("FINAL STATE")
    print("=" * 70)
    print(system.snapshot())


if __name__ == "__main__":
    _demo()
