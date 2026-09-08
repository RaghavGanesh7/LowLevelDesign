# 02 · ATM

> **File:** [`atm.py`](atm.py) · **Run:** `python 02_atm/atm.py`
> **Difficulty:** ★★☆☆☆ — the classic state-machine question.

---

## The problem

Design the software inside an ATM: insert card, enter PIN, withdraw cash or
check balance, take the card back.

## Requirements

| # | Requirement |
|---|---|
| R1 | Card in → PIN → menu → card out, never stuck half-way |
| R2 | Three wrong PINs and the machine swallows the card |
| R3 | Withdrawal fails cleanly if the account **or** the machine is short — and never debits money it did not hand out |
| R4 | New transaction types must be addable without rewriting the ATM |

---

## The one big idea: it is a state machine

Almost every bug in a naive ATM is *"an operation ran in a state where it
should have been impossible"*. Write the states down first; make every public
method start by checking the state.

```mermaid
stateDiagram-v2
    [*] --> IDLE
    IDLE --> CARD_INSERTED : insert_card()<br/>card is valid
    IDLE --> IDLE : insert_card()<br/>unknown card → eject
    CARD_INSERTED --> AUTHENTICATED : correct PIN
    CARD_INSERTED --> CARD_INSERTED : wrong PIN<br/>(< 3 attempts)
    CARD_INSERTED --> IDLE : 3 wrong PINs<br/>retain + block card
    AUTHENTICATED --> AUTHENTICATED : withdraw / balance
    AUTHENTICATED --> IDLE : choose Exit<br/>eject card, clear session
```

---

## Architecture

```mermaid
classDiagram
    class ATM {
        -_state: ATMState
        -_card: Card
        -_keypad: Keypad
        -_dispenser: CashDispenser
        +insert_card(card_number)
        -_authenticate()
        -_serve()
        -_reset()
    }
    class BankServer {
        <<Singleton>>
        +verify_pin(card, pin) bool
        +get_account(acct_no) Account
        +block_card(card)
    }
    class Account {
        -_balance: float
        -_lock: Lock
        +debit(amount) bool
        +credit(amount)
    }
    class Card {
        +card_number: str
        +account_no: str
        +is_valid() bool
    }

    class Keypad {
        <<abstract>>
        +get_pin() str
        +get_amount() float
        +get_choice() int
    }
    class PhysicalKeypad
    class ScriptedKeypad

    class CashDispenser {
        -_cash: float
        +reserve(amount) bool
        +release(amount)
        +dispense(amount)
    }

    class Transaction {
        <<abstract>>
        +execute() bool
    }
    class Withdrawal
    class BalanceEnquiry

    ATM --> Card
    ATM --> Keypad : injected
    ATM --> CashDispenser : injected
    ATM ..> Transaction : creates
    Keypad <|-- PhysicalKeypad
    Keypad <|-- ScriptedKeypad
    Transaction <|-- Withdrawal
    Transaction <|-- BalanceEnquiry
    Withdrawal --> CashDispenser
    Transaction ..> BankServer
    BankServer "1" *-- "many" Account
```

### Withdrawal: the ordering that matters

The naive order loses money. Read this diagram carefully — it is the point of
the whole design.

```mermaid
sequenceDiagram
    participant W as Withdrawal
    participant D as CashDispenser
    participant A as Account

    W->>D: reserve(2000)
    alt machine is short
        D-->>W: False
        Note over W: STOP. Account never touched.
    else notes earmarked
        D-->>W: True
        W->>A: debit(2000)
        alt insufficient funds
            A-->>W: False
            W->>D: release(2000)
            Note over W,D: Rolled back.<br/>Neither side changed.
        else debited
            A-->>W: True
            W->>D: dispense(2000)
            Note over W,D: Both sides committed.
        end
    end
```

**Why not just check-then-pay?**

```python
if dispenser.has(amount):     # 1. check
    if account.debit(amount): # 2. take the money
        dispenser.dispense()  # 3. hand over notes
```

Between 1 and 3 anything can go wrong — another transaction takes the last
notes, the cassette jams, the power dies. The account is already debited and
the customer has no cash. Reserve-first makes the failure recoverable.

---

## Patterns used

| Pattern | Where | Why |
|---|---|---|
| **State machine** | `ATMState` | Illegal operations become impossible, not just unlikely |
| **Singleton** | `BankServer` | One source of account truth |
| **Strategy / Command** | `Transaction` subclasses | Adding `Deposit` = adding a class |
| **Dependency injection** | `Keypad`, `CashDispenser` passed in | Makes the ATM testable and the demo runnable without typing |

---

## What was fixed vs. the original

| Issue | Original | Here |
|---|---|---|
| **Unrunnable demo** | `Keypad` called `input()` directly — the program just hung | `Keypad` is an interface; `ScriptedKeypad` replays keystrokes |
| **Money can vanish** | Debited the account, *then* dispensed, with no rollback | `reserve → debit → release-on-failure → dispense` |
| **No rollback possible** | `Account` had no `credit()` | Added, plus a lock |
| **Dead safety net** | `Card.is_valid()` existed but was never called | Wired into `insert_card()` |
| **Crash on typo** | `float(input(...))` with no guard | Re-prompts on bad input |
| **Session leak** | Previous customer's `Card` left on the machine after eject | `_reset()` clears session state |
| **Blocked card not blocked** | Card was "retained" but still worked | `BankServer.block_card()` actually blocks it |

---

## Test yourself

1. Why does `reserve()` come *before* `debit()` and not after?
2. `_authenticate()` calls `_serve()` on success. Draw the call stack for a full session. What would you change to flatten it?
3. Add a `Deposit` transaction. Which existing classes change?
4. Two ATMs share one `BankServer` and both withdraw from account ACC1 at the same instant. What protects the balance?
5. The dispenser tracks a total rupee amount. How would you change it to track denominations, and which method signatures change?

## Your notes

<!-- space for you -->
