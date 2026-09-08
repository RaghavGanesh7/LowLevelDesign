"""
================================================================================
 SPLITWISE  --  Low Level Design
================================================================================

THE PROBLEM
-----------
A group of friends share expenses. One person pays; the cost is split among
several. Track who owes whom, and settle up with as few transfers as possible.

NOTE ON PROVENANCE
------------------
The upstream Python file had the class skeleton but every interesting method
was a stub:

    def simplify_debt(self, group): pass   # TODO
    def add_expense(...): ...              # TODO: update balance_sheet

So the data model existed and the behaviour did not. Both are implemented here.

REQUIREMENTS WE ARE DESIGNING FOR
---------------------------------
  R1. Split an expense EQUALLY, by EXACT amounts, or by PERCENTAGE.
  R2. Maintain a running balance per person.
  R3. Reject an expense whose splits do not add up to the total.
  R4. "Simplify debts": settle the group in the fewest transfers.
  R5. Never lose a cent to rounding.

DESIGN PATTERNS USED  (jump to the numbered tags in the code below)
-------------------------------------------------------------------
  [1] STRATEGY -- SplitStrategy: one class per way of dividing an amount.
  [2] FACTORY  -- SplitStrategyFactory: SplitType -> strategy.
  [3] FACADE   -- SplitwiseService: the API the app calls.

THE ONE BIG IDEA
----------------
Do NOT store a graph of "A owes B 30, B owes C 30". Store ONE NUMBER PER
PERSON: their net balance (positive = owed money, negative = owes money).

  - The graph is O(n^2) edges and needs updating on every expense.
  - The net-balance vector is O(n), and settling becomes a simple matching
    problem: repeatedly pair the biggest creditor with the biggest debtor.

Every net balance in a group always sums to zero. That invariant is your
assertion, your test, and your proof the books are straight.

RUN IT
------
    python 06_splitwise/splitwise.py
================================================================================
"""

import heapq
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Sequence, Tuple


# =============================================================================
#  SECTION 0 -- MONEY, AND WHY IT IS IN CENTS
# =============================================================================
# THE SINGLE MOST COMMON BUG IN THIS PROBLEM:
#
#     >>> 0.1 + 0.2 == 0.3
#     False
#
# Floats are binary fractions; 0.1 is not exactly representable, the same way
# 1/3 is not exactly representable in decimal. Add a few thousand of them and
# your books are off by a cent -- and a cent that appears from nowhere is the
# kind of bug that gets found by an auditor, not by a test.
#
# THE FIX, used by every real payments system: store money as an INTEGER
# number of the smallest unit (cents / paise). Integers are exact. Convert to
# a decimal string only for display.
#
# The two helpers below are the ONLY places rupees and cents meet.


def to_cents(amount: float) -> int:
    """Rupees (float, from the UI) -> cents (int, for storage and maths)."""
    return int(round(amount * 100))


def to_display(cents: int) -> str:
    """Cents -> a string a human reads. Handles the sign correctly."""
    sign = "-" if cents < 0 else ""
    magnitude = abs(cents)
    return f"{sign}{magnitude // 100}.{magnitude % 100:02d}"


# =============================================================================
#  SECTION 1 -- DOMAIN OBJECTS
# =============================================================================


class SplitType(Enum):
    EQUAL = auto()
    EXACT = auto()
    PERCENTAGE = auto()


@dataclass(frozen=True)
class User:
    """
    frozen=True makes User hashable, so it can be a dict key.

    But note that the balance sheet below is keyed by user_id (a str), not by
    User. Keying domain maps by a stable ID rather than by the object means a
    user renaming themselves does not orphan their balance.
    """

    user_id: str
    name: str

    def __str__(self) -> str:
        return self.name


@dataclass
class Split:
    """One person's share of one expense, in cents."""

    user: User
    amount_cents: int


@dataclass
class Expense:
    expense_id: str
    description: str
    amount_cents: int
    paid_by: User
    split_type: SplitType
    splits: List[Split]

    def __str__(self) -> str:
        return (
            f"{self.description} ({to_display(self.amount_cents)}) "
            f"paid by {self.paid_by.name}"
        )


@dataclass
class BalanceSheet:
    """
    One person's position in one group.

    net_cents is the number that matters:
       > 0  the group owes this person
       < 0  this person owes the group
      == 0  square

    `owed_by` is the per-counterparty detail, kept ONLY so the UI can show
    "you owe Bob 30". Settlement never reads it -- that runs off net_cents.
    Detail for humans, aggregate for algorithms.
    """

    total_paid_cents: int = 0
    total_share_cents: int = 0
    owed_by: Dict[str, int] = field(default_factory=dict)

    @property
    def net_cents(self) -> int:
        return self.total_paid_cents - self.total_share_cents


# =============================================================================
#  SECTION 2 -- [1] STRATEGY: how to divide an amount
# =============================================================================


class SplitStrategy(ABC):
    """
    Turns "500 rupees among these 3 people" into a concrete list of shares.

    Each strategy VALIDATES its own input. Putting validation inside the
    strategy rather than in the service means a new split type cannot forget
    to validate -- the interface forces the question.
    """

    @abstractmethod
    def split(
        self,
        total_cents: int,
        participants: Sequence[User],
        values: Optional[Sequence[float]] = None,
    ) -> List[Split]:
        """`values` means exact amounts or percentages, depending on subclass."""


class EqualSplit(SplitStrategy):
    """
    Divide evenly.

    THE REMAINDER PROBLEM -- the whole reason this class is more than one line:
      1000 cents among 3 people is 333.33... cents each. Integer division gives
      333, and 333 * 3 = 999. One cent has evaporated.

      Over a year of expenses those cents add up, the group's net balances stop
      summing to zero, and the invariant that proves your books are correct is
      broken.

    THE FIX: hand the remainder out, one cent at a time, to the first R people.
    Somebody pays a cent more; nothing is lost. Which people get the extra cent
    is arbitrary but must be DETERMINISTIC -- otherwise re-computing the same
    expense gives different answers.
    """

    def split(
        self,
        total_cents: int,
        participants: Sequence[User],
        values: Optional[Sequence[float]] = None,
    ) -> List[Split]:
        if not participants:
            raise ValueError("cannot split among nobody")

        count = len(participants)
        base, remainder = divmod(total_cents, count)

        # The first `remainder` participants pay one extra cent.
        return [
            Split(user, base + (1 if index < remainder else 0))
            for index, user in enumerate(participants)
        ]


class ExactSplit(SplitStrategy):
    """Caller states each share. We only check that they add up."""

    def split(
        self,
        total_cents: int,
        participants: Sequence[User],
        values: Optional[Sequence[float]] = None,
    ) -> List[Split]:
        if values is None or len(values) != len(participants):
            raise ValueError("EXACT split needs one amount per participant")

        shares = [to_cents(value) for value in values]

        # R3: the splits must reconstruct the total, exactly. Because we are
        # in integer cents this is a plain ==, with no epsilon fudge factor.
        # That is the second payoff of Section 0.
        if sum(shares) != total_cents:
            raise ValueError(
                f"splits total {to_display(sum(shares))} "
                f"but the expense is {to_display(total_cents)}"
            )

        return [Split(user, share) for user, share in zip(participants, shares)]


class PercentageSplit(SplitStrategy):
    """
    Caller states each share as a percentage. Percentages must total 100.

    Same remainder problem as EqualSplit -- 33.33% of 1000 is 333.3 cents --
    so the LAST participant absorbs whatever is left over. That guarantees the
    shares sum to the total no matter how the percentages round.
    """

    def split(
        self,
        total_cents: int,
        participants: Sequence[User],
        values: Optional[Sequence[float]] = None,
    ) -> List[Split]:
        if values is None or len(values) != len(participants):
            raise ValueError("PERCENTAGE split needs one percentage per participant")

        # Percentages come from a UI as floats, so compare with a tolerance
        # here -- this is a percentage, not money. Money never gets an epsilon.
        if abs(sum(values) - 100.0) > 1e-6:
            raise ValueError(f"percentages must sum to 100, got {sum(values)}")

        shares: List[int] = []
        for percentage in values[:-1]:
            shares.append(int(round(total_cents * percentage / 100.0)))
        shares.append(total_cents - sum(shares))  # last one soaks up the rest

        return [Split(user, share) for user, share in zip(participants, shares)]


class SplitStrategyFactory:
    """[2] FACTORY -- SplitType in, strategy out."""

    _REGISTRY = {
        SplitType.EQUAL: EqualSplit,
        SplitType.EXACT: ExactSplit,
        SplitType.PERCENTAGE: PercentageSplit,
    }

    @classmethod
    def get(cls, split_type: SplitType) -> SplitStrategy:
        return cls._REGISTRY[split_type]()


# =============================================================================
#  SECTION 3 -- THE GROUP AND ITS LEDGER
# =============================================================================


@dataclass
class Settlement:
    """One suggested transfer: `payer` should send `amount` to `receiver`."""

    payer: User
    receiver: User
    amount_cents: int

    def __str__(self) -> str:
        return (
            f"{self.payer.name} pays {self.receiver.name} "
            f"{to_display(self.amount_cents)}"
        )


class Group:
    """A set of people and the expenses between them."""

    def __init__(self, group_id: str, name: str, members: Sequence[User]):
        self.group_id = group_id
        self.name = name
        self.members: Dict[str, User] = {user.user_id: user for user in members}
        self.expenses: List[Expense] = []
        self.balance_sheets: Dict[str, BalanceSheet] = {
            user.user_id: BalanceSheet() for user in members
        }

    def add_member(self, user: User) -> None:
        if user.user_id in self.members:
            return  # idempotent: re-adding is a no-op, not an error
        self.members[user.user_id] = user
        self.balance_sheets[user.user_id] = BalanceSheet()

    def record_expense(self, expense: Expense) -> None:
        """
        Apply an expense to the ledger.

        THIS IS THE METHOD THE ORIGINAL LEFT AS `# TODO: update balance_sheet`,
        which meant the whole app tracked nothing.

        Two books are updated:
          1. the AGGREGATE (total_paid / total_share) -- what settlement uses
          2. the PAIRWISE detail (owed_by) -- what the UI shows

        Both are kept in sync here, in one place, so they cannot drift.
        """
        payer_id = expense.paid_by.user_id
        if payer_id not in self.members:
            raise ValueError(f"{expense.paid_by.name} is not in {self.name}")

        self.balance_sheets[payer_id].total_paid_cents += expense.amount_cents

        for split in expense.splits:
            participant_id = split.user.user_id
            if participant_id not in self.members:
                raise ValueError(f"{split.user.name} is not in {self.name}")

            self.balance_sheets[participant_id].total_share_cents += split.amount_cents

            # The payer's own share is not a debt to themselves.
            if participant_id == payer_id:
                continue

            # participant owes payer
            self.balance_sheets[participant_id].owed_by[payer_id] = (
                self.balance_sheets[participant_id].owed_by.get(payer_id, 0)
                + split.amount_cents
            )
            # and symmetrically, payer is owed by participant
            self.balance_sheets[payer_id].owed_by[participant_id] = (
                self.balance_sheets[payer_id].owed_by.get(participant_id, 0)
                - split.amount_cents
            )

        self.expenses.append(expense)

    def net_balances(self) -> Dict[str, int]:
        """user_id -> net cents. The vector everything downstream runs on."""
        return {
            user_id: sheet.net_cents
            for user_id, sheet in self.balance_sheets.items()
        }

    def assert_books_balance(self) -> None:
        """
        THE INVARIANT: net balances always sum to zero.

        Money is never created or destroyed by an expense -- it only moves.
        If this ever fails you have a rounding bug (Section 0) or a missing
        ledger update. Keeping the check in the code, and calling it in the
        demo, is how you find that on day one instead of at month end.
        """
        total = sum(self.net_balances().values())
        if total != 0:
            raise AssertionError(
                f"books do not balance: net sum is {total} cents, expected 0"
            )


# =============================================================================
#  SECTION 4 -- DEBT SIMPLIFICATION
# =============================================================================


class DebtSimplificationService:
    """
    Settle the group in as few transfers as possible.

    THIS WAS `pass` IN THE ORIGINAL. It is the most interesting algorithm in
    the problem, so it is worth doing properly.

    THE SETUP:
      After Section 3 every member has one net number. Split them into
      DEBTORS (net < 0) and CREDITORS (net > 0). The sums of the two sides are
      equal in magnitude, by the invariant above.

    THE ALGORITHM (greedy max-debtor / max-creditor):
      1. Put debtors in a max-heap by how much they owe, creditors likewise.
      2. Pop the biggest of each. Transfer min(|debt|, credit).
      3. At least one of the two is now settled and leaves the heap. Push the
         other back with its reduced amount.
      4. Repeat until one heap is empty.

    WHY IT TERMINATES QUICKLY:
      Every iteration zeroes at least one person, so with n people there are at
      most n-1 transfers. Compare with the naive "pay back each edge you owe",
      which can need O(n^2) transfers.

    IS IT OPTIMAL?
      Not always. Finding the true minimum number of transfers requires
      partitioning the balances into the maximum number of zero-sum subsets,
      which is NP-hard (it contains subset-sum). Greedy gives you <= n-1
      transfers, is O(n log n), and is what Splitwise itself ships.

      SAY THIS OUT LOUD IN AN INTERVIEW. "Greedy, n-1 transfers, exact optimum
      is NP-hard, here is the counter-example" is a much stronger answer than
      claiming optimality.

    THE COUNTER-EXAMPLE, so you can produce it on demand:
      balances A=-5, B=+5, C=-5, D=+5.
      Optimum is 2 transfers: A->B and C->D.
      Greedy pops the largest of each -- ties broken arbitrarily -- and can
      pick A->D (5), then C->B (5). Also 2 transfers here, but with balances
      like A=-3, B=-2, C=+5, D=-5, E=+5 the tie-breaking can cost you an extra
      hop. Greedy never does WORSE than n-1; it just is not guaranteed minimal.

    ON heapq:
      Python's heapq is a MIN-heap only. The standard trick for a max-heap is
      to push NEGATED keys. Debtors are already negative, which makes the code
      read oddly -- the comments below track the signs carefully.
    """

    def simplify(self, group: "Group") -> List[Settlement]:
        group.assert_books_balance()

        # Min-heap of (net, user_id). Debtor nets are negative, so the most
        # negative -- the biggest debtor -- sorts first. Exactly what we want.
        debtors: List[Tuple[int, str]] = []
        # For creditors we want the LARGEST first, so push the negated net.
        creditors: List[Tuple[int, str]] = []

        for user_id, net in group.net_balances().items():
            if net < 0:
                heapq.heappush(debtors, (net, user_id))
            elif net > 0:
                heapq.heappush(creditors, (-net, user_id))
            # net == 0: already square, ignore entirely

        settlements: List[Settlement] = []

        while debtors and creditors:
            debt, debtor_id = heapq.heappop(debtors)        # debt is negative
            credit_neg, creditor_id = heapq.heappop(creditors)
            credit = -credit_neg                             # back to positive

            transfer = min(-debt, credit)
            settlements.append(
                Settlement(
                    payer=group.members[debtor_id],
                    receiver=group.members[creditor_id],
                    amount_cents=transfer,
                )
            )

            # Whoever still has a balance goes back on their heap. At least one
            # of these two is now exactly zero, which is why we terminate.
            remaining_debt = debt + transfer
            remaining_credit = credit - transfer
            if remaining_debt < 0:
                heapq.heappush(debtors, (remaining_debt, debtor_id))
            if remaining_credit > 0:
                heapq.heappush(creditors, (-remaining_credit, creditor_id))

        return settlements


# =============================================================================
#  SECTION 5 -- [3] FACADE
# =============================================================================


class SplitwiseService:
    """The API the app talks to. Everything above is an implementation detail."""

    def __init__(self) -> None:
        self._groups: Dict[str, Group] = {}
        self._simplifier = DebtSimplificationService()
        self._expense_counter = 0

    def create_group(self, name: str, members: Sequence[User]) -> Group:
        group_id = f"G{len(self._groups) + 1}"
        group = Group(group_id, name, members)
        self._groups[group_id] = group
        return group

    def get_group(self, group_id: str) -> Group:
        # FIXED: the original did self.groups[group_id] with no guard, so a
        # typo'd id raised a bare KeyError with no context. Say what was wrong.
        try:
            return self._groups[group_id]
        except KeyError:
            raise ValueError(f"no such group: {group_id!r}") from None

    def add_expense(
        self,
        group_id: str,
        description: str,
        amount: float,
        paid_by: User,
        split_type: SplitType,
        participants: Sequence[User],
        values: Optional[Sequence[float]] = None,
    ) -> Expense:
        """
        The one call the UI makes. Note how thin it is: pick a strategy, ask
        it to split, hand the result to the group. All the rules live in the
        strategy and the group, not here.
        """
        group = self.get_group(group_id)
        total_cents = to_cents(amount)
        if total_cents <= 0:
            raise ValueError("expense amount must be positive")

        strategy = SplitStrategyFactory.get(split_type)
        splits = strategy.split(total_cents, participants, values)

        self._expense_counter += 1
        expense = Expense(
            expense_id=f"E{self._expense_counter}",
            description=description,
            amount_cents=total_cents,
            paid_by=paid_by,
            split_type=split_type,
            splits=splits,
        )
        group.record_expense(expense)
        return expense

    def show_balances(self, group_id: str) -> None:
        group = self.get_group(group_id)
        print(f"  balances in {group.name}:")
        for user_id, net in sorted(group.net_balances().items()):
            user = group.members[user_id]
            if net > 0:
                print(f"    {user.name:<8} is owed  {to_display(net)}")
            elif net < 0:
                print(f"    {user.name:<8} owes     {to_display(-net)}")
            else:
                print(f"    {user.name:<8} is square")

    def settle_up(self, group_id: str) -> List[Settlement]:
        return self._simplifier.simplify(self.get_group(group_id))


# =============================================================================
#  SECTION 6 -- RUNNABLE DEMO
# =============================================================================


def _demo() -> None:
    alice = User("u1", "Alice")
    bob = User("u2", "Bob")
    carol = User("u3", "Carol")
    dave = User("u4", "Dave")

    service = SplitwiseService()
    trip = service.create_group("Goa Trip", [alice, bob, carol, dave])

    print("=" * 70)
    print("SCENARIO 1: EQUAL split, and the remainder cent")
    print("=" * 70)
    expense = service.add_expense(
        trip.group_id, "Dinner", 100.00, alice, SplitType.EQUAL,
        [alice, bob, carol],
    )
    print(f"  {expense}")
    print("  shares: " + ", ".join(
        f"{s.user.name}={to_display(s.amount_cents)}" for s in expense.splits
    ))
    print(f"  they sum to {to_display(sum(s.amount_cents for s in expense.splits))} "
          f"-- exactly the total. 100.00 / 3 does not divide evenly, so Alice")
    print("  pays the extra cent. Nothing is lost.")

    print()
    print("=" * 70)
    print("SCENARIO 2: EXACT and PERCENTAGE splits")
    print("=" * 70)
    service.add_expense(
        trip.group_id, "Taxi", 60.00, bob, SplitType.EXACT,
        [alice, bob, dave], values=[20.00, 25.00, 15.00],
    )
    print("  Taxi 60.00 by exact amounts: Alice 20, Bob 25, Dave 15")

    percentage_expense = service.add_expense(
        trip.group_id, "Hotel", 400.00, carol, SplitType.PERCENTAGE,
        [alice, bob, carol, dave], values=[25.0, 25.0, 25.0, 25.0],
    )
    print("  Hotel 400.00 split 25% each: " + ", ".join(
        f"{s.user.name}={to_display(s.amount_cents)}"
        for s in percentage_expense.splits
    ))

    print()
    print("=" * 70)
    print("SCENARIO 3: the ledger, and the invariant")
    print("=" * 70)
    service.show_balances(trip.group_id)
    trip.assert_books_balance()
    print(f"  net balances sum to {sum(trip.net_balances().values())} cents -- "
          f"the books balance.")

    print()
    print("=" * 70)
    print("SCENARIO 4: settling up in the fewest transfers")
    print("=" * 70)
    settlements = service.settle_up(trip.group_id)
    debtor_count = sum(1 for n in trip.net_balances().values() if n != 0)
    for settlement in settlements:
        print(f"    {settlement}")
    print(f"  {len(settlements)} transfer(s) for {debtor_count} non-square "
          f"people (upper bound is n-1 = {debtor_count - 1})")

    print()
    print("=" * 70)
    print("SCENARIO 5: bad input is rejected, not silently absorbed")
    print("=" * 70)
    for label, kwargs in [
        ("exact splits that don't add up", dict(
            split_type=SplitType.EXACT, participants=[alice, bob],
            values=[10.0, 10.0], amount=50.0)),
        ("percentages that don't reach 100", dict(
            split_type=SplitType.PERCENTAGE, participants=[alice, bob],
            values=[30.0, 30.0], amount=50.0)),
        ("a negative expense", dict(
            split_type=SplitType.EQUAL, participants=[alice, bob],
            values=None, amount=-10.0)),
    ]:
        try:
            service.add_expense(
                trip.group_id, "bad", paid_by=alice, **kwargs  # type: ignore[arg-type]
            )
            print(f"    {label}: NOT REJECTED (this would be a bug)")
        except ValueError as error:
            print(f"    {label}: rejected -- {error}")

    print()
    print("=" * 70)
    print("SCENARIO 6: floats would have lost money here; cents do not")
    print("=" * 70)
    pennies = SplitwiseService()
    penny_group = pennies.create_group("Cents", [alice, bob, carol])
    for _ in range(1000):
        pennies.add_expense(
            penny_group.group_id, "coffee", 0.10, alice,
            SplitType.EQUAL, [alice, bob, carol],
        )
    penny_group.assert_books_balance()
    print("  1000 expenses of 0.10 split three ways.")
    print(f"  net sum: {sum(penny_group.net_balances().values())} cents (must be 0)")
    print("  In float arithmetic 0.10/3 repeated 1000 times drifts off zero;")
    print("  in integer cents it cannot.")


if __name__ == "__main__":
    _demo()
