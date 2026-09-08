"""
================================================================================
 LOGGER SYSTEM  --  Low Level Design
================================================================================

THE PROBLEM
-----------
Design a logging library: log.info("..."), log.error("..."). Messages below a
configured severity are dropped; the rest are formatted and written to one or
more destinations (console, file, network).

NOTE ON PROVENANCE
------------------
The upstream repo had NOTHING for this problem: the Python and Java files were
empty, and loggersystem.cpp was a copy-paste of the Amazon Locker code -- the
wrong program under the right filename. So this file is written from scratch
against the classic interview statement.

REQUIREMENTS WE ARE DESIGNING FOR
---------------------------------
  R1. Severity levels with an order: DEBUG < INFO < WARN < ERROR < FATAL.
  R2. A configurable threshold; anything below it is dropped cheaply.
  R3. Many destinations at once (console AND file AND ...).
  R4. Message layout must be swappable (human-readable vs JSON) independently
      of the destination.
  R5. One shared logger for the whole app.
  R6. Safe to call from many threads without interleaved half-lines.

DESIGN PATTERNS USED  (jump to the numbered tags in the code below)
-------------------------------------------------------------------
  [1] CHAIN OF RESPONSIBILITY -- the classic interview answer for level routing.
  [2] STRATEGY  -- Formatter: how a record turns into text.
  [3] OBSERVER  -- Appenders: N destinations subscribe to one stream.
  [4] SINGLETON -- Logger: one instance for the process.
  [5] DECORATOR -- AsyncAppender wraps any appender and makes it non-blocking.

THE ONE BIG IDEA -- AND THE TRAP
--------------------------------
Interviewers ask this problem *hoping* you say "Chain of Responsibility", so
Section 5 builds it. But be ready for the follow-up, because CoR is the wrong
tool here and knowing why is the actual signal:

  - CoR shines when the handler is UNKNOWN until you walk the chain (approval
    limits, HTTP middleware, exception handlers).
  - Log levels are a total order known up front. Walking a linked list to
    discover that ERROR >= WARN is O(n) ceremony for an O(1) comparison.

The production shape is Section 6: one threshold check, then fan out to every
appender. Say both. "Here is the CoR they asked for, here is why real loggers
don't use it" is a better answer than either half alone.

RUN IT
------
    python 04_logger_system/logger_system.py
================================================================================
"""

import json
import queue
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, TextIO


# =============================================================================
#  SECTION 1 -- LEVELS
# =============================================================================


class LogLevel(IntEnum):
    """
    Severity, lowest to highest.

    IntEnum again (same reason as the locker sizes): severity has a natural
    ORDER, so encode it in the type. The entire filtering rule then collapses
    to one comparison:

        if record.level >= self.threshold:

    The numbers are spaced by 10 -- a convention borrowed from Python's own
    `logging` module -- so you can slot TRACE=5 or NOTICE=25 in later without
    renumbering anything.
    """

    DEBUG = 10
    INFO = 20
    WARN = 30
    ERROR = 40
    FATAL = 50


@dataclass
class LogRecord:
    """
    One log event, as DATA rather than as a formatted string.

    THIS IS THE MOST IMPORTANT DESIGN DECISION IN THE FILE.

    The tempting shortcut is to pass a finished string around:
        appender.write("2026-01-01 ERROR [main] disk full")

    Do that and you have thrown away structure at the very first step. The
    file appender can no longer emit JSON, the network appender cannot index
    by level, and the alerting appender has to regex the timestamp back out.

    Keep the record structured and let each FORMATTER decide the layout at the
    last possible moment. "Parse, don't stringify, until you must."
    """

    level: LogLevel
    message: str
    timestamp: float = field(default_factory=time.time)
    logger_name: str = "root"
    thread_name: str = field(default_factory=lambda: threading.current_thread().name)
    context: Dict[str, Any] = field(default_factory=dict)  # request_id, user_id, ...


# =============================================================================
#  SECTION 2 -- [2] STRATEGY: formatters
# =============================================================================
# A formatter turns a LogRecord into a line of text. It knows nothing about
# where that text goes.


class Formatter(ABC):
    @abstractmethod
    def format(self, record: LogRecord) -> str: ...


class SimpleFormatter(Formatter):
    """Human-readable. What you want on a developer's terminal."""

    def format(self, record: LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.timestamp))
        extra = f" {record.context}" if record.context else ""
        return (
            f"{stamp} {record.level.name:<5} "
            f"[{record.thread_name}] {record.logger_name}: {record.message}{extra}"
        )


class JsonFormatter(Formatter):
    """
    Machine-readable. What you want when the logs go to Splunk/ELK/Datadog and
    somebody needs `level:ERROR AND context.user_id:42`.

    Same LogRecord, completely different output -- that is the payoff for
    keeping the record structured in Section 1.
    """

    def format(self, record: LogRecord) -> str:
        return json.dumps(
            {
                "ts": round(record.timestamp, 3),
                "level": record.level.name,
                "logger": record.logger_name,
                "thread": record.thread_name,
                "msg": record.message,
                **record.context,
            },
            sort_keys=True,
        )


# =============================================================================
#  SECTION 3 -- [3] OBSERVER: appenders (destinations)
# =============================================================================
# An appender knows WHERE bytes go. It does not know how they were formatted
# (that is the Formatter's job) or whether they should have been emitted at all
# (that is the Logger's job).
#
# Formatter x Appender is a deliberate 2-axis split. With 3 formatters and 4
# destinations you write 7 classes, not 12. Combining them into
# "JsonFileAppender", "JsonConsoleAppender", ... is the classic class
# explosion this split exists to prevent.


class Appender(ABC):
    def __init__(self, formatter: Optional[Formatter] = None,
                 min_level: LogLevel = LogLevel.DEBUG):
        self.formatter = formatter or SimpleFormatter()
        # Per-appender threshold: console shows everything, the pager only
        # wants FATAL. Independent of the Logger's global threshold.
        self.min_level = min_level

    def handle(self, record: LogRecord) -> None:
        if record.level >= self.min_level:
            self.append(record)

    @abstractmethod
    def append(self, record: LogRecord) -> None: ...

    def close(self) -> None:
        """Flush and release resources. Override where it matters."""


class ConsoleAppender(Appender):
    """
    Writes to stdout.

    The lock is not decoration. print() from several threads can interleave
    mid-line and produce spliced garbage. One lock per destination keeps each
    record atomic -- and it must be per-DESTINATION, because two threads
    writing to the same file need to serialise even if they came through
    different loggers.
    """

    def __init__(self, formatter: Optional[Formatter] = None,
                 min_level: LogLevel = LogLevel.DEBUG,
                 stream: Optional[TextIO] = None):
        super().__init__(formatter, min_level)
        self._stream = stream
        self._lock = threading.Lock()

    def append(self, record: LogRecord) -> None:
        line = self.formatter.format(record)
        with self._lock:
            if self._stream is None:
                print(line)
            else:
                self._stream.write(line + "\n")


class FileAppender(Appender):
    """Writes to a file, opened once and held open."""

    def __init__(self, path: str, formatter: Optional[Formatter] = None,
                 min_level: LogLevel = LogLevel.DEBUG):
        super().__init__(formatter, min_level)
        self._path = path
        self._handle = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def append(self, record: LogRecord) -> None:
        line = self.formatter.format(record)
        with self._lock:
            self._handle.write(line + "\n")
            # flush() every record is slow but means a crash loses nothing.
            # A real logger makes this configurable; the trade-off is
            # durability vs throughput and there is no universally right side.
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()


class MemoryAppender(Appender):
    """Keeps records in a list. Exists so tests can assert on what was logged."""

    def __init__(self, formatter: Optional[Formatter] = None,
                 min_level: LogLevel = LogLevel.DEBUG):
        super().__init__(formatter, min_level)
        self.records: List[LogRecord] = []
        self._lock = threading.Lock()

    def append(self, record: LogRecord) -> None:
        with self._lock:
            self.records.append(record)


class AsyncAppender(Appender):
    """
    [5] DECORATOR: wraps ANY appender and makes writes non-blocking.

    THE PROBLEM IT SOLVES:
      FileAppender.append() does a disk write with an fsync. If your request
      handler logs 5 lines, it just paid for 5 disk syncs. Logging should
      never be the reason a request is slow.

    HOW:
      Producer threads drop the record on a queue and return immediately. One
      background worker drains the queue and does the slow write.

    WHY DECORATOR AND NOT INHERITANCE:
      AsyncAppender IS an Appender and HOLDS an Appender. So it composes with
      anything -- Async(File(...)), Async(Console(...)), even Async(Async(...))
      if you were feeling silly. Subclassing instead would need
      AsyncFileAppender, AsyncConsoleAppender, AsyncNetworkAppender: the same
      class explosion the Formatter/Appender split already avoided once.

    THE TRADE-OFF YOU MUST STATE OUT LOUD:
      Async logging can LOSE the last few records if the process is killed
      before the queue drains. That is exactly the records you want after a
      crash. Mitigations: bound the queue, flush on shutdown (close() below),
      and keep FATAL synchronous.
    """

    def __init__(self, inner: Appender, max_queue: int = 10_000):
        super().__init__(inner.formatter, inner.min_level)
        self._inner = inner
        self._queue: "queue.Queue[Optional[LogRecord]]" = queue.Queue(maxsize=max_queue)
        self._worker = threading.Thread(
            target=self._drain, name="log-worker", daemon=True
        )
        self._worker.start()

    def append(self, record: LogRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # BACKPRESSURE POLICY -- a real decision, not an oversight.
            # Options: (a) block the caller, (b) drop the new record,
            # (c) drop the oldest. We drop, because a logger must never be
            # the thing that stalls the application. We announce it so the
            # gap in the logs is not a silent mystery.
            print("  [logger] queue full -- record dropped")

    def _drain(self) -> None:
        while True:
            record = self._queue.get()
            if record is None:  # None is the shutdown sentinel ("poison pill")
                self._queue.task_done()
                return
            try:
                self._inner.handle(record)
            finally:
                self._queue.task_done()

    def close(self) -> None:
        """Drain everything, stop the worker, then close what we wrap."""
        self._queue.join()          # wait for queued records to be written
        self._queue.put(None)       # tell the worker to stop
        self._worker.join(timeout=2)
        self._inner.close()


# =============================================================================
#  SECTION 4 -- [4] SINGLETON: the logger
# =============================================================================


class Logger:
    """
    The object application code actually calls.

    Its job is exactly three things:
      1. drop anything below the threshold (cheaply -- one int compare)
      2. build a LogRecord
      3. hand it to every appender

    It does not format. It does not know about files. That is what makes it
    small enough to be obviously correct.
    """

    _instance: Optional["Logger"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls) -> "Logger":
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._init_state()
                    cls._instance = instance
        return cls._instance

    def _init_state(self) -> None:
        self._appenders: List[Appender] = []
        self._threshold: LogLevel = LogLevel.DEBUG
        self._name = "root"
        self._context: Dict[str, Any] = {}
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "Logger":
        return cls()

    @classmethod
    def _reset_for_demo(cls) -> None:
        if cls._instance is not None:
            for appender in cls._instance._appenders:
                appender.close()
        cls._instance = None

    # -- configuration --------------------------------------------------------

    def add_appender(self, appender: Appender) -> "Logger":
        with self._lock:
            self._appenders.append(appender)
        return self  # returning self allows fluent chaining

    def set_threshold(self, level: LogLevel) -> "Logger":
        self._threshold = level
        return self

    def set_context(self, **kwargs: Any) -> "Logger":
        """Fields stamped onto every record, e.g. service name or request id."""
        self._context.update(kwargs)
        return self

    # -- the hot path ---------------------------------------------------------

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        # THE EARLY RETURN. This one line is why `log.debug(...)` is nearly
        # free in production: an int comparison, then nothing. No record is
        # built, no string is formatted, no lock is taken.
        #
        # Corollary for callers: never write
        #     log.debug("state = " + expensive_dump())
        # because expensive_dump() runs before log.debug is even called and
        # the early return cannot save you. Pass the pieces, not the product.
        if level < self._threshold:
            return

        record = LogRecord(
            level=level,
            message=message,
            logger_name=self._name,
            context={**self._context, **context},
        )

        # Snapshot the list under the lock, then fan out OUTSIDE the lock, so
        # a slow appender does not block other threads from logging.
        with self._lock:
            appenders = list(self._appenders)

        for appender in appenders:
            appender.handle(record)

    # -- sugar ---------------------------------------------------------------

    def debug(self, message: str, **ctx: Any) -> None:
        self.log(LogLevel.DEBUG, message, **ctx)

    def info(self, message: str, **ctx: Any) -> None:
        self.log(LogLevel.INFO, message, **ctx)

    def warn(self, message: str, **ctx: Any) -> None:
        self.log(LogLevel.WARN, message, **ctx)

    def error(self, message: str, **ctx: Any) -> None:
        self.log(LogLevel.ERROR, message, **ctx)

    def fatal(self, message: str, **ctx: Any) -> None:
        self.log(LogLevel.FATAL, message, **ctx)

    def shutdown(self) -> None:
        """Flush every appender. Call this before the process exits."""
        for appender in self._appenders:
            appender.close()


# =============================================================================
#  SECTION 5 -- [1] CHAIN OF RESPONSIBILITY (the version interviewers ask for)
# =============================================================================
# Each handler owns ONE level. It either handles the record or passes it to
# the next handler in the chain. The caller holds only the head of the chain
# and has no idea how long it is or who ends up doing the work.
#
# Read Section 6 immediately after for when this is and is not the right call.


class LogHandler(ABC):
    """One link in the chain."""

    def __init__(self, level: LogLevel):
        self._level = level
        self._next: Optional["LogHandler"] = None

    def set_next(self, handler: "LogHandler") -> "LogHandler":
        """Returns the handler passed in, so chains read left to right:

            debug.set_next(info).set_next(error)
        """
        self._next = handler
        return handler

    def handle(self, record: LogRecord) -> None:
        if record.level == self._level:
            self.write(record)
        elif self._next is not None:
            self._next.handle(record)
        else:
            # THE CHAIN'S FAILURE MODE, made explicit. A CoR whose tail
            # silently swallows unmatched requests is a debugging nightmare:
            # your ERROR logs just... don't appear, and nothing says why.
            # Always give a chain a terminal case.
            print(f"  [chain] no handler for {record.level.name} -- record dropped")

    @abstractmethod
    def write(self, record: LogRecord) -> None: ...


class DebugHandler(LogHandler):
    def __init__(self) -> None:
        super().__init__(LogLevel.DEBUG)

    def write(self, record: LogRecord) -> None:
        print(f"  [chain/DEBUG  ] {record.message}")


class InfoHandler(LogHandler):
    def __init__(self) -> None:
        super().__init__(LogLevel.INFO)

    def write(self, record: LogRecord) -> None:
        print(f"  [chain/INFO   ] {record.message}")


class ErrorHandler(LogHandler):
    def __init__(self) -> None:
        super().__init__(LogLevel.ERROR)

    def write(self, record: LogRecord) -> None:
        print(f"  [chain/ERROR  ] {record.message} -> also paging on-call")


# =============================================================================
#  SECTION 6 -- WHY REAL LOGGERS DON'T USE THE CHAIN
# =============================================================================
# Say this part out loud in an interview; it is the difference between
# "knows the pattern" and "knows when to use the pattern".
#
#   1. LOOKUP COST. The chain is a linked list, so routing a FATAL record
#      walks every handler ahead of it: O(n). A threshold compare is O(1).
#
#   2. LEVELS ARE A TOTAL ORDER, NOT AN UNKNOWN SET. CoR earns its keep when
#      you genuinely cannot tell which handler applies without asking each in
#      turn -- approval limits, middleware, exception dispatch. `ERROR >= WARN`
#      needs no search.
#
#   3. ONE HANDLER WINS. The chain stops at the first match, but real logging
#      wants FAN-OUT: an ERROR should go to console AND file AND the pager.
#      Expressing that in a chain means every handler forwards after handling,
#      at which point it is not a chain any more -- it is a broadcast list
#      with extra steps. Which is exactly what Observer is.
#
#   4. CONFIGURATION. Users configure loggers with "threshold = WARN".
#      Translating that into "rebuild the chain, omitting DEBUG and INFO" is
#      work you did not need to do.
#
#   VERDICT: build the chain when asked, then reach for
#   `Logger + threshold + N appenders` -- Sections 3 and 4 -- for anything real.


# =============================================================================
#  SECTION 7 -- RUNNABLE DEMO
# =============================================================================


def _demo() -> None:
    import os
    import tempfile

    print("=" * 70)
    print("SCENARIO 1: one record -> three destinations, two layouts")
    print("=" * 70)
    Logger._reset_for_demo()
    log = Logger.get_instance()

    log_path = os.path.join(tempfile.gettempdir(), "lld_demo.log")
    if os.path.exists(log_path):
        os.remove(log_path)

    memory = MemoryAppender()
    (
        log.set_context(service="checkout")
        .add_appender(ConsoleAppender(SimpleFormatter()))
        .add_appender(FileAppender(log_path, JsonFormatter()))
        .add_appender(memory)
    )

    log.info("order placed", order_id="ORD-1", amount=499)
    log.error("payment gateway timeout", order_id="ORD-1", retry=2)

    print(f"  -- same two records, JSON, from {log_path}:")
    with open(log_path, encoding="utf-8") as handle:
        for line in handle:
            print(f"     {line.rstrip()}")

    print()
    print("=" * 70)
    print("SCENARIO 2: the threshold drops records before any work happens")
    print("=" * 70)
    log.set_threshold(LogLevel.WARN)
    before = len(memory.records)
    log.debug("this is dropped")
    log.info("so is this")
    log.warn("this one survives")
    log.error("and this one")
    print(f"  4 calls made, {len(memory.records) - before} records reached the appenders")
    log.set_threshold(LogLevel.DEBUG)

    print()
    print("=" * 70)
    print("SCENARIO 3: per-appender levels -- the pager only wants FATAL")
    print("=" * 70)
    Logger._reset_for_demo()
    log = Logger.get_instance()
    log.add_appender(ConsoleAppender(SimpleFormatter(), min_level=LogLevel.DEBUG))
    pager = MemoryAppender(min_level=LogLevel.FATAL)
    log.add_appender(pager)
    log.info("routine")
    log.error("bad but survivable")
    log.fatal("database is gone")
    print(f"  console saw 3 records, pager saw {len(pager.records)}: "
          f"{[r.message for r in pager.records]}")

    print()
    print("=" * 70)
    print("SCENARIO 4: DECORATOR -- async writes, from many threads")
    print("=" * 70)
    Logger._reset_for_demo()
    log = Logger.get_instance()
    collected = MemoryAppender()
    log.add_appender(AsyncAppender(collected))

    def worker(worker_id: int) -> None:
        for i in range(5):
            log.info(f"worker {worker_id} step {i}")

    threads = [threading.Thread(target=worker, args=(n,), name=f"w{n}") for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    log.shutdown()  # drains the queue -- without this we would lose records
    print(f"  4 threads x 5 records = 20 expected, {len(collected.records)} written")

    print()
    print("=" * 70)
    print("SCENARIO 5: the CHAIN OF RESPONSIBILITY version")
    print("=" * 70)
    debug_handler = DebugHandler()
    debug_handler.set_next(InfoHandler()).set_next(ErrorHandler())

    for level, message in [
        (LogLevel.DEBUG, "cache miss"),
        (LogLevel.INFO, "user logged in"),
        (LogLevel.ERROR, "disk full"),
        (LogLevel.FATAL, "kernel panic"),  # nobody handles FATAL
    ]:
        debug_handler.handle(LogRecord(level=level, message=message))
    print("  ^ FATAL fell off the end of the chain. See SECTION 6 for why")
    print("    real loggers use a threshold + fan-out instead.")

    Logger._reset_for_demo()


if __name__ == "__main__":
    _demo()
