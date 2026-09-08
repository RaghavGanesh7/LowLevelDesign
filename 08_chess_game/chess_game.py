"""
================================================================================
 CHESS  --  Low Level Design
================================================================================

THE PROBLEM
-----------
Design a two-player chess game: a board, six kinds of piece with different
movement rules, turn alternation, undo/redo, and detection of check,
checkmate and stalemate.

NOTE ON PROVENANCE
------------------
The upstream repo's Python file was EMPTY, and the C++ version had this for
every single piece:

    vector<pair<int,int>> getValidMoves() override { return {}; }

Six classes, six empty methods. The Strategy pattern was declared but never
filled in, so the "game" accepted any move at all -- a pawn to h8, a king
capturing its own queen. The movement rules ARE the problem; this file
implements them.

REQUIREMENTS WE ARE DESIGNING FOR
---------------------------------
  R1. Each piece type moves by its own rule.
  R2. You cannot capture your own pieces, or move off the board.
  R3. You cannot make a move that leaves YOUR OWN king in check.
  R4. Detect check, checkmate and stalemate.
  R5. Undo and redo, any number of moves deep.

DESIGN PATTERNS USED  (jump to the numbered tags in the code below)
-------------------------------------------------------------------
  [1] STRATEGY  -- Piece subclasses: one movement algorithm each.
  [2] COMMAND   -- Move: an executable, reversible action object.
  [3] FACTORY   -- PieceFactory: PieceType -> piece instance.
  [4] SINGLETON -- Game (with a caveat -- see Section 7).

THE TWO BIG IDEAS
-----------------
1. PSEUDO-LEGAL vs LEGAL. A piece knows how it moves; only the GAME knows
   whether a move is allowed. A pinned knight moves in an L like any other
   knight -- but moving it would expose its king, so the game rejects it.
   Keeping those two questions in different classes is what stops every piece
   from needing to know about kings.

2. LEGALITY IS TESTED BY SIMULATION. There is no clever formula. You make the
   move, ask "is my king attacked?", and take it back. That is why Move must
   be perfectly reversible -- which is exactly what COMMAND gives you, and the
   same machinery then powers undo/redo for free.

RUN IT
------
    python 08_chess_game/chess_game.py
================================================================================
"""

from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

Position = Tuple[int, int]  # (row, col), both 0..7


# =============================================================================
#  SECTION 1 -- BASIC TYPES
# =============================================================================


class Color(Enum):
    WHITE = auto()
    BLACK = auto()

    @property
    def opponent(self) -> "Color":
        return Color.BLACK if self is Color.WHITE else Color.WHITE


class PieceType(Enum):
    KING = auto()
    QUEEN = auto()
    ROOK = auto()
    BISHOP = auto()
    KNIGHT = auto()
    PAWN = auto()


class GameStatus(Enum):
    ACTIVE = auto()
    CHECK = auto()
    CHECKMATE = auto()
    STALEMATE = auto()


def in_bounds(row: int, col: int) -> bool:
    return 0 <= row < 8 and 0 <= col < 8


def to_algebraic(position: Position) -> str:
    """(0,4) -> 'e1'. Board rows run 0..7 from White's back rank."""
    row, col = position
    return f"{'abcdefgh'[col]}{row + 1}"


def from_algebraic(square: str) -> Position:
    """'e1' -> (0,4)."""
    return int(square[1]) - 1, "abcdefgh".index(square[0])


# =============================================================================
#  SECTION 2 -- [1] STRATEGY: pieces and their movement rules
# =============================================================================


class Piece(ABC):
    """
    Base for all six piece types.

    THE CONTRACT: pseudo_legal_moves() returns every square this piece could
    move to CONSIDERING ONLY its own movement rule, the edges of the board, and
    what is sitting on the target square. It does NOT consider check -- that is
    the Game's job (Section 6).

    Splitting it this way is the whole reason the six subclasses stay tiny. A
    Knight class that also had to reason about pins would be unreadable, and
    you would have to write that reasoning six times.
    """

    def __init__(self, color: Color, piece_type: PieceType):
        self.color = color
        self.piece_type = piece_type
        # Needed for pawn double-steps and (if you add them) castling rights.
        # Move.undo() must restore this -- see the bug note in Section 3.
        self.has_moved = False

    @abstractmethod
    def pseudo_legal_moves(self, board: "Board", origin: Position) -> List[Position]:
        """Squares reachable by this piece's own rule."""

    def attacked_squares(self, board: "Board", origin: Position) -> List[Position]:
        """
        Squares this piece ATTACKS.

        For five of the six pieces this is identical to where it can move, so
        the default just delegates. The PAWN is the exception and overrides it
        -- a pawn moves straight ahead but captures diagonally, so its move
        list and its attack list are different sets. Using the move list for
        attack detection is a classic chess-engine bug: a king would refuse to
        step onto a square merely "occupied" by a pawn's forward push.
        """
        return self.pseudo_legal_moves(board, origin)

    def _slide(
        self, board: "Board", origin: Position, directions: List[Position]
    ) -> List[Position]:
        """
        Shared helper for the sliding pieces (rook, bishop, queen).

        Walk outward in each direction until you hit the edge or a piece.
        An enemy piece is included (you can capture it) and then stops the ray;
        a friendly piece stops the ray without being included.

        Rook, Bishop and Queen differ ONLY in their direction list, so all
        three subclasses below are two lines each. That is the payoff of
        putting the shared walk in the base class instead of copy-pasting it.
        """
        row, col = origin
        moves: List[Position] = []
        for delta_row, delta_col in directions:
            step_row, step_col = row + delta_row, col + delta_col
            while in_bounds(step_row, step_col):
                occupant = board.piece_at((step_row, step_col))
                if occupant is None:
                    moves.append((step_row, step_col))
                else:
                    if occupant.color is not self.color:
                        moves.append((step_row, step_col))  # capture
                    break  # blocked either way -- the ray ends here
                step_row += delta_row
                step_col += delta_col
        return moves

    def __repr__(self) -> str:
        return f"{self.color.name[0]}{self.piece_type.name[:2]}"


class King(Piece):
    """One square in any of the eight directions."""

    OFFSETS = [(-1, -1), (-1, 0), (-1, 1), (0, -1),
               (0, 1), (1, -1), (1, 0), (1, 1)]

    def __init__(self, color: Color):
        super().__init__(color, PieceType.KING)

    def pseudo_legal_moves(self, board: "Board", origin: Position) -> List[Position]:
        row, col = origin
        moves = []
        for delta_row, delta_col in self.OFFSETS:
            target = (row + delta_row, col + delta_col)
            if not in_bounds(*target):
                continue
            occupant = board.piece_at(target)
            if occupant is None or occupant.color is not self.color:
                moves.append(target)
        return moves
        # NOT IMPLEMENTED: castling. It needs the king and rook both unmoved,
        # empty squares between them, and the king neither in check nor passing
        # through an attacked square. `has_moved` above is already tracked for
        # it. Left out to keep this file readable -- but know what it takes.


class Queen(Piece):
    DIRECTIONS = [(-1, -1), (-1, 0), (-1, 1), (0, -1),
                  (0, 1), (1, -1), (1, 0), (1, 1)]

    def __init__(self, color: Color):
        super().__init__(color, PieceType.QUEEN)

    def pseudo_legal_moves(self, board: "Board", origin: Position) -> List[Position]:
        return self._slide(board, origin, self.DIRECTIONS)


class Rook(Piece):
    DIRECTIONS = [(-1, 0), (1, 0), (0, -1), (0, 1)]

    def __init__(self, color: Color):
        super().__init__(color, PieceType.ROOK)

    def pseudo_legal_moves(self, board: "Board", origin: Position) -> List[Position]:
        return self._slide(board, origin, self.DIRECTIONS)


class Bishop(Piece):
    DIRECTIONS = [(-1, -1), (-1, 1), (1, -1), (1, 1)]

    def __init__(self, color: Color):
        super().__init__(color, PieceType.BISHOP)

    def pseudo_legal_moves(self, board: "Board", origin: Position) -> List[Position]:
        return self._slide(board, origin, self.DIRECTIONS)


class Knight(Piece):
    """The only piece that jumps -- so no ray-walking, just eight offsets."""

    OFFSETS = [(-2, -1), (-2, 1), (-1, -2), (-1, 2),
               (1, -2), (1, 2), (2, -1), (2, 1)]

    def __init__(self, color: Color):
        super().__init__(color, PieceType.KNIGHT)

    def pseudo_legal_moves(self, board: "Board", origin: Position) -> List[Position]:
        row, col = origin
        moves = []
        for delta_row, delta_col in self.OFFSETS:
            target = (row + delta_row, col + delta_col)
            if not in_bounds(*target):
                continue
            occupant = board.piece_at(target)
            if occupant is None or occupant.color is not self.color:
                moves.append(target)
        return moves


class Pawn(Piece):
    """
    The most complicated piece, despite looking like the simplest:

      - moves forward one, but only onto an EMPTY square
      - may move forward two from its starting rank, if BOTH squares are empty
      - captures ONLY diagonally, and only onto an occupied enemy square
      - direction depends on colour
      - promotes on the last rank
      - (en passant, not implemented -- see the note at the bottom)

    This is why it overrides attacked_squares(): its moves and its attacks are
    genuinely different sets.
    """

    def __init__(self, color: Color):
        super().__init__(color, PieceType.PAWN)

    @property
    def direction(self) -> int:
        """White marches up the rows (+1), Black down (-1)."""
        return 1 if self.color is Color.WHITE else -1

    @property
    def start_row(self) -> int:
        return 1 if self.color is Color.WHITE else 6

    @property
    def promotion_row(self) -> int:
        return 7 if self.color is Color.WHITE else 0

    def pseudo_legal_moves(self, board: "Board", origin: Position) -> List[Position]:
        row, col = origin
        moves: List[Position] = []

        one_ahead = (row + self.direction, col)
        if in_bounds(*one_ahead) and board.piece_at(one_ahead) is None:
            moves.append(one_ahead)

            # The double step is only legal from the start rank AND only if
            # the intermediate square was empty -- which is why this is nested
            # inside the one-step check rather than tested independently.
            two_ahead = (row + 2 * self.direction, col)
            if row == self.start_row and board.piece_at(two_ahead) is None:
                moves.append(two_ahead)

        # Diagonal captures: legal ONLY when an enemy piece is there.
        for delta_col in (-1, 1):
            target = (row + self.direction, col + delta_col)
            if not in_bounds(*target):
                continue
            occupant = board.piece_at(target)
            if occupant is not None and occupant.color is not self.color:
                moves.append(target)

        return moves
        # NOT IMPLEMENTED: en passant. It needs the board to remember whether
        # the LAST move was an enemy pawn's double step -- state that lives on
        # the game, not on the piece. Mentioning what you left out and why is
        # worth more in an interview than silently omitting it.

    def attacked_squares(self, board: "Board", origin: Position) -> List[Position]:
        """Both diagonals, occupied or not -- attack is about control."""
        row, col = origin
        return [
            (row + self.direction, col + delta_col)
            for delta_col in (-1, 1)
            if in_bounds(row + self.direction, col + delta_col)
        ]


# =============================================================================
#  SECTION 3 -- [3] FACTORY
# =============================================================================


class PieceFactory:
    """PieceType + Color in, piece out. One dict instead of a switch."""

    _REGISTRY = {
        PieceType.KING: King,
        PieceType.QUEEN: Queen,
        PieceType.ROOK: Rook,
        PieceType.BISHOP: Bishop,
        PieceType.KNIGHT: Knight,
        PieceType.PAWN: Pawn,
    }

    @classmethod
    def create(cls, piece_type: PieceType, color: Color) -> Piece:
        return cls._REGISTRY[piece_type](color)


# =============================================================================
#  SECTION 4 -- THE BOARD
# =============================================================================


class Board:
    """
    An 8x8 grid of Optional[Piece].

    NO Cell CLASS. The original wrapped every square in a Cell object holding
    its own row/col -- 64 objects that each store the coordinates you already
    used to look them up. A plain nested list is simpler and faster, and
    positions are passed as (row, col) tuples. Delete the class that only
    stores what the caller already knows.
    """

    #: Unicode glyphs, purely so the demo board is readable.
    GLYPHS = {
        (Color.WHITE, PieceType.KING): "K", (Color.BLACK, PieceType.KING): "k",
        (Color.WHITE, PieceType.QUEEN): "Q", (Color.BLACK, PieceType.QUEEN): "q",
        (Color.WHITE, PieceType.ROOK): "R", (Color.BLACK, PieceType.ROOK): "r",
        (Color.WHITE, PieceType.BISHOP): "B", (Color.BLACK, PieceType.BISHOP): "b",
        (Color.WHITE, PieceType.KNIGHT): "N", (Color.BLACK, PieceType.KNIGHT): "n",
        (Color.WHITE, PieceType.PAWN): "P", (Color.BLACK, PieceType.PAWN): "p",
    }

    BACK_RANK = [
        PieceType.ROOK, PieceType.KNIGHT, PieceType.BISHOP, PieceType.QUEEN,
        PieceType.KING, PieceType.BISHOP, PieceType.KNIGHT, PieceType.ROOK,
    ]

    def __init__(self, empty: bool = False):
        self._grid: List[List[Optional[Piece]]] = [
            [None] * 8 for _ in range(8)
        ]
        if not empty:
            self._setup_standard_position()

    def _setup_standard_position(self) -> None:
        for col, piece_type in enumerate(self.BACK_RANK):
            self._grid[0][col] = PieceFactory.create(piece_type, Color.WHITE)
            self._grid[1][col] = PieceFactory.create(PieceType.PAWN, Color.WHITE)
            self._grid[6][col] = PieceFactory.create(PieceType.PAWN, Color.BLACK)
            self._grid[7][col] = PieceFactory.create(piece_type, Color.BLACK)

    def piece_at(self, position: Position) -> Optional[Piece]:
        row, col = position
        if not in_bounds(row, col):
            return None
        return self._grid[row][col]

    def set_piece(self, position: Position, piece: Optional[Piece]) -> None:
        row, col = position
        self._grid[row][col] = piece

    def positions_of(self, color: Color) -> List[Position]:
        return [
            (row, col)
            for row in range(8)
            for col in range(8)
            if self._grid[row][col] is not None
            and self._grid[row][col].color is color
        ]

    def find_king(self, color: Color) -> Optional[Position]:
        for position in self.positions_of(color):
            piece = self.piece_at(position)
            if piece is not None and piece.piece_type is PieceType.KING:
                return position
        return None

    def is_square_attacked(self, position: Position, by_color: Color) -> bool:
        """
        Is `position` attacked by any piece of `by_color`?

        THIS ONE METHOD IS THE ENGINE OF EVERYTHING IN SECTION 6. Check,
        checkmate, stalemate and legal-move filtering are all expressed in
        terms of it.

        It is O(pieces x moves-per-piece) -- fine for a game, and the obvious
        thing to optimise first if you were writing a real engine (you would
        keep incremental attack maps rather than recomputing from scratch).
        """
        for origin in self.positions_of(by_color):
            piece = self.piece_at(origin)
            if piece is None:
                continue
            if position in piece.attacked_squares(self, origin):
                return True
        return False

    def render(self) -> str:
        """ASCII board, White at the bottom -- so row 7 prints first."""
        lines = []
        for row in range(7, -1, -1):
            cells = []
            for col in range(8):
                piece = self._grid[row][col]
                cells.append(
                    "." if piece is None
                    else self.GLYPHS[(piece.color, piece.piece_type)]
                )
            lines.append(f"  {row + 1} " + " ".join(cells))
        lines.append("    " + " ".join("abcdefgh"))
        return "\n".join(lines)


# =============================================================================
#  SECTION 5 -- [2] COMMAND: moves that undo themselves
# =============================================================================


class Move:
    """
    An executable, REVERSIBLE action.

    That reversibility is not just a convenience feature for the player. It is
    load-bearing: legality testing works by making a move, inspecting the
    resulting position, and taking it back (see Game._is_legal). If undo() is
    not a perfect inverse of execute(), the rules engine silently corrupts the
    board.

    TWO BUGS FIXED FROM THE ORIGINAL C++:

      BUG 1 -- undo() did not restore `has_moved`:
          void undo() { start->piece = piece; end->piece = captured; }
        The piece goes back to its square, but it is still flagged as having
        moved. Undo a pawn's first move and it permanently loses its right to
        the two-square advance. Since legality testing calls undo() on every
        candidate move, this corrupts `has_moved` for every piece within a few
        turns. An undo must restore ALL state it touched, not just the obvious
        state.

      BUG 2 -- the move was recorded in two places:
          cmdHistory.push(m);
          board->moveHistory.push_back(m);
        Two histories, and undo only popped one. They diverge on the first
        undo and every consumer of the second list is then wrong. ONE history,
        owned by ONE object.
    """

    def __init__(self, board: Board, origin: Position, target: Position):
        self.board = board
        self.origin = origin
        self.target = target
        self.piece = board.piece_at(origin)
        self.captured = board.piece_at(target)
        # Saved so undo() can restore it exactly (BUG 1 above).
        self.piece_had_moved = self.piece.has_moved if self.piece else False
        self.promoted_from: Optional[Piece] = None

    @property
    def is_capture(self) -> bool:
        return self.captured is not None

    def execute(self) -> None:
        assert self.piece is not None
        self.board.set_piece(self.target, self.piece)
        self.board.set_piece(self.origin, None)
        self.piece.has_moved = True

        # PROMOTION: a pawn reaching the last rank becomes a queen.
        # Always-queen ("auto-queen") is a simplification; real chess lets you
        # under-promote to a rook, bishop or knight, which matters in a handful
        # of endgames. The hook is here if you want to extend it.
        if (
            self.piece.piece_type is PieceType.PAWN
            and self.target[0] == self.piece.promotion_row  # type: ignore[attr-defined]
        ):
            self.promoted_from = self.piece
            self.board.set_piece(self.target, Queen(self.piece.color))

    def undo(self) -> None:
        assert self.piece is not None
        # If we promoted, put the PAWN back, not the queen it became.
        self.board.set_piece(self.origin, self.promoted_from or self.piece)
        self.board.set_piece(self.target, self.captured)
        self.piece.has_moved = self.piece_had_moved  # the BUG 1 fix

    def __str__(self) -> str:
        piece_name = self.piece.piece_type.name.title() if self.piece else "?"
        connector = "x" if self.is_capture else "-"
        return (
            f"{piece_name} {to_algebraic(self.origin)}"
            f"{connector}{to_algebraic(self.target)}"
        )


class MoveHistory:
    """
    The undo/redo stack pair -- textbook COMMAND bookkeeping.

    Two stacks:
      - `_done`: moves that have been played
      - `_undone`: moves taken back, available to redo

    Playing a NEW move clears the redo stack. That is not an implementation
    detail, it is the semantics every editor uses: once you branch off the
    timeline, the old future is gone.
    """

    def __init__(self) -> None:
        self._done: List[Move] = []
        self._undone: List[Move] = []

    def push(self, move: Move) -> None:
        move.execute()
        self._done.append(move)
        self._undone.clear()

    def undo(self) -> Optional[Move]:
        if not self._done:
            return None
        move = self._done.pop()
        move.undo()
        self._undone.append(move)
        return move

    def redo(self) -> Optional[Move]:
        if not self._undone:
            return None
        move = self._undone.pop()
        move.execute()
        self._done.append(move)
        return move

    @property
    def moves(self) -> List[Move]:
        return list(self._done)

    def __len__(self) -> int:
        return len(self._done)


# =============================================================================
#  SECTION 6 -- THE RULES: pseudo-legal becomes legal
# =============================================================================


class Player:
    def __init__(self, name: str, color: Color, is_ai: bool = False):
        self.name = name
        self.color = color
        self.is_ai = is_ai


class IllegalMoveError(ValueError):
    """Raised with a reason, so the UI can tell the player WHY."""


class Game:
    """
    Owns the board, whose turn it is, and the rules.

    [4] SINGLETON -- WITH A CAVEAT, and this is worth saying out loud:
      The original made Game a hard singleton. That is fine for one desktop
      game and WRONG for a server, which runs thousands of games at once. A
      singleton is a global variable in a costume; reach for it only when the
      thing really is unique in the process (a connection pool, a logger, the
      one physical parking lot).

      So: the constructor is public -- make as many Games as you like -- and
      get_instance() is offered separately for the single-game case. You get
      the convenience without the design being locked to it.
    """

    _instance: Optional["Game"] = None

    def __init__(self, board: Optional[Board] = None,
                 turn: Color = Color.WHITE):
        self.board = board or Board()
        self.players: Dict[Color, Player] = {
            Color.WHITE: Player("White", Color.WHITE),
            Color.BLACK: Player("Black", Color.BLACK),
        }
        self.current_turn = turn
        self.history = MoveHistory()  # THE single history (BUG 2 fix)

    @classmethod
    def get_instance(cls) -> "Game":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def _reset_for_demo(cls) -> None:
        cls._instance = None

    # -- the core rules -------------------------------------------------------

    def is_in_check(self, color: Color) -> bool:
        king_square = self.board.find_king(color)
        if king_square is None:
            return False  # only reachable in a hand-built test position
        return self.board.is_square_attacked(king_square, color.opponent)

    def _is_legal(self, move: Move) -> bool:
        """
        R3: a move is legal iff it does not leave your own king in check.

        THE SIMULATION TRICK, and the reason Move had to be reversible:
            1. execute the move
            2. ask "is my king attacked now?"
            3. undo it
            4. the answer to (2) is the verdict

        This one method handles pins, discovered checks, moving out of check,
        blocking a check, and capturing the checker -- WITHOUT a single special
        case for any of them. That is what makes it worth the cost of
        simulating: five rules for the price of none.
        """
        assert move.piece is not None
        move.execute()
        try:
            return not self.is_in_check(move.piece.color)
        finally:
            # `finally` so an exception mid-check cannot leave the board
            # corrupted. Simulation must ALWAYS be undone.
            move.undo()

    def legal_moves_from(self, origin: Position) -> List[Position]:
        piece = self.board.piece_at(origin)
        if piece is None or piece.color is not self.current_turn:
            return []
        return [
            target
            for target in piece.pseudo_legal_moves(self.board, origin)
            if self._is_legal(Move(self.board, origin, target))
        ]

    def all_legal_moves(self, color: Color) -> List[Move]:
        moves = []
        for origin in self.board.positions_of(color):
            piece = self.board.piece_at(origin)
            if piece is None:
                continue
            for target in piece.pseudo_legal_moves(self.board, origin):
                candidate = Move(self.board, origin, target)
                if self._is_legal(candidate):
                    moves.append(candidate)
        return moves

    @property
    def status(self) -> GameStatus:
        """
        R4. Note how compact this is once `is_in_check` and `all_legal_moves`
        exist -- the entire distinction between checkmate and stalemate is two
        booleans:

                          no legal moves    has legal moves
            in check      CHECKMATE         CHECK
            not in check  STALEMATE         ACTIVE

        Getting stalemate wrong (scoring it as a loss) is the classic bug --
        it is a DRAW, and it is the only cell in that table that surprises
        people.
        """
        has_moves = bool(self.all_legal_moves(self.current_turn))
        in_check = self.is_in_check(self.current_turn)

        if in_check and not has_moves:
            return GameStatus.CHECKMATE
        if not in_check and not has_moves:
            return GameStatus.STALEMATE
        if in_check:
            return GameStatus.CHECK
        return GameStatus.ACTIVE

    @property
    def is_over(self) -> bool:
        return self.status in (GameStatus.CHECKMATE, GameStatus.STALEMATE)

    # -- playing --------------------------------------------------------------

    def move(self, origin: Position, target: Position) -> Move:
        """
        Validate and play one move.

        Every rejection says WHY. The original just `return`ed on a bad move,
        so an illegal move was indistinguishable from a legal one that did
        nothing -- the single most frustrating thing a rules engine can do.
        """
        if self.is_over:
            raise IllegalMoveError(f"game is over ({self.status.name})")

        piece = self.board.piece_at(origin)
        if piece is None:
            raise IllegalMoveError(f"no piece on {to_algebraic(origin)}")
        if piece.color is not self.current_turn:
            raise IllegalMoveError(
                f"{to_algebraic(origin)} holds a {piece.color.name} piece, "
                f"but it is {self.current_turn.name}'s turn"
            )
        if not in_bounds(*target):
            raise IllegalMoveError("target is off the board")

        # R2, and the rest of the movement rule, in one membership test.
        if target not in piece.pseudo_legal_moves(self.board, origin):
            raise IllegalMoveError(
                f"a {piece.piece_type.name.lower()} cannot move "
                f"{to_algebraic(origin)} -> {to_algebraic(target)}"
            )

        candidate = Move(self.board, origin, target)
        if not self._is_legal(candidate):
            raise IllegalMoveError(
                f"{to_algebraic(origin)} -> {to_algebraic(target)} would leave "
                f"the {self.current_turn.name} king in check"
            )

        self.history.push(candidate)
        self.current_turn = self.current_turn.opponent
        return candidate

    def move_algebraic(self, origin: str, target: str) -> Move:
        """Convenience: game.move_algebraic('e2', 'e4')."""
        return self.move(from_algebraic(origin), from_algebraic(target))

    def undo(self) -> Optional[Move]:
        move = self.history.undo()
        if move is not None:
            self.current_turn = self.current_turn.opponent
        return move

    def redo(self) -> Optional[Move]:
        move = self.history.redo()
        if move is not None:
            self.current_turn = self.current_turn.opponent
        return move

    def show(self) -> None:
        print(self.board.render())
        print(f"  turn: {self.current_turn.name}   status: {self.status.name}")


# =============================================================================
#  SECTION 7 -- RUNNABLE DEMO
# =============================================================================


def _demo() -> None:
    print("=" * 70)
    print("SCENARIO 1: the opening position")
    print("=" * 70)
    game = Game()
    game.show()

    print()
    print("=" * 70)
    print("SCENARIO 2: pieces actually enforce their own movement rules")
    print("=" * 70)
    print("  (the original returned an empty move list for every piece,")
    print("   so literally any move was accepted)")
    for origin, target, note in [
        ("e2", "e4", "pawn double step from the start rank -- legal"),
        ("e2", "e5", "pawn three squares -- not a thing"),
        ("b1", "c3", "knight in an L -- legal"),
        ("b1", "b3", "knight straight ahead -- not a thing"),
        ("a1", "a4", "rook through its own pawn -- blocked"),
        ("d1", "d3", "queen through its own pawn -- blocked"),
    ]:
        probe = Game()
        try:
            probe.move_algebraic(origin, target)
            print(f"    {origin}->{target}  ACCEPTED  ({note})")
        except IllegalMoveError as error:
            print(f"    {origin}->{target}  REJECTED  ({error})")

    print()
    print("=" * 70)
    print("SCENARIO 3: turn order is enforced")
    print("=" * 70)
    game = Game()
    game.move_algebraic("e2", "e4")
    try:
        game.move_algebraic("d2", "d4")  # White again -- not allowed
    except IllegalMoveError as error:
        print(f"    rejected: {error}")

    print()
    print("=" * 70)
    print("SCENARIO 4: Scholar's Mate -- checkmate in 4 moves")
    print("=" * 70)
    game = Game()
    for origin, target in [
        ("e2", "e4"), ("e7", "e5"),
        ("f1", "c4"), ("b8", "c6"),
        ("d1", "h5"), ("g8", "f6"),
        ("h5", "f7"),  # Qxf7#
    ]:
        move = game.move_algebraic(origin, target)
        print(f"    {move}")
    game.show()
    print(f"  game over? {game.is_over}")

    print()
    print("=" * 70)
    print("SCENARIO 5: you may not make a move that exposes your own king")
    print("=" * 70)
    # Hand-built position: White Ke1, White Rd1 (pinned), Black Qd8.
    board = Board(empty=True)
    board.set_piece(from_algebraic("e1"), King(Color.WHITE))
    board.set_piece(from_algebraic("d1"), Rook(Color.WHITE))
    board.set_piece(from_algebraic("d8"), Queen(Color.BLACK))
    board.set_piece(from_algebraic("h8"), King(Color.BLACK))
    unpinned = Game(board)
    unpinned.show()
    print("  CONTROL CASE: the black queen attacks down the d-file, but the")
    print("  white king is on e1 -- off that file -- so the d1 rook is free:")
    print(f"    legal rook moves from d1: "
          f"{[to_algebraic(p) for p in unpinned.legal_moves_from(from_algebraic('d1'))]}")

    print()
    print("  NOW THE PIN: same rook, but on e2, directly between its own king")
    print("  on e1 and a black rook on e8.")
    board = Board(empty=True)
    board.set_piece(from_algebraic("e1"), King(Color.WHITE))
    board.set_piece(from_algebraic("e2"), Rook(Color.WHITE))
    board.set_piece(from_algebraic("e8"), Rook(Color.BLACK))
    board.set_piece(from_algebraic("a8"), King(Color.BLACK))
    pinned = Game(board)
    pinned.show()
    legal = [to_algebraic(p) for p in pinned.legal_moves_from(from_algebraic("e2"))]
    print(f"    legal rook moves from e2: {legal}")
    print("    ^ it can only move ALONG the pin line. Every sideways move is")
    print("      pseudo-legal but illegal -- and note that nothing in the Rook")
    print("      class knows what a pin is. Simulation found all of it.")

    print()
    print("=" * 70)
    print("SCENARIO 6: stalemate is a DRAW, not a win")
    print("=" * 70)
    # Black king on a8, White queen on c7, White king on c6. Black to move:
    # not in check, but every square is covered.
    board = Board(empty=True)
    board.set_piece(from_algebraic("a8"), King(Color.BLACK))
    board.set_piece(from_algebraic("c7"), Queen(Color.WHITE))
    board.set_piece(from_algebraic("c6"), King(Color.WHITE))
    stale = Game(board, turn=Color.BLACK)
    stale.show()
    print(f"  black in check? {stale.is_in_check(Color.BLACK)}")
    print(f"  black legal moves: {len(stale.all_legal_moves(Color.BLACK))}")
    print(f"  status: {stale.status.name}  <- no moves and NOT in check")

    print()
    print("=" * 70)
    print("SCENARIO 7: undo / redo, and the has_moved bug")
    print("=" * 70)
    game = Game()
    pawn = game.board.piece_at(from_algebraic("e2"))
    print(f"  before e2-e4:  has_moved = {pawn.has_moved}")
    game.move_algebraic("e2", "e4")
    print(f"  after  e2-e4:  has_moved = {pawn.has_moved}")
    game.undo()
    print(f"  after  undo:   has_moved = {pawn.has_moved}   <- restored")
    print("  ^ the original left this True forever, so an undone pawn silently")
    print("    lost its double-step. And since legality testing undoes moves")
    print("    constantly, it corrupted every piece within a few turns.")

    print()
    print("  full undo/redo cycle:")
    game = Game()
    for origin, target in [("e2", "e4"), ("e7", "e5"), ("g1", "f3")]:
        game.move_algebraic(origin, target)
    print(f"    played {len(game.history)} moves, turn = {game.current_turn.name}")
    game.undo()
    game.undo()
    print(f"    after 2 undos: {len(game.history)} moves, "
          f"turn = {game.current_turn.name}")
    game.redo()
    print(f"    after 1 redo:  {len(game.history)} moves, "
          f"turn = {game.current_turn.name}")
    game.move_algebraic("d2", "d4")  # a new move kills the redo branch
    print(f"    played a new move -- redo now returns {game.redo()}")

    print()
    print("=" * 70)
    print("SCENARIO 8: capturing your own piece is impossible")
    print("=" * 70)
    game = Game()
    try:
        game.move_algebraic("d1", "d2")  # queen onto its own pawn
    except IllegalMoveError as error:
        print(f"    rejected: {error}")


if __name__ == "__main__":
    _demo()
