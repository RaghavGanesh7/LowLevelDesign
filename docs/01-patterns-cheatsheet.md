# Design patterns cheat sheet

Only the patterns that actually appear in this repo, each with the concrete
place you can go read it working.

---

## Quick index

| Pattern | One-line purpose | Live in this repo |
|---|---|---|
| [Strategy](#strategy) | Swap an algorithm without touching the caller | Pricing · Pieces · Rate limiters · Splits |
| [Factory](#factory) | Decide *which* class to build, in one place | `PriceFactory` · `PieceFactory` · `RateLimiterFactory` |
| [Singleton](#singleton) | One instance for the whole process | `ParkingLot` · `BankServer` · `Logger` |
| [Facade](#facade) | A small, task-shaped API over a subsystem | `Gate` · `LockerController` · `BookingService` |
| [Command](#command) | Package an action as a reversible object | `Move` in chess |
| [Observer](#observer) | One event, many listeners | `Logger` → N appenders |
| [Decorator](#decorator) | Add behaviour by wrapping, not subclassing | `AsyncAppender` |
| [Composite](#composite) | Treat one and many identically | `MultiChannelNotification` |
| [Chain of Responsibility](#chain-of-responsibility) | Pass a request along until someone handles it | `LogHandler` (and why not to) |
| [State machine](#state-machine) | Legal operations depend on where you are | `ATMState` · `BookingStatus` |

---

## Strategy

**Problem it solves.** You have an `if/elif` ladder that grows every time the
business adds a rule, and it lives in the middle of a class that has nothing to
do with those rules.

**Shape.** One interface, N implementations, and the caller holds a reference
to the interface.

```python
class PricingStrategy(ABC):
    @abstractmethod
    def calculate(self, vehicle_type, entry, exit) -> float: ...

class HourlyPricing(PricingStrategy): ...
class MonthlyPassPricing(PricingStrategy): ...
```

**Why it matters.** Adding `WeekendSurgePricing` means *adding a file*, not
editing `ParkingLot`. That is the **Open/Closed Principle** in one move — open
to extension, closed to modification.

**In this repo**
- [`01_parking_lot`](../01_parking_lot/) — `PricingStrategy`, the cleanest example
- [`08_chess_game`](../08_chess_game/) — `Piece`, the richest: six algorithms
- [`05_rate_limiter`](../05_rate_limiter/) — four genuinely different algorithms
- [`06_splitwise`](../06_splitwise/) — `SplitStrategy`, where each one validates its own input
- [`07_book_my_show`](../07_book_my_show/) — `LockProvider`, in-memory vs Redis

**Tell.** Any time you catch yourself writing `if type == A: ... elif type == B:`
for the second time.

---

## Factory

**Problem it solves.** Strategy answers *"how do I compute this?"*. Factory
answers *"which one applies?"*. Mixing the two puts selection policy inside
the algorithm.

**Shape.** Prefer a dict registry over an if/elif ladder — registering a new
type is one entry, and there is no branch to forget.

```python
class RateLimiterFactory:
    _REGISTRY = {
        RateLimitType.TOKEN_BUCKET: TokenBucket,
        RateLimitType.FIXED_WINDOW: FixedWindow,
        ...
    }
    @classmethod
    def create(cls, limiter_type, config):
        return cls._REGISTRY[limiter_type](config, limiter_type)
```

**In this repo:** `PriceFactory`, `PieceFactory`, `RateLimiterFactory`,
`SplitStrategyFactory`.

---

## Singleton

**Problem it solves.** Some things are genuinely unique in a process — the one
physical parking lot, the one connection pool, the one logger.

**Shape — the correct one.** Guard in `__new__`, not `__init__`:

```python
class ParkingLot:
    _instance = None
    _singleton_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:              # fast path, no lock
            with cls._singleton_lock:
                if cls._instance is None:      # double-checked
                    instance = super().__new__(cls)
                    instance._init_state()     # runs exactly ONCE
                    cls._instance = instance
        return cls._instance
```

**Three traps, all of which the originals hit:**

1. **Guarding `__init__` instead of `__new__`.** The common textbook version
   raises from `__init__` and hands out instances from `get_instance()`. The
   very first direct `ParkingLot()` then succeeds and creates an object that is
   never registered — you silently have two lots.
2. **Real state in `__init__`.** `__init__` runs on *every* call, even when
   `__new__` returned the existing object, so it wipes the state every time
   somebody looks the singleton up. Put initialisation in a `_init_state()`
   that `__new__` calls once.
3. **Using it when the thing is not unique.** See below.

**⚠️ When NOT to use it.** A singleton is a global variable in a costume. It
makes tests order-dependent and makes "one per process" a permanent
architectural commitment. The chess `Game` in the original was a hard
singleton — fine for one desktop game, **wrong for a server** running thousands
of games. In [`08_chess_game`](../08_chess_game/) the constructor is public and
`get_instance()` is offered *separately*: convenience without the lock-in.

---

## Facade

**Problem it solves.** A subsystem with many moving parts, and callers who
should not have to know about any of them.

**Shape.** One class with a small, task-shaped API that delegates.

**In this repo**
- `EntryGate` / `ExitGate` — own **no** parking logic, which is exactly why you can add a fifth gate and change nothing else
- `LockerController` — lockers + OTP + notification behind `book_locker` / `pickup`
- `BookingService` — the whole two-phase seat protocol behind two methods
- `SplitwiseService`, `RateLimiterService`

**Tell.** If a caller has to call three of your objects in the right order to
get one thing done, that ordering belongs in a Facade.

---

## Command

**Problem it solves.** You need to undo an action, queue it, log it, or replay
it. A plain method call is not a thing you can hold.

**Shape.** An object with `execute()` and `undo()`.

```python
class Move:
    def execute(self):
        self.board.set_piece(self.target, self.piece)
        self.board.set_piece(self.origin, None)
        self.piece.has_moved = True

    def undo(self):
        self.board.set_piece(self.origin, self.promoted_from or self.piece)
        self.board.set_piece(self.target, self.captured)
        self.piece.has_moved = self.piece_had_moved   # ← restore EVERYTHING
```

**The rule that bites everyone:** `undo()` must restore **all** state
`execute()` touched, not just the obvious state. The original chess `undo()`
put the piece back but left `has_moved = True` forever.

**Undo/redo bookkeeping:** two stacks, and playing a *new* move clears the redo
stack. That is not an implementation detail — it is the semantics every editor
uses.

**In this repo:** [`08_chess_game`](../08_chess_game/) — `Move` + `MoveHistory`.
Note it is load-bearing beyond the undo button: legality testing works by
making a move, inspecting the position, and taking it back.

---

## Observer

**Problem it solves.** One event, an unknown number of interested parties.

**Shape.** The subject holds a list of listeners and fans out.

```python
with self._lock:
    appenders = list(self._appenders)   # snapshot under the lock
for appender in appenders:              # fan out OUTSIDE the lock
    appender.handle(record)
```

That snapshot-then-fan-out is worth copying: a slow listener must not block
other threads from publishing.

**In this repo:** [`04_logger_system`](../04_logger_system/) — `Logger` → N
`Appender`s.

---

## Decorator

**Problem it solves.** You want to add a behaviour (async, retry, compression,
encryption) to *any* implementation of an interface, without writing
`AsyncFile`, `AsyncConsole`, `AsyncNetwork`, …

**Shape.** A class that **is** the interface and **holds** the interface.

```python
class AsyncAppender(Appender):
    def __init__(self, inner: Appender):
        self._inner = inner        # ← holds one
```

So it composes with anything: `Async(File(...))`, `Async(Console(...))`.

**In this repo:** `AsyncAppender` in [`04_logger_system`](../04_logger_system/).

---

## Composite

**Problem it solves.** Callers should not have to know whether they hold one
thing or many.

**Shape.** Same as Decorator structurally — is-a and has-a — but holds *many*
and the intent is "treat a group like a single item".

```python
class MultiChannelNotification(NotificationService):
    def __init__(self, *channels): self._channels = channels
    def send_otp(self, customer, otp, locker_id):
        for channel in self._channels:
            channel.send_otp(customer, otp, locker_id)
```

**In this repo:** [`03_amazon_locker`](../03_amazon_locker/).

---

## Chain of Responsibility

**Problem it solves.** A request that must be handled by *one* of several
handlers, where **you cannot tell which one without asking each in turn**.

**Shape.** Each handler either handles the request or forwards it.

**Good fits:** approval limits (manager → director → VP), HTTP middleware,
exception dispatch.

**⚠️ Bad fit: log levels** — and this is the trap in
[`04_logger_system`](../04_logger_system/). Interviewers ask for CoR there, so
build it, then explain why real loggers don't use it:

1. O(n) walk to discover an O(1) comparison
2. levels are a **total order**, known up front — nothing to search
3. logging wants **fan-out**, but a chain stops at the first match
4. users configure `threshold = WARN`, not "rebuild the chain"

**Always give a chain a terminal case.** A chain whose tail silently swallows
unmatched requests is a debugging nightmare — your ERROR logs just don't
appear, and nothing says why.

---

## State machine

**Problem it solves.** Operations that are only legal in certain states. Almost
every bug in a naive implementation is *"an operation ran in a state where it
should have been impossible"*.

**Shape.** Name the states in an enum, and make every public method start by
checking the state.

```python
def insert_card(self, card_number):
    if self._state is not ATMState.IDLE:
        self._screen.show("machine busy")
        return
```

**Rule of thumb:** if you cannot draw the states on a napkin, the machine will
have unreachable and un-exitable corners.

**In this repo:** [`02_atm`](../02_atm/) — `ATMState`;
[`07_book_my_show`](../07_book_my_show/) — `SeatStatus` / `BookingStatus` /
`PaymentStatus`.

---

## Bonus: Dependency Injection (not a GoF pattern, more useful than most)

**A class receives its collaborators instead of constructing them.**

```python
def __init__(self, keypad=None, dispenser=None):
    self._keypad = keypad or PhysicalKeypad()       # default for production
    self._dispenser = dispenser or CashDispenser()  # override in tests
```

The original ATM called `input()` from inside `Keypad`, which made the whole
machine untestable and un-runnable in CI — the program just sat there waiting
for a human. One interface and a `ScriptedKeypad` fixed it.

**This is the single highest-leverage habit in LLD.** If you take one thing
from this repo, take this: *anything slow, external, random, or clock-dependent
gets injected.* `Keypad`, `CashDispenser`, `OTPGenerator`, `LockProvider`,
`PaymentGateway`, `NotificationService` — every one of them is injected here,
and every one of them is why the demos run hands-free.
