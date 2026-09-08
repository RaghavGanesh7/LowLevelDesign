# 06 · Splitwise

> **File:** [`splitwise.py`](splitwise.py) · **Run:** `python 06_splitwise/splitwise.py`
> **Difficulty:** ★★★★☆ — the only one here with a genuinely interesting algorithm.

> ⚠️ **Provenance:** the upstream Python file had the class skeleton but every
> interesting method was a stub — `def simplify_debt(self, group): pass` and
> `# TODO: update balance_sheet`. The data model existed; the behaviour did
> not. Both are implemented here.

---

## The problem

A group of friends share expenses. One person pays; the cost is split among
several. Track who owes whom, and settle up with as few transfers as possible.

## Requirements

| # | Requirement |
|---|---|
| R1 | Split EQUALLY, by EXACT amounts, or by PERCENTAGE |
| R2 | Maintain a running balance per person |
| R3 | Reject an expense whose splits do not add up |
| R4 | "Simplify debts": settle the group in the fewest transfers |
| R5 | Never lose a cent to rounding |

---

## Big idea 1: store net balances, not a debt graph

Do **not** store a graph of "A owes B 30, B owes C 30".

```mermaid
flowchart LR
    subgraph Bad["✗ Debt graph — O(n²) edges, updated on every expense"]
        A1((Alice)) -->|30| B1((Bob))
        B1 -->|30| C1((Carol))
        A1 -->|15| C1
        C1 -->|8| A1
    end
    subgraph Good["✓ Net balance vector — O(n), sums to zero"]
        A2["Alice  −53.34"]
        B2["Bob    −98.33"]
        C2["Carol +266.67"]
        D2["Dave  −115.00"]
    end
    Bad -.->|collapse| Good
```

**The invariant:** every net balance in a group always sums to **exactly
zero**. Money is never created or destroyed by an expense — it only moves.
That invariant is your assertion, your test, and your proof the books are
straight. It is a live `assert_books_balance()` in this file.

## Big idea 2: money is an integer number of cents

```python
>>> 0.1 + 0.2 == 0.3
False
```

Floats are binary fractions; `0.1` is not exactly representable, the same way
`1/3` is not exactly representable in decimal. Add a few thousand and your
books are off by a cent — the kind of bug found by an auditor, not by a test.

Every real payments system stores money as an **integer of the smallest unit**.
Two consequences in this file:

- Split validation is a plain `==`, with no epsilon fudge factor.
- `EqualSplit` hands the remainder out one cent at a time: 1000 ¢ / 3 people
  becomes 334 + 333 + 333, not 333 × 3 = 999 with a cent evaporated.

---

## Big idea 3: debt simplification

After the ledger runs, split everyone into **debtors** (net < 0) and
**creditors** (net > 0). Greedily pair the biggest of each:

```mermaid
flowchart TD
    S["Net balances<br/>A −53.34 · B −98.33 · C +266.67 · D −115.00"] --> H["Two max-heaps:<br/>debtors by |debt|, creditors by credit"]
    H --> P["Pop biggest debtor (D, 115.00)<br/>Pop biggest creditor (C, 266.67)"]
    P --> T["Transfer min(115.00, 266.67) = 115.00<br/>D pays C"]
    T --> U["D is now zero → leaves the heap<br/>C has 151.67 left → pushed back"]
    U --> Q{"Either heap empty?"}
    Q -- no --> P
    Q -- yes --> R["Done — at most n−1 transfers"]
    style R fill:#14532d,color:#fff
```

**Why it terminates quickly:** every iteration zeroes at least one person, so
with *n* people there are at most **n−1** transfers. The naive "pay back each
edge you owe" can need O(n²).

**Is it optimal? No — and say so.** Finding the true minimum number of
transfers requires partitioning the balances into the maximum number of
zero-sum subsets, which is **NP-hard** (it contains subset-sum). Greedy gives
≤ n−1 transfers, is O(n log n), and is what Splitwise itself ships.

> *"Greedy, n−1 transfers, exact optimum is NP-hard, here is the
> counter-example"* is a much stronger answer than claiming optimality.

---

## Architecture

```mermaid
classDiagram
    class SplitwiseService {
        <<Facade>>
        -_groups: Dict~str,Group~
        -_simplifier: DebtSimplificationService
        +create_group(name, members) Group
        +add_expense(...) Expense
        +settle_up(group_id) List~Settlement~
    }
    class Group {
        +members: Dict~str,User~
        +expenses: List~Expense~
        +balance_sheets: Dict~str,BalanceSheet~
        +record_expense(Expense)
        +net_balances() Dict~str,int~
        +assert_books_balance()
    }
    class BalanceSheet {
        +total_paid_cents: int
        +total_share_cents: int
        +owed_by: Dict~str,int~
        +net_cents: int
    }
    class Expense {
        +amount_cents: int
        +paid_by: User
        +splits: List~Split~
    }
    class Split {
        +user: User
        +amount_cents: int
    }
    class User {
        <<frozen>>
        +user_id: str
        +name: str
    }
    class Settlement {
        +payer: User
        +receiver: User
        +amount_cents: int
    }

    class SplitStrategy {
        <<abstract>>
        +split(total, participants, values) List~Split~
    }
    class EqualSplit
    class ExactSplit
    class PercentageSplit
    class SplitStrategyFactory
    class DebtSimplificationService {
        +simplify(Group) List~Settlement~
    }

    SplitwiseService "1" o-- "many" Group
    SplitwiseService --> DebtSimplificationService
    SplitwiseService ..> SplitStrategyFactory
    Group "1" *-- "many" BalanceSheet
    Group "1" o-- "many" Expense
    Expense "1" *-- "many" Split
    Split --> User
    SplitStrategy <|-- EqualSplit
    SplitStrategy <|-- ExactSplit
    SplitStrategy <|-- PercentageSplit
    SplitStrategyFactory ..> SplitStrategy : creates
    DebtSimplificationService ..> Settlement : produces
```

### Adding an expense

```mermaid
sequenceDiagram
    actor App
    participant S as SplitwiseService
    participant F as SplitStrategyFactory
    participant St as EqualSplit
    participant G as Group

    App->>S: add_expense("Dinner", 100.00, Alice, EQUAL, [A,B,C])
    S->>S: total_cents = to_cents(100.00) = 10000
    S->>F: get(EQUAL)
    F-->>S: EqualSplit
    S->>St: split(10000, [A,B,C])
    Note over St: divmod(10000, 3) = (3333, 1)<br/>first 1 person pays 1¢ extra
    St-->>S: [A:3334, B:3333, C:3333]
    S->>G: record_expense(expense)
    Note over G: Alice.total_paid += 10000<br/>each participant.total_share += share<br/>pairwise owed_by updated
    G-->>App: Expense E1
```

---

## Patterns used

| Pattern | Where | Why |
|---|---|---|
| **Strategy** | `SplitStrategy` + 3 subclasses | Each split rule validates its own input — a new type cannot forget to |
| **Factory** | `SplitStrategyFactory` | `SplitType` → strategy |
| **Facade** | `SplitwiseService` | The API the app calls; everything else is an implementation detail |

---

## What was implemented / fixed vs. the original

| Issue | Original | Here |
|---|---|---|
| **The core feature was a stub** | `def simplify_debt(self, group): pass` | Greedy max-heap settlement, ≤ n−1 transfers |
| **Ledger never updated** | `add_expense` had `# TODO: update balance_sheet` — the app tracked nothing | `Group.record_expense()` updates aggregate *and* pairwise books in one place |
| **`BalanceSheet` unused** | Declared, never populated | It is the ledger |
| **Only 2 split types, neither validated** | EQUAL / PERCENTAGE enum, no logic | 3 types, each validating; bad input raises with a message |
| **Float money** | `amount: float` throughout | Integer cents, with `to_cents` / `to_display` as the only boundary |
| **Bare `KeyError`** | `self.groups[group_id]` | `ValueError` naming the bad id |
| **No invariant check** | — | `assert_books_balance()`, called in the demo |

---

## Test yourself

1. Why net balances instead of a debt graph? Give the complexity of each.
2. 1000 ¢ split 3 ways. What does naive integer division lose, and how does `EqualSplit` avoid it?
3. Why does `ExactSplit` compare with `==` while `PercentageSplit` uses an epsilon? (Hint: one of them is money.)
4. Produce a 4-person balance set where greedy needs more transfers than the true optimum.
5. `heapq` is a min-heap. Trace the signs in `DebtSimplificationService.simplify` — why do debtors need no negation but creditors do?
6. Add "settle up partially: Bob pays Carol 50". Which classes change?

## Your notes

<!-- space for you -->
