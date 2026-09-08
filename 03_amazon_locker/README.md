# 03 · Amazon Locker

> **File:** [`amazon_locker.py`](amazon_locker.py) · **Run:** `python 03_amazon_locker/amazon_locker.py`
> **Difficulty:** ★★☆☆☆ — resource allocation with a twist.

> ⚠️ **Provenance:** the upstream repo's Python file for this problem was
> **empty**. This is a port of the C++ version, plus the fixes below.

---

## The problem

Design the pickup-locker system. A courier drops a package into a locker; the
customer gets a one-time code and later opens that locker to collect it.

## Requirements

| # | Requirement |
|---|---|
| R1 | Lockers come in sizes; a package must go in a locker it fits in |
| R2 | Booking generates a one-time code (OTP) and notifies the customer |
| R3 | Pickup requires the right code |
| R4 | Uncollected packages expire so the locker is not blocked forever |
| R5 | Notification channel (email / SMS / push) must be swappable |

---

## The one big idea: best fit, not exact fit

> Find the **smallest** locker the package fits in — not a locker of exactly
> that size.

Exact-match wastes capacity: a small package is refused while three large
lockers stand empty. This is the part the original got wrong, and it is the
interesting part of the problem.

```mermaid
flowchart TD
    A["Package arrives<br/>size = SMALL"] --> B{"Any AVAILABLE locker<br/>with size >= SMALL?"}
    B -- no --> C["Refuse — station full"]
    B -- yes --> D["Candidates:<br/>SMALL, MEDIUM, LARGE"]
    D --> E["Pick the SMALLEST candidate<br/>min by size"]
    E --> F["Occupy it,<br/>generate OTP,<br/>notify customer"]

    G["The original instead did:<br/>locker.size == package.size"] -.-> H["SMALL package refused<br/>while LARGE lockers sit empty"]

    style H fill:#7f1d1d,color:#fff
    style E fill:#14532d,color:#fff
```

Why *smallest* and not *first that fits*? Handing a LARGE locker to a SMALL
package when a SMALL one is free means the next large package gets refused.
Greedy best-fit avoids that.

---

## Architecture

```mermaid
classDiagram
    class LockerController {
        <<Facade>>
        -_station: LockerStation
        -_notifier: NotificationService
        -_otp_generator: OTPGenerator
        -_bookings: Dict~str,Booking~
        +book_locker(id, package, customer) Booking
        +pickup(booking_id, otp) bool
        +reclaim_expired() int
    }
    class LockerStation {
        -_lockers: List~Locker~
        +find_locker_for(package) Locker
    }
    class Locker {
        +locker_id: str
        +size: LockerSize
        +status: LockerStatus
        +fits(package) bool
        +occupy()
        +release()
    }
    class Booking {
        +otp: str
        +created_at: float
        +failed_attempts: int
        +is_expired(ttl) bool
    }
    class Package {
        +size: LockerSize
    }
    class Customer

    class OTPGenerator {
        <<abstract>>
        +generate() str
    }
    class RandomOTPGenerator
    class FixedOTPGenerator
    class OTPVerifier {
        +verify(expected, submitted)$ bool
    }

    class NotificationService {
        <<abstract>>
        +send_otp(customer, otp, locker_id)
    }
    class EmailNotification
    class SMSNotification
    class MultiChannelNotification

    LockerController --> LockerStation
    LockerController --> NotificationService
    LockerController --> OTPGenerator
    LockerController "1" o-- "many" Booking
    LockerStation "1" *-- "many" Locker
    Booking --> Locker
    Booking --> Package
    Booking --> Customer
    OTPGenerator <|-- RandomOTPGenerator
    OTPGenerator <|-- FixedOTPGenerator
    NotificationService <|-- EmailNotification
    NotificationService <|-- SMSNotification
    NotificationService <|-- MultiChannelNotification
    MultiChannelNotification o-- NotificationService : composite
```

### Booking and pickup

```mermaid
sequenceDiagram
    actor Courier
    participant C as LockerController
    participant S as LockerStation
    participant N as NotificationService
    actor Customer

    Courier->>C: book_locker(B1, package, customer)
    C->>S: find_locker_for(package)
    Note over S: smallest AVAILABLE locker<br/>with size >= package.size
    S-->>C: Locker L1
    C->>C: L1.occupy() and otp = generate()
    C->>N: send_otp(customer, otp, L1)
    N-->>Customer: "Locker L1, code 123456"

    Note over Customer: ...comes to the station...

    Customer->>C: pickup(B1, "000000")
    C-->>Customer: invalid — 2 attempts left
    Customer->>C: pickup(B1, "123456")
    C->>C: compare_digest OK → L1.release()
    C-->>Customer: locker opens
```

---

## Patterns used

| Pattern | Where | Why |
|---|---|---|
| **Strategy** | `NotificationService` | Controller does not care if it is email, SMS, or a log line |
| **Strategy** | `OTPGenerator` | Random in production, fixed in the demo/tests |
| **Composite** | `MultiChannelNotification` | *Is* a notifier and *holds* notifiers — caller cannot tell one from many |
| **Facade** | `LockerController` | One small API over lockers + OTP + notification |

---

## What was fixed vs. the original

| Issue | Original | Here |
|---|---|---|
| **Exact-size allocation** | `locker.size == package.size` | Best fit: smallest locker with `size >= package.size` |
| **Shared password** | `generateOTP()` returned the literal `"1234"` for everyone | `secrets.randbelow`, 6 digits, per booking |
| **Timing attack** | OTP compared with `==` (early-exits on first mismatch) | `secrets.compare_digest` — constant time |
| **Unlimited guesses** | No attempt cap | 3 attempts, then the booking locks |
| **Lockers blocked forever** | No expiry | `Booking.is_expired()` + `reclaim_expired()` sweep |
| **Silent overwrite** | Re-using a booking id clobbered the old one, orphaning an occupied locker | Duplicate ids are rejected |

---

## Test yourself

1. Why is `LockerSize` an `IntEnum` rather than an `Enum`? What code gets longer if you change it?
2. `find_locker_for` is O(n). Sketch the O(log n) version for a station with 10,000 lockers.
3. Why `secrets.compare_digest` instead of `==`? Describe the attack in one sentence.
4. `MultiChannelNotification` implements the same interface it holds. What is that pattern called, and where else in this repo does it appear?
5. Two couriers book the last SMALL locker at the same instant. What goes wrong, and what would you add? (Compare with `01_parking_lot`.)

## Your notes

<!-- space for you -->
