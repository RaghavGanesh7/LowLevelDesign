# SOLID, with examples from this repo

Five principles, each shown as **the bad version** and **the version in this
repo**. Skip the acronym-recital; the point is recognising the smell.

---

## S — Single Responsibility

> A class should have one reason to change.

**The test:** describe the class in one sentence. If you need an "and", split it.

### ✗ Bad

```python
class Logger:
    def log(self, level, message):
        if level < self.threshold: return
        line = f"{time.time()} {level} {message}"   # formatting
        with open("app.log", "a") as f:             # file handling
            f.write(line + "\n")
        if level == "ERROR":
            requests.post(PAGER_URL, ...)           # alerting
```

Three reasons to change: the layout, the destination, the alerting rule.

### ✓ In this repo — [`04_logger_system`](../04_logger_system/)

- `LogRecord` — holds the event
- `Formatter` — turns a record into text
- `Appender` — puts text somewhere
- `Logger` — decides whether to emit and fans out

Four classes, four sentences, no "and".

**Also:** `EntryGate` in [`01_parking_lot`](../01_parking_lot/) owns *no*
parking logic — it forwards to the lot. That is why you can add a fifth gate
and change nothing else.

---

## O — Open/Closed

> Open to extension, closed to modification.

**The test:** to add the next variant of this thing, do you *add* a file or
*edit* one?

### ✗ Bad

```python
def calculate_fare(vehicle_type, entry, exit, is_subscriber, is_weekend, is_ev):
    if is_subscriber: return 0
    elif is_weekend: ...
    elif is_ev: ...
    else: ...
```

Every new rule edits this function, and every edit risks the existing rules.

### ✓ In this repo — [`01_parking_lot`](../01_parking_lot/)

`FreeFirstHourPricing` was added to that file **without changing a single line
of `ParkingLot`, `HourlyPricing` or `PriceFactory`**. That is the whole claim,
and it is checkable.

Same shape in [`05_rate_limiter`](../05_rate_limiter/): a fifth algorithm is a
new class plus one dict entry.

---

## L — Liskov Substitution

> A subclass must be usable anywhere its base class is, without surprises.

**The test:** can a caller holding the base type be *wrong* about what happens?

### ✗ Bad

```python
class ReadOnlyAppender(Appender):
    def append(self, record):
        raise NotImplementedError("this appender doesn't append")
```

Now `for appender in appenders: appender.handle(record)` explodes on one of
them. The subclass narrowed the contract.

### ✓ In this repo — [`08_chess_game`](../08_chess_game/)

Every `Piece` subclass returns a list of positions from
`pseudo_legal_moves()` — possibly empty, never an exception, never `None`.
`Game.all_legal_moves()` iterates all six types without knowing which is which.

**The subtle one:** `Pawn` overrides `attacked_squares()` to return something
*different* from its move list. That is **not** an LSP violation — the base
contract is "squares this piece attacks", and the pawn genuinely attacks
different squares than it moves to. The base class documents that the default
implementation is a convenience, not the definition. Getting this backwards
(using the move list for attack detection) is a real chess-engine bug.

---

## I — Interface Segregation

> No client should depend on methods it does not use.

**The test:** does any implementer have a method that makes no sense for it?

### ✗ Bad

```python
class LockProvider(ABC):
    def try_lock(...): ...
    def unlock(...): ...
    def is_locked(...): ...
    def flush_all(...): ...        # only Redis has this
    def get_cluster_stats(...): ...# only Redis has this
```

`InMemoryLockProvider` now has two methods it must stub out.

### ✓ In this repo — [`07_book_my_show`](../07_book_my_show/)

`LockProvider` has exactly three methods, all of which both implementations
genuinely need. `PaymentGateway` has exactly one.

Small interfaces are also what make test doubles cheap: `AlwaysFailsGateway`
is four lines because `PaymentGateway` is one method.

---

## D — Dependency Inversion

> Depend on abstractions, not concretions. High-level modules should not depend
> on low-level ones.

**The test:** can you run this class without its slow/external/random
collaborator?

### ✗ Bad — this is what the original ATM did

```python
class Keypad:
    def get_pin(self):
        return input("PIN: ")      # hard-wired to a human at a terminal

class ATM:
    def __init__(self):
        self._keypad = Keypad()    # hard-wired to that
```

The whole ATM is untestable and un-runnable in CI. The program just hangs.

### ✓ In this repo — [`02_atm`](../02_atm/)

```python
class Keypad(ABC):                     # abstraction
    @abstractmethod
    def get_pin(self) -> str: ...

class PhysicalKeypad(Keypad): ...      # production
class ScriptedKeypad(Keypad): ...      # tests and the demo

class ATM:
    def __init__(self, keypad=None, dispenser=None):
        self._keypad = keypad or PhysicalKeypad()   # injected, with a default
```

`python 02_atm/atm.py` now runs five scenarios end to end with no typing.

**Everything injected in this repo:** `Keypad`, `CashDispenser`,
`OTPGenerator`, `NotificationService`, `LockProvider`, `PaymentGateway`,
`Formatter`, `Appender`, `PricingStrategy`, `SplitStrategy`.

> **Rule of thumb:** anything **slow, external, random, or clock-dependent**
> gets injected. That single habit is worth more than the other four
> principles combined, because it is the one that makes your design *testable*
> — and untestable designs rot.

---

## A shorter version of all five

| Principle | The question to ask |
|---|---|
| **S** | Can I describe this class without saying "and"? |
| **O** | To add the next variant, do I add a file or edit one? |
| **L** | Can a caller holding the base type be surprised? |
| **I** | Does any implementer stub out a method it doesn't need? |
| **D** | Can I run this without the database / network / clock / human? |
