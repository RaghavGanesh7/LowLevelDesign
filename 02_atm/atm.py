"""
================================================================================
 ATM  --  Low Level Design
================================================================================

THE PROBLEM
-----------
Design the software inside an ATM: insert card, enter PIN, withdraw cash or
check balance, take the card back.

REQUIREMENTS WE ARE DESIGNING FOR
---------------------------------
  R1. Card in -> PIN check -> menu -> card out. The machine must never get
      stuck in a half-way state.
  R2. Three wrong PINs and the machine swallows the card.
  R3. Withdrawal must fail cleanly if the account is short OR the machine is
      short of cash -- and must never debit money it did not hand out.
  R4. New transaction types (deposit, mini-statement) must be addable without
      rewriting the ATM.

DESIGN PATTERNS USED  (jump to the numbered tags in the code below)
-------------------------------------------------------------------
  [1] STATE MACHINE       -- ATMState: legal operations depend on where we are.
  [2] SINGLETON           -- BankServer: one shared source of account truth.
  [3] STRATEGY / COMMAND  -- Transaction: one class per transaction type.
  [4] DEPENDENCY INJECTION -- hardware is passed in, not hard-wired.

THE ONE BIG IDEA
----------------
An ATM is a STATE MACHINE wearing a metal box. Almost every bug in a naive
implementation is "an operation ran in a state where it should have been
impossible". Write the states down first, then make every public method start
by checking the state.

        IDLE  --insert_card-->  CARD_INSERTED  --correct PIN-->  AUTHENTICATED
         ^                            |                                |
         |                       3 wrong PINs                     eject card
         +----------------------------+--------------------------------+

RUN IT
------
    python 02_atm/atm.py

    It runs to completion with no typing required -- the keypad is injected
    (see [4]), so the demo feeds it a scripted set of keystrokes.
================================================================================
"""

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, List, Optional


# =============================================================================
#  SECTION 1 -- [1] THE STATE MACHINE
# =============================================================================


class ATMState(Enum):
    """
    Every state the machine can be in. Keeping this tiny is the point: if you
    cannot draw the states on a napkin, the machine will have unreachable and
    un-exitable corners.
    """

    IDLE = auto()  # no card, waiting for a customer
    CARD_INSERTED = auto()  # card read, PIN not yet verified
    AUTHENTICATED = auto()  # PIN accepted, customer may transact


# =============================================================================
#  SECTION 2 -- DOMAIN OBJECTS
# =============================================================================


@dataclass
class Card:
    """
    The plastic. Note it carries the ACCOUNT NUMBER, not the balance -- the
    card is an identifier, the bank is the source of truth. Putting a balance
    on the card would mean two copies of the same fact, which is how money
    goes missing.
    """

    card_number: str = ""
    account_no: str = ""

    def is_valid(self) -> bool:
        """
        Cheap sanity check before we bother the bank.

        FIXED: in the original this method existed but was never called -- dead
        code that looks like a safety net and isn't one. It is now actually
        used by ATM.insert_card().
        """
        return bool(self.card_number) and bool(self.account_no)


class Account:
    """
    A bank account.

    `_balance` is deliberately private (leading underscore) and only reachable
    through debit/credit. If callers could do `account._balance -= x` directly
    there would be no single place to add overdraft rules, audit logging, or
    the lock below.
    """

    def __init__(self, account_no: str, balance: float):
        self.account_no = account_no
        self._balance = balance
        self._lock = threading.Lock()  # a real bank has concurrent access

    def get_balance(self) -> float:
        with self._lock:
            return self._balance

    def debit(self, amount: float) -> bool:
        """Take money out. Returns False (and changes nothing) if short."""
        with self._lock:
            if amount <= 0 or amount > self._balance:
                return False
            self._balance -= amount
            return True

    def credit(self, amount: float) -> None:
        """
        Put money back in.

        ADDED (fixed from the original): without a credit() there is no way to
        roll back a debit. See Withdrawal.execute() for why that matters.
        """
        with self._lock:
            self._balance += amount


# =============================================================================
#  SECTION 3 -- [2] SINGLETON: the bank server
# =============================================================================
# One bank, one source of truth. Everything else in this file is a client of
# it. In production this would be a network call; the Singleton is standing in
# for "the one connection pool everyone shares".


class BankServer:
    """Singleton. See 01_parking_lot for why __new__ is the right hook."""

    _instance: Optional["BankServer"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls) -> "BankServer":
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._seed()
                    cls._instance = instance
        return cls._instance

    def _seed(self) -> None:
        """Runs exactly once. Stands in for the bank's database."""
        self._card_to_pin: Dict[str, str] = {"1111222233334444": "1234"}
        self._card_to_account: Dict[str, str] = {"1111222233334444": "ACC1"}
        self._accounts: Dict[str, Account] = {"ACC1": Account("ACC1", 10_000.0)}
        self._blocked_cards: set = set()

    def verify_pin(self, card_number: str, pin: str) -> bool:
        if card_number in self._blocked_cards:
            return False
        return self._card_to_pin.get(card_number) == pin

    def get_account_no(self, card_number: str) -> str:
        """Returns "" for an unknown card -- caller checks Card.is_valid()."""
        return self._card_to_account.get(card_number, "")

    def get_account(self, account_no: str) -> Account:
        return self._accounts[account_no]

    def block_card(self, card_number: str) -> None:
        self._blocked_cards.add(card_number)

    @classmethod
    def _reset_for_demo(cls) -> None:
        cls._instance = None


# =============================================================================
#  SECTION 4 -- [4] HARDWARE, BEHIND INTERFACES
# =============================================================================
# WHY INTERFACES FOR A KEYPAD?
#   The original called input() straight from Keypad. That makes the whole ATM
#   untestable and un-runnable in CI: the program just sits there waiting for
#   a human. Put the keypad behind an interface and you can inject a scripted
#   one for tests/demos and the real one in the field. Same code, both worlds.
#
#   This is DEPENDENCY INJECTION: a class receives its collaborators instead
#   of constructing them. It is the single highest-leverage habit in LLD.


class Keypad(ABC):
    """Where digits come from."""

    @abstractmethod
    def get_pin(self) -> str: ...

    @abstractmethod
    def get_amount(self) -> float: ...

    @abstractmethod
    def get_choice(self) -> int: ...


class PhysicalKeypad(Keypad):
    """The real thing. Note the try/except -- humans mistype."""

    def get_pin(self) -> str:
        return input("PIN: ").strip()

    def get_amount(self) -> float:
        # FIXED: the original did float(input(...)) with no guard, so typing
        # "abc" crashed the ATM with a ValueError traceback. Real hardware
        # re-prompts.
        while True:
            try:
                return float(input("Amount: ").strip())
            except ValueError:
                print("  Digits only, please.")

    def get_choice(self) -> int:
        while True:
            try:
                return int(input("Choice: ").strip())
            except ValueError:
                print("  Digits only, please.")


class ScriptedKeypad(Keypad):
    """
    Test double: replays a fixed list of keystrokes. This is what makes the
    demo at the bottom of the file run hands-free.
    """

    def __init__(self, pins: List[str], choices: List[int], amounts: List[float]):
        self._pins, self._choices, self._amounts = pins, choices, amounts

    def _pop(self, queue: list, label: str):
        if not queue:
            raise RuntimeError(f"ScriptedKeypad ran out of {label}")
        value = queue.pop(0)
        print(f"  <keypad> {label}: {value}")
        return value

    def get_pin(self) -> str:
        return self._pop(self._pins, "PIN")

    def get_amount(self) -> float:
        return self._pop(self._amounts, "amount")

    def get_choice(self) -> int:
        return self._pop(self._choices, "choice")


class CardReader:
    """Reads, ejects and (on 3 bad PINs) swallows the card."""

    def read(self, card_number: str) -> Card:
        print("  [CardReader] card accepted")
        return Card(
            card_number=card_number,
            account_no=BankServer().get_account_no(card_number),
        )

    def eject(self) -> None:
        print("  [CardReader] card ejected")

    def retain(self) -> None:
        print("  [CardReader] *** card retained ***")


class Screen:
    def show(self, message: str) -> None:
        print(f"  [Screen] {message}")

    def menu(self) -> None:
        print("  [Screen] 1) Withdraw   2) Balance   3) Exit")


class CashDispenser:
    """
    The cash cassette.

    RESERVE-THEN-DISPENSE: `reserve` earmarks notes so no other transaction can
    claim them, and dispense/release settles or undoes that. A single
    `dispense()` that both checks and pays out cannot be rolled back, and
    rollback is exactly what we need when the bank debit fails.
    """

    def __init__(self, initial_cash: float = 100_000.0):
        self._cash = initial_cash
        self._lock = threading.Lock()

    def reserve(self, amount: float) -> bool:
        with self._lock:
            if amount <= 0 or amount > self._cash:
                return False
            self._cash -= amount
            return True

    def release(self, amount: float) -> None:
        """Undo a reserve when the transaction later fails."""
        with self._lock:
            self._cash += amount

    def dispense(self, amount: float) -> None:
        print(f"  [Dispenser] please collect Rs.{amount:.2f}")

    @property
    def cash_available(self) -> float:
        with self._lock:
            return self._cash


# =============================================================================
#  SECTION 5 -- [3] TRANSACTIONS AS OBJECTS
# =============================================================================
# Every transaction type is a class with one execute(). This is Strategy if you
# think of it as "swap the algorithm", and Command if you think of it as
# "package a request as an object". Both readings are correct; the payoff is
# the same -- adding Deposit means adding a class, and ATM._serve() barely
# grows.


class Transaction(ABC):
    def __init__(self, account_no: str, amount: float = 0.0):
        self.account_no = account_no
        self.amount = amount

    @abstractmethod
    def execute(self) -> bool:
        """Returns True if the transaction completed."""
        raise NotImplementedError


class Withdrawal(Transaction):
    """
    THE MOST IMPORTANT METHOD IN THIS FILE. Read the ordering carefully.

    THE BUG IN THE NAIVE VERSION:
        if dispenser.has(amount):        # 1. check cash
            if account.debit(amount):    # 2. take money from the account
                dispenser.dispense(...)  # 3. hand over notes

    Between steps 1 and 3 anything can go wrong -- another transaction takes
    the last notes, the cassette jams, the power dies. The account has already
    been debited and the customer has no cash. Money vanishes.

    THE FIX -- reserve, debit, then settle, and unwind in reverse on failure:
        1. RESERVE the notes (they are now nobody else's)
        2. DEBIT the account
        3. if the debit failed -> RELEASE the reservation, nothing happened
        4. dispense

    This is the same shape as a two-phase commit, and "what happens if it
    fails halfway?" is the question that separates a passing answer from a
    good one.
    """

    def __init__(self, account_no: str, amount: float, dispenser: CashDispenser):
        super().__init__(account_no, amount)
        self._dispenser = dispenser

    def execute(self) -> bool:
        # 1. Reserve the cash first.
        if not self._dispenser.reserve(self.amount):
            print("  [Txn] machine is low on cash -- nothing charged")
            return False

        # 2. Try to take the money from the account.
        account = BankServer().get_account(self.account_no)
        if not account.debit(self.amount):
            # 3. Debit failed -> put the notes back. Customer is untouched.
            self._dispenser.release(self.amount)
            print("  [Txn] insufficient funds -- nothing charged")
            return False

        # 4. Both sides succeeded: hand over the money.
        self._dispenser.dispense(self.amount)
        print(f"  [Txn] withdrawal ok. New balance: Rs.{account.get_balance():.2f}")
        return True


class BalanceEnquiry(Transaction):
    """Read-only, so no rollback story is needed."""

    def execute(self) -> bool:
        balance = BankServer().get_account(self.account_no).get_balance()
        print(f"  [Txn] balance: Rs.{balance:.2f}")
        return True


# =============================================================================
#  SECTION 6 -- THE ATM ITSELF
# =============================================================================


class ATM:
    """
    Orchestrator. It owns the STATE and delegates everything else.

    Notice what is NOT here: no pricing maths, no account arithmetic, no
    input parsing. The ATM's single responsibility is sequencing.
    """

    MAX_PIN_TRIES = 3

    def __init__(
        self,
        keypad: Optional[Keypad] = None,
        dispenser: Optional[CashDispenser] = None,
    ):
        # [4] DEPENDENCY INJECTION with sensible defaults: production code can
        # just say ATM(), tests can pass their own fakes.
        self._state = ATMState.IDLE
        self._card = Card()
        self._reader = CardReader()
        self._screen = Screen()
        self._keypad = keypad or PhysicalKeypad()
        self._dispenser = dispenser or CashDispenser()

    # -- public entry point ---------------------------------------------------

    def insert_card(self, card_number: str) -> None:
        # STATE GUARD: every public method starts by asserting where we are.
        if self._state is not ATMState.IDLE:
            self._screen.show("machine busy")
            return

        card = self._reader.read(card_number)

        # FIXED: the original never validated the card, so an unknown card
        # walked all the way to the PIN prompt with an empty account number
        # and only failed later, confusingly.
        if not card.is_valid():
            self._screen.show("card not recognised")
            self._reader.eject()
            return

        self._card = card
        self._state = ATMState.CARD_INSERTED
        self._authenticate()

    # -- internal steps -------------------------------------------------------

    def _authenticate(self) -> None:
        """CARD_INSERTED -> AUTHENTICATED, or swallow the card."""
        for attempt in range(self.MAX_PIN_TRIES):
            pin = self._keypad.get_pin()

            if BankServer().verify_pin(self._card.card_number, pin):
                self._state = ATMState.AUTHENTICATED
                self._screen.show("PIN accepted")
                self._serve()
                return

            tries_left = self.MAX_PIN_TRIES - attempt - 1
            self._screen.show(f"wrong PIN -- {tries_left} attempt(s) left")

        # Fell out of the loop: three failures.
        self._screen.show("card blocked")
        BankServer().block_card(self._card.card_number)
        self._reader.retain()
        self._reset()

    def _serve(self) -> None:
        """The AUTHENTICATED menu loop. Ends by ejecting the card."""
        while True:
            self._screen.menu()
            choice = self._keypad.get_choice()

            if choice == 3:
                break

            if choice == 1:
                amount = self._keypad.get_amount()
                Withdrawal(self._card.account_no, amount, self._dispenser).execute()
            elif choice == 2:
                BalanceEnquiry(self._card.account_no).execute()
            else:
                self._screen.show("invalid choice")

        self._reader.eject()
        self._reset()

    def _reset(self) -> None:
        """
        Back to IDLE with no leftovers.

        FIXED: the original left the previous customer's Card object on the
        machine after ejecting. Harmless in a demo, a data leak in real life.
        Always clear session state when a session ends.
        """
        self._card = Card()
        self._state = ATMState.IDLE

    @property
    def state(self) -> ATMState:
        return self._state


# =============================================================================
#  SECTION 7 -- RUNNABLE DEMO
# =============================================================================


def _demo() -> None:
    valid_card = "1111222233334444"

    print("=" * 70)
    print("SCENARIO 1: correct PIN -> balance -> withdraw 2000 -> exit")
    print("=" * 70)
    BankServer._reset_for_demo()
    atm = ATM(
        keypad=ScriptedKeypad(pins=["1234"], choices=[2, 1, 3], amounts=[2000.0]),
        dispenser=CashDispenser(initial_cash=50_000.0),
    )
    atm.insert_card(valid_card)
    print(f"  final state: {atm.state.name}")

    print()
    print("=" * 70)
    print("SCENARIO 2: withdraw more than the balance -- account untouched")
    print("=" * 70)
    BankServer._reset_for_demo()
    atm = ATM(
        keypad=ScriptedKeypad(pins=["1234"], choices=[1, 2, 3], amounts=[99_999.0]),
        dispenser=CashDispenser(initial_cash=500_000.0),
    )
    atm.insert_card(valid_card)
    print("  ^ note the balance is still 10000 -- the reserve was released")

    print()
    print("=" * 70)
    print("SCENARIO 3: machine low on cash -- account untouched")
    print("=" * 70)
    BankServer._reset_for_demo()
    dispenser = CashDispenser(initial_cash=100.0)
    atm = ATM(
        keypad=ScriptedKeypad(pins=["1234"], choices=[1, 2, 3], amounts=[5000.0]),
        dispenser=dispenser,
    )
    atm.insert_card(valid_card)
    print(f"  cash still in machine: Rs.{dispenser.cash_available:.2f}")

    print()
    print("=" * 70)
    print("SCENARIO 4: three wrong PINs -- card is retained and blocked")
    print("=" * 70)
    BankServer._reset_for_demo()
    atm = ATM(keypad=ScriptedKeypad(pins=["0000", "1111", "2222"], choices=[], amounts=[]))
    atm.insert_card(valid_card)
    print(f"  final state: {atm.state.name}")
    print(f"  card now blocked? {not BankServer().verify_pin(valid_card, '1234')}")

    print()
    print("=" * 70)
    print("SCENARIO 5: unknown card is rejected at the reader")
    print("=" * 70)
    BankServer._reset_for_demo()
    atm = ATM(keypad=ScriptedKeypad(pins=[], choices=[], amounts=[]))
    atm.insert_card("0000000000000000")
    print(f"  final state: {atm.state.name}")


if __name__ == "__main__":
    _demo()
