import secrets
import threading
import time
from typing import Dict, List, Optional, Any, Callable
from poker_evaluator import RANKS, SUITS, evaluate_best_hand

MAX_SEATS = 8
TURN_TIMEOUT_SECONDS = 45
DISCONNECT_GRACE_SECONDS = 120
NEXT_HAND_DELAY_SECONDS = 5


class SecureDeck:
    def __init__(self):
        self.cards: List[str] = [f"{r}{s}" for r in RANKS for s in SUITS]
        self.shuffle()

    def shuffle(self) -> None:
        # Fisher-Yates shuffle using Python's cryptographically secure secrets module
        for i in range(len(self.cards) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            self.cards[i], self.cards[j] = self.cards[j], self.cards[i]

    def draw(self, count: int = 1) -> List[str]:
        if len(self.cards) < count:
            raise RuntimeError("Deck exhausted")
        drawn = self.cards[:count]
        self.cards = self.cards[count:]
        return drawn


class TablePlayer:
    def __init__(self, session_id: str, name: str, seat: int, chips: int):
        self.session_id: str = session_id
        self.name: str = name
        self.seat: int = seat
        self.chips: int = chips
        self.hole_cards: List[str] = []
        self.current_bet: int = 0
        self.total_hand_bet: int = 0
        self.folded: bool = False
        self.all_in: bool = False
        self.in_hand: bool = False
        self.acted_this_round: bool = False
        self.connected: bool = True
        self.disconnected_at: Optional[float] = None
        self.last_action: Optional[str] = None
        self.sitting_out: bool = False

    def reset_for_hand(self) -> None:
        self.hole_cards = []
        self.current_bet = 0
        self.total_hand_bet = 0
        self.folded = False
        self.all_in = False
        self.in_hand = self.chips > 0 and not self.sitting_out
        self.acted_this_round = False
        self.last_action = None


class PokerTable:
    def __init__(
        self,
        table_id: str,
        room_code: str,
        name: str,
        starting_chips: int = 1000,
        small_blind: int = 10,
        big_blind: int = 20,
        on_state_change: Optional[Callable[[str], None]] = None,
        on_player_removed: Optional[Callable[[str, str], None]] = None,
    ):
        self.table_id: str = table_id
        self.room_code: str = room_code
        self.name: str = name
        self.starting_chips: int = max(100, int(starting_chips))
        self.small_blind: int = max(1, int(small_blind))
        self.big_blind: int = max(2, int(big_blind))

        self.lock = threading.RLock()
        self.seats: Dict[int, Optional[TablePlayer]] = {i: None for i in range(MAX_SEATS)}

        self.game_started: bool = False
        self.phase: str = "WAITING"  # WAITING, PREFLOP, FLOP, TURN, RIVER, SHOWDOWN
        self.hand_number: int = 0
        self.deck: Optional[SecureDeck] = None
        self.community_cards: List[str] = []

        self.dealer_seat: Optional[int] = None
        self.sb_seat: Optional[int] = None
        self.bb_seat: Optional[int] = None
        self.current_turn_seat: Optional[int] = None

        self.current_bet: int = 0
        self.min_raise: int = self.big_blind
        self.turn_deadline: Optional[float] = None
        self.turn_token: Optional[str] = None

        self.activity_log: List[Dict[str, Any]] = []
        self.hand_history: List[Dict[str, Any]] = []
        self.chat_messages: List[Dict[str, Any]] = []
        self.last_winners: List[Dict[str, Any]] = []
        self.showdown_revealed: Dict[str, List[str]] = {}

        self._on_state_change = on_state_change
        self._on_player_removed = on_player_removed
        self._turn_timer: Optional[threading.Timer] = None
        self._next_hand_timer: Optional[threading.Timer] = None
        self._closed: bool = False

    def _notify(self) -> None:
        if self._on_state_change and not self._closed:
            self._on_state_change(self.table_id)

    def _log(self, message: str, category: str = "game") -> None:
        entry = {
            "id": secrets.token_hex(4),
            "timestamp": time.time(),
            "message": message,
            "category": category,
        }
        self.activity_log.append(entry)
        if len(self.activity_log) > 100:
            self.activity_log = self.activity_log[-100:]

    def add_chat(self, session_id: str, text: str) -> bool:
        with self.lock:
            player = self.get_player_by_session(session_id)
            if not player:
                return False
            clean = text.strip()[:200]
            if not clean:
                return False
            self.chat_messages.append({
                "id": secrets.token_hex(4),
                "sender": player.name,
                "seat": player.seat,
                "text": clean,
                "timestamp": time.time(),
            })
            if len(self.chat_messages) > 80:
                self.chat_messages = self.chat_messages[-80:]
            self._notify()
            return True

    def get_player_by_session(self, session_id: str) -> Optional[TablePlayer]:
        for p in self.seats.values():
            if p and p.session_id == session_id:
                return p
        return None

    def seated_players(self) -> List[TablePlayer]:
        return [self.seats[s] for s in range(MAX_SEATS) if self.seats[s] is not None]

    def active_hand_players(self) -> List[TablePlayer]:
        return [p for p in self.seated_players() if p.in_hand and not p.folded]

    def players_who_can_act(self) -> List[TablePlayer]:
        return [p for p in self.seated_players() if p.in_hand and not p.folded and not p.all_in]

    def seat_player(self, session_id: str, name: str, preferred_seat: Optional[int] = None) -> Dict[str, Any]:
        with self.lock:
            if self._closed:
                return {"ok": False, "error": "Table is closed."}

            existing = self.get_player_by_session(session_id)
            if existing:
                existing.connected = True
                existing.disconnected_at = None
                self._notify()
                return {"ok": True, "seat": existing.seat, "reconnected": True}

            target_seat = None
            if preferred_seat is not None and 0 <= preferred_seat < MAX_SEATS and self.seats[preferred_seat] is None:
                target_seat = preferred_seat
            else:
                for s in range(MAX_SEATS):
                    if self.seats[s] is None:
                        target_seat = s
                        break

            if target_seat is None:
                return {"ok": False, "error": "Table is full (maximum 8 players)."}

            player = TablePlayer(
                session_id=session_id,
                name=name,
                seat=target_seat,
                chips=self.starting_chips,
            )
            self.seats[target_seat] = player
            self._log(f"{name} joined Seat {target_seat + 1} with {self.starting_chips:,} chips.", "join")
            self._notify()
            return {"ok": True, "seat": target_seat, "reconnected": False}

    def mark_disconnected(self, session_id: str) -> None:
        with self.lock:
            player = self.get_player_by_session(session_id)
            if not player:
                return
            player.connected = False
            player.disconnected_at = time.time()
            self._log(f"{player.name} disconnected. Seat reserved for 2 minutes.", "system")
            self._notify()

    def mark_reconnected(self, session_id: str) -> Optional[TablePlayer]:
        with self.lock:
            player = self.get_player_by_session(session_id)
            if not player:
                return None
            was_disconnected = not player.connected
            player.connected = True
            player.disconnected_at = None
            if was_disconnected:
                self._log(f"{player.name} reconnected.", "system")
            self._notify()
            return player

    def cleanup_expired_disconnects(self) -> List[str]:
        removed_sessions = []
        with self.lock:
            now = time.time()
            for seat, player in list(self.seats.items()):
                if player and not player.connected and player.disconnected_at:
                    if now - player.disconnected_at >= DISCONNECT_GRACE_SECONDS:
                        sid = player.session_id
                        self.remove_player(sid, reason="disconnection timeout")
                        removed_sessions.append(sid)
        return removed_sessions

    def remove_player(self, session_id: str, reason: str = "left the table") -> bool:
        with self.lock:
            player = self.get_player_by_session(session_id)
            if not player:
                return False

            seat = player.seat
            name = player.name
            was_their_turn = (self.current_turn_seat == seat and self.phase not in ("WAITING", "SHOWDOWN"))
            was_in_hand = player.in_hand and not player.folded

            self.seats[seat] = None
            self._log(f"{name} {reason} (Seat {seat + 1}).", "leave")

            if self._on_player_removed:
                self._on_player_removed(self.table_id, session_id)

            # Check if active hand needs resolution
            if self.phase not in ("WAITING", "SHOWDOWN"):
                remaining = self.active_hand_players()
                if len(remaining) <= 1:
                    self._cancel_turn_timer()
                    if len(remaining) == 1:
                        self._award_uncontested_winner(remaining[0])
                    else:
                        self._finish_hand_cleanup()
                elif was_their_turn:
                    self._cancel_turn_timer()
                    self._advance_turn_after_action(seat)
                elif was_in_hand and self._is_betting_round_complete():
                    self._cancel_turn_timer()
                    self._advance_street()

            if len(self.seated_players()) < 2 and self.phase in ("WAITING", "SHOWDOWN"):
                self.game_started = False
                self.phase = "WAITING"
                self._cancel_next_hand_timer()

            self._notify()
            return True

    def configure_stakes(self, starting_chips: int, small_blind: int, big_blind: int) -> Dict[str, Any]:
        with self.lock:
            if self.phase not in ("WAITING", "SHOWDOWN"):
                return {"ok": False, "error": "Cannot change stakes during an active hand."}
            if starting_chips < 50 or small_blind < 1 or big_blind <= small_blind:
                return {"ok": False, "error": "Invalid blind or starting stack values."}
            self.starting_chips = int(starting_chips)
            self.small_blind = int(small_blind)
            self.big_blind = int(big_blind)
            self.min_raise = self.big_blind
            self._log(
                f"Host updated stakes: Starting {self.starting_chips:,}, Blinds {self.small_blind}/{self.big_blind}.",
                "host",
            )
            self._notify()
            return {"ok": True}

    def host_adjust_chips(self, target_session_id: str, mode: str, amount: int) -> Dict[str, Any]:
        with self.lock:
            player = self.get_player_by_session(target_session_id)
            if not player:
                return {"ok": False, "error": "Player not found at this table."}
            if amount < 0:
                return {"ok": False, "error": "Chip amount cannot be negative."}

            old_chips = player.chips
            if mode == "add":
                player.chips += amount
            elif mode == "reduce":
                player.chips = max(0, player.chips - amount)
            elif mode == "set":
                player.chips = amount
            else:
                return {"ok": False, "error": "Invalid chip operation."}

            self._log(
                f"Host adjusted {player.name}'s chips: {old_chips:,} -> {player.chips:,}.",
                "host",
            )
            self._notify()
            return {"ok": True, "new_chips": player.chips}

    def start_game(self) -> Dict[str, Any]:
        with self.lock:
            if self.phase not in ("WAITING", "SHOWDOWN"):
                return {"ok": False, "error": "A hand is already in progress."}
            eligible = [p for p in self.seated_players() if p.chips > 0]
            if len(eligible) < 2:
                return {"ok": False, "error": "At least 2 players with chips are required to start."}
            self.game_started = True
            self._start_new_hand()
            return {"ok": True}

    def end_game(self) -> Dict[str, Any]:
        with self.lock:
            self._cancel_turn_timer()
            self._cancel_next_hand_timer()
            self.game_started = False
            self.phase = "WAITING"
            self.community_cards = []
            self.current_turn_seat = None
            self.turn_deadline = None
            self.current_bet = 0
            for p in self.seated_players():
                p.reset_for_hand()
                p.in_hand = False
            self._log("Host ended the active game session.", "host")
            self._notify()
            return {"ok": True}

    def close_table(self) -> None:
        with self.lock:
            self._closed = True
            self._cancel_turn_timer()
            self._cancel_next_hand_timer()

    def _next_occupied_seat(self, start_seat: int, eligible_only: bool = True) -> Optional[int]:
        for offset in range(1, MAX_SEATS + 1):
            s = (start_seat + offset) % MAX_SEATS
            p = self.seats[s]
            if p is not None:
                if not eligible_only or (p.in_hand and not p.folded and not p.all_in):
                    return s
        return None

    def _next_in_hand_seat(self, start_seat: int) -> Optional[int]:
        for offset in range(1, MAX_SEATS + 1):
            s = (start_seat + offset) % MAX_SEATS
            p = self.seats[s]
            if p is not None and p.in_hand:
                return s
        return None

    def _start_new_hand(self) -> None:
        with self.lock:
            self._cancel_turn_timer()
            self._cancel_next_hand_timer()

            if not self.game_started:
                return

            for p in self.seated_players():
                p.reset_for_hand()

            in_hand = [p for p in self.seated_players() if p.in_hand]
            if len(in_hand) < 2:
                self.game_started = False
                self.phase = "WAITING"
                self._log("Waiting for at least 2 players with chips.", "system")
                self._notify()
                return

            self.hand_number += 1
            self.deck = SecureDeck()
            self.community_cards = []
            self.last_winners = []
            self.showdown_revealed = {}
            self.current_bet = 0
            self.min_raise = self.big_blind

            # Rotate dealer button
            if self.dealer_seat is None or self.seats.get(self.dealer_seat) is None or not self.seats[self.dealer_seat].in_hand:
                self.dealer_seat = in_hand[0].seat
            else:
                self.dealer_seat = self._next_in_hand_seat(self.dealer_seat)

            # Determine blinds (Heads-up rule: Dealer is SB, other player is BB)
            if len(in_hand) == 2:
                self.sb_seat = self.dealer_seat
                self.bb_seat = self._next_in_hand_seat(self.sb_seat)
            else:
                self.sb_seat = self._next_in_hand_seat(self.dealer_seat)
                self.bb_seat = self._next_in_hand_seat(self.sb_seat)

            self._log(f"--- Hand #{self.hand_number} started (Dealer: {self.seats[self.dealer_seat].name}) ---", "hand")

            # Post blinds
            self._post_blind(self.sb_seat, self.small_blind, "Small Blind")
            self._post_blind(self.bb_seat, self.big_blind, "Big Blind")

            self.current_bet = max(
                self.seats[self.sb_seat].current_bet,
                self.seats[self.bb_seat].current_bet,
            )
            self.min_raise = self.big_blind

            # Deal 2 private hole cards clockwise starting left of dealer
            deal_order = []
            curr = self.dealer_seat
            for _ in range(len(in_hand)):
                curr = self._next_in_hand_seat(curr)
                deal_order.append(curr)

            for _ in range(2):
                for s in deal_order:
                    self.seats[s].hole_cards.extend(self.deck.draw(1))

            self.phase = "PREFLOP"

            # First to act pre-flop: left of BB (in heads-up, SB/Dealer acts first)
            first_actor = self._next_occupied_seat(self.bb_seat, eligible_only=True)
            if first_actor is None or len(self.players_who_can_act()) <= 1 and self._all_bets_matched():
                self._runout_to_showdown()
            else:
                self._set_turn(first_actor)

            self._notify()

    def _post_blind(self, seat: int, amount: int, label: str) -> None:
        player = self.seats[seat]
        posted = min(player.chips, amount)
        player.chips -= posted
        player.current_bet += posted
        player.total_hand_bet += posted
        if player.chips == 0:
            player.all_in = True
            self._log(f"{player.name} posts {label} ({posted:,}) and is ALL-IN.", "bet")
        else:
            self._log(f"{player.name} posts {label} ({posted:,}).", "bet")

    def _set_turn(self, seat: int) -> None:
        self._cancel_turn_timer()
        self.current_turn_seat = seat
        self.turn_deadline = time.time() + TURN_TIMEOUT_SECONDS
        token = secrets.token_hex(8)
        self.turn_token = token

        timer = threading.Timer(TURN_TIMEOUT_SECONDS, self._handle_turn_timeout, args=(seat, token))
        timer.daemon = True
        self._turn_timer = timer
        timer.start()

    def _cancel_turn_timer(self) -> None:
        if self._turn_timer:
            self._turn_timer.cancel()
            self._turn_timer = None
        self.turn_token = None
        self.turn_deadline = None

    def _cancel_next_hand_timer(self) -> None:
        if self._next_hand_timer:
            self._next_hand_timer.cancel()
            self._next_hand_timer = None

    def _handle_turn_timeout(self, expected_seat: int, expected_token: str) -> None:
        with self.lock:
            if self._closed or self.phase in ("WAITING", "SHOWDOWN"):
                return
            if self.current_turn_seat != expected_seat or self.turn_token != expected_token:
                return
            player = self.seats.get(expected_seat)
            if not player:
                return

            to_call = self.current_bet - player.current_bet
            if to_call <= 0:
                self._log(f"{player.name} timed out — auto-CHECK.", "timeout")
                self._execute_validated_action(player, "check", 0)
            else:
                self._log(f"{player.name} timed out — auto-FOLD.", "timeout")
                self._execute_validated_action(player, "fold", 0)

    def get_legal_actions(self, session_id: str) -> Dict[str, Any]:
        with self.lock:
            player = self.get_player_by_session(session_id)
            if (
                not player
                or self.phase in ("WAITING", "SHOWDOWN")
                or self.current_turn_seat != player.seat
                or player.folded
                or player.all_in
            ):
                return {"is_turn": False, "actions": []}

            to_call = max(0, self.current_bet - player.current_bet)
            actions = ["fold"]

            if to_call == 0:
                actions.append("check")
            else:
                actions.append("call")

            max_total_bet = player.current_bet + player.chips
            min_total_raise = self.current_bet + self.min_raise

            if player.chips > to_call:
                if max_total_bet >= min_total_raise:
                    actions.append("raise" if self.current_bet > 0 else "bet")
                actions.append("all_in")
            elif player.chips > 0 and to_call >= player.chips:
                # Call amount puts player all-in
                actions.append("all_in")

            return {
                "is_turn": True,
                "actions": actions,
                "to_call": min(to_call, player.chips),
                "min_raise_to": min(min_total_raise, max_total_bet),
                "max_raise_to": max_total_bet,
                "current_bet": self.current_bet,
                "player_bet": player.current_bet,
            }

    def process_action(self, session_id: str, action: str, amount: int = 0) -> Dict[str, Any]:
        with self.lock:
            if self._closed or self.phase in ("WAITING", "SHOWDOWN"):
                return {"ok": False, "error": "No active betting round."}

            player = self.get_player_by_session(session_id)
            if not player:
                return {"ok": False, "error": "Player is not seated at this table."}

            if self.current_turn_seat != player.seat:
                return {"ok": False, "error": "It is not your turn."}

            if self.turn_deadline and time.time() > self.turn_deadline + 0.5:
                return {"ok": False, "error": "Turn timer expired."}

            if player.folded or player.all_in or not player.in_hand:
                return {"ok": False, "error": "Player cannot act."}

            action = action.lower().strip()
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                amount = 0

            return self._execute_validated_action(player, action, amount)

    def _execute_validated_action(self, player: TablePlayer, action: str, amount: int) -> Dict[str, Any]:
        to_call = max(0, self.current_bet - player.current_bet)
        max_total = player.current_bet + player.chips

        if action == "fold":
            self._cancel_turn_timer()
            player.folded = True
            player.acted_this_round = True
            player.last_action = "Fold"
            self._log(f"{player.name} folds.", "action")

        elif action == "check":
            if to_call != 0:
                return {"ok": False, "error": f"Cannot check; {to_call} required to call."}
            self._cancel_turn_timer()
            player.acted_this_round = True
            player.last_action = "Check"
            self._log(f"{player.name} checks.", "action")

        elif action == "call":
            if to_call <= 0:
                return {"ok": False, "error": "Nothing to call; you can check."}
            self._cancel_turn_timer()
            call_amt = min(to_call, player.chips)
            player.chips -= call_amt
            player.current_bet += call_amt
            player.total_hand_bet += call_amt
            player.acted_this_round = True
            if player.chips == 0:
                player.all_in = True
                player.last_action = f"Call All-In ({player.current_bet:,})"
                self._log(f"{player.name} calls {call_amt:,} and is ALL-IN.", "bet")
            else:
                player.last_action = f"Call ({call_amt:,})"
                self._log(f"{player.name} calls {call_amt:,}.", "action")

        elif action in ("bet", "raise"):
            if player.chips <= to_call:
                return {"ok": False, "error": "Insufficient chips to raise; use all-in."}
            min_target = self.current_bet + self.min_raise
            # Allow all-in even if slightly below min_raise
            if amount < min_target and amount != max_total:
                return {"ok": False, "error": f"Minimum total bet/raise is {min_target:,}."}
            if amount > max_total:
                return {"ok": False, "error": "Bet exceeds available chips."}

            self._cancel_turn_timer()
            added = amount - player.current_bet
            raise_increment = amount - self.current_bet

            player.chips -= added
            player.current_bet = amount
            player.total_hand_bet += added
            player.acted_this_round = True

            if raise_increment >= self.min_raise:
                self.min_raise = raise_increment
                # Full raise re-opens action for all other active players
                for other in self.players_who_can_act():
                    if other.seat != player.seat:
                        other.acted_this_round = False

            self.current_bet = amount

            if player.chips == 0:
                player.all_in = True
                player.last_action = f"All-In ({amount:,})"
                self._log(f"{player.name} raises to {amount:,} (ALL-IN).", "bet")
            else:
                verb = "bets" if to_call == 0 and self.current_bet == amount else "raises to"
                player.last_action = f"Raise ({amount:,})"
                self._log(f"{player.name} {verb} {amount:,}.", "bet")

        elif action == "all_in":
            if player.chips <= 0:
                return {"ok": False, "error": "No chips remaining."}
            self._cancel_turn_timer()
            target_bet = max_total
            added = player.chips
            raise_increment = target_bet - self.current_bet

            player.chips = 0
            player.current_bet = target_bet
            player.total_hand_bet += added
            player.all_in = True
            player.acted_this_round = True
            player.last_action = f"All-In ({target_bet:,})"

            if target_bet > self.current_bet:
                if raise_increment >= self.min_raise:
                    self.min_raise = raise_increment
                    for other in self.players_who_can_act():
                        if other.seat != player.seat:
                            other.acted_this_round = False
                self.current_bet = target_bet

            self._log(f"{player.name} goes ALL-IN for {target_bet:,}!", "bet")

        else:
            return {"ok": False, "error": f"Unknown action: {action}"}

        # Check immediate single-survivor fold victory
        remaining = self.active_hand_players()
        if len(remaining) == 1:
            self._award_uncontested_winner(remaining[0])
            self._notify()
            return {"ok": True}

        self._advance_turn_after_action(player.seat)
        self._notify()
        return {"ok": True}

    def _all_bets_matched(self) -> bool:
        for p in self.players_who_can_act():
            if p.current_bet != self.current_bet:
                return False
        return True

    def _is_betting_round_complete(self) -> bool:
        can_act = self.players_who_can_act()
        if not can_act:
            return True
        for p in can_act:
            if not p.acted_this_round or p.current_bet != self.current_bet:
                return False
        return True

    def _advance_turn_after_action(self, last_seat: int) -> None:
        if self._is_betting_round_complete():
            self._advance_street()
            return

        next_seat = self._next_occupied_seat(last_seat, eligible_only=True)
        if next_seat is None:
            self._advance_street()
        else:
            self._set_turn(next_seat)

    def _advance_street(self) -> None:
        # Reset street bets
        for p in self.seated_players():
            p.current_bet = 0
            p.acted_this_round = False
            if not p.folded and not p.all_in:
                p.last_action = None

        self.current_bet = 0
        self.min_raise = self.big_blind

        if len(self.players_who_can_act()) <= 1:
            self._runout_to_showdown()
            return

        if self.phase == "PREFLOP":
            self.phase = "FLOP"
            self.deck.draw(1)  # Burn card
            self.community_cards.extend(self.deck.draw(3))
            self._log(f"FLOP: {' '.join(self.community_cards)}", "street")
        elif self.phase == "FLOP":
            self.phase = "TURN"
            self.deck.draw(1)
            self.community_cards.extend(self.deck.draw(1))
            self._log(f"TURN: {' '.join(self.community_cards)}", "street")
        elif self.phase == "TURN":
            self.phase = "RIVER"
            self.deck.draw(1)
            self.community_cards.extend(self.deck.draw(1))
            self._log(f"RIVER: {' '.join(self.community_cards)}", "street")
        elif self.phase == "RIVER":
            self._resolve_showdown()
            return

        first_postflop = self._next_occupied_seat(self.dealer_seat, eligible_only=True)
        if first_postflop is None:
            self._runout_to_showdown()
        else:
            self._set_turn(first_postflop)

    def _runout_to_showdown(self) -> None:
        while len(self.community_cards) < 5:
            self.deck.draw(1)
            self.community_cards.extend(self.deck.draw(1))
        self._log(f"Board runout: {' '.join(self.community_cards)}", "street")
        self._resolve_showdown()

    def calculate_pots(self) -> List[Dict[str, Any]]:
        """
        Constructs exact main pot and side pots from total_hand_bet across all contributors,
        handling uncalled excess bets accurately.
        """
        contributors = [p for p in self.seated_players() if p.total_hand_bet > 0]
        if not contributors:
            return []

        levels = sorted(set(p.total_hand_bet for p in contributors))
        pots = []
        prev_level = 0

        for level in levels:
            slice_amt = level - prev_level
            participants = [p for p in contributors if p.total_hand_bet >= level]
            pot_amount = slice_amt * len(participants)
            eligible = [p for p in participants if not p.folded and p.in_hand]

            pots.append({
                "name": "Main Pot" if not pots else f"Side Pot {len(pots)}",
                "amount": pot_amount,
                "eligible_sessions": [p.session_id for p in eligible],
                "all_contributor_sessions": [p.session_id for p in participants],
            })
            prev_level = level

        return pots

    def _award_uncontested_winner(self, winner: TablePlayer) -> None:
        self._cancel_turn_timer()
        total_pot = sum(p.total_hand_bet for p in self.seated_players())
        winner.chips += total_pot
        self.phase = "SHOWDOWN"
        self.current_turn_seat = None
        self.last_winners = [{
            "session_id": winner.session_id,
            "name": winner.name,
            "seat": winner.seat,
            "amount": total_pot,
            "pot_name": "Main Pot",
            "hand_description": "Opponents folded",
        }]
        self._log(f"{winner.name} wins {total_pot:,} chips (all opponents folded).", "win")
        self._record_hand_history()
        self._schedule_next_hand()

    def _resolve_showdown(self) -> None:
        self._cancel_turn_timer()
        self.phase = "SHOWDOWN"
        self.current_turn_seat = None

        active = self.active_hand_players()
        # Reveal hole cards only for active showdown contenders
        self.showdown_revealed = {p.session_id: list(p.hole_cards) for p in active}

        evaluations: Dict[str, Dict[str, Any]] = {}
        for p in active:
            evaluations[p.session_id] = evaluate_best_hand(p.hole_cards + self.community_cards)

        pots = self.calculate_pots()
        winners_summary: List[Dict[str, Any]] = []

        # Seat order clockwise from left of dealer for odd-chip remainder distribution
        seat_order = []
        if self.dealer_seat is not None:
            for offset in range(1, MAX_SEATS + 1):
                seat_order.append((self.dealer_seat + offset) % MAX_SEATS)
        else:
            seat_order = list(range(MAX_SEATS))

        for pot in pots:
            eligible_players = [
                self.get_player_by_session(sid)
                for sid in pot["eligible_sessions"]
                if self.get_player_by_session(sid) is not None
            ]

            # Uncalled bet refund if only 1 contributor or 1 eligible player
            if len(eligible_players) == 1:
                solo = eligible_players[0]
                solo.chips += pot["amount"]
                self._log(f"Uncalled bet of {pot['amount']:,} returned to {solo.name}.", "win")
                continue

            if not eligible_players:
                continue

            best_score = max(evaluations[p.session_id]["score"] for p in eligible_players)
            pot_winners = [p for p in eligible_players if evaluations[p.session_id]["score"] == best_score]

            # Sort winners clockwise from dealer for odd chip rule
            pot_winners.sort(key=lambda p: seat_order.index(p.seat))

            share = pot["amount"] // len(pot_winners)
            remainder = pot["amount"] % len(pot_winners)

            for idx, w in enumerate(pot_winners):
                won = share + (1 if idx < remainder else 0)
                w.chips += won
                desc = evaluations[w.session_id]["description"]
                winners_summary.append({
                    "session_id": w.session_id,
                    "name": w.name,
                    "seat": w.seat,
                    "amount": won,
                    "pot_name": pot["name"],
                    "hand_description": desc,
                })
                self._log(f"{w.name} wins {won:,} from {pot['name']} with {desc}.", "win")

        self.last_winners = winners_summary
        self._record_hand_history()
        self._schedule_next_hand()

    def _finish_hand_cleanup(self) -> None:
        self.phase = "WAITING"
        self.current_turn_seat = None
        self.community_cards = []
        self._notify()

    def _record_hand_history(self) -> None:
        record = {
            "hand_number": self.hand_number,
            "community_cards": list(self.community_cards),
            "winners": list(self.last_winners),
            "revealed_hands": {
                self.get_player_by_session(sid).name: cards
                for sid, cards in self.showdown_revealed.items()
                if self.get_player_by_session(sid)
            },
            "timestamp": time.time(),
        }
        self.hand_history.insert(0, record)
        if len(self.hand_history) > 25:
            self.hand_history = self.hand_history[:25]

    def _schedule_next_hand(self) -> None:
        self._cancel_next_hand_timer()
        if not self.game_started:
            return
        timer = threading.Timer(NEXT_HAND_DELAY_SECONDS, self._auto_next_hand_callback)
        timer.daemon = True
        self._next_hand_timer = timer
        timer.start()

    def _auto_next_hand_callback(self) -> None:
        with self.lock:
            if self._closed or not self.game_started:
                return
            self._start_new_hand()

    def serialize_for_player(self, viewer_session_id: str) -> Dict[str, Any]:
        """
        Builds a strictly isolated view for a specific player.
        NEVER leaks opponent hole cards or deck state.
        """
        with self.lock:
            viewer = self.get_player_by_session(viewer_session_id)
            pots = self.calculate_pots()
            total_pot = sum(p["amount"] for p in pots)

            seats_data = []
            for s in range(MAX_SEATS):
                p = self.seats[s]
                if p is None:
                    seats_data.append(None)
                    continue

                # Determine visible cards for this seat
                if p.session_id == viewer_session_id:
                    cards = list(p.hole_cards)
                elif self.phase == "SHOWDOWN" and p.session_id in self.showdown_revealed:
                    cards = list(self.showdown_revealed[p.session_id])
                elif p.in_hand and not p.folded and self.phase != "WAITING":
                    cards = ["??", "??"]
                else:
                    cards = []

                seats_data.append({
                    "seat": p.seat,
                    "session_id": p.session_id,
                    "name": p.name,
                    "chips": p.chips,
                    "current_bet": p.current_bet,
                    "folded": p.folded,
                    "all_in": p.all_in,
                    "in_hand": p.in_hand,
                    "connected": p.connected,
                    "last_action": p.last_action,
                    "cards": cards,
                    "is_dealer": self.dealer_seat == p.seat,
                    "is_sb": self.sb_seat == p.seat,
                    "is_bb": self.bb_seat == p.seat,
                    "is_turn": self.current_turn_seat == p.seat and self.phase not in ("WAITING", "SHOWDOWN"),
                })

            personal_eval = None
            if viewer and viewer.hole_cards:
                personal_eval = evaluate_best_hand(viewer.hole_cards + self.community_cards)

            legal_actions = self.get_legal_actions(viewer_session_id) if viewer else {"is_turn": False, "actions": []}

            remaining_seconds = 0
            if self.turn_deadline and self.phase not in ("WAITING", "SHOWDOWN"):
                remaining_seconds = max(0, int(round(self.turn_deadline - time.time())))

            return {
                "table_id": self.table_id,
                "room_code": self.room_code,
                "name": self.name,
                "starting_chips": self.starting_chips,
                "small_blind": self.small_blind,
                "big_blind": self.big_blind,
                "game_started": self.game_started,
                "phase": self.phase,
                "hand_number": self.hand_number,
                "community_cards": list(self.community_cards),
                "total_pot": total_pot,
                "pots": [{"name": pt["name"], "amount": pt["amount"]} for pt in pots],
                "current_bet": self.current_bet,
                "current_turn_seat": self.current_turn_seat,
                "turn_remaining_seconds": remaining_seconds,
                "turn_total_seconds": TURN_TIMEOUT_SECONDS,
                "seats": seats_data,
                "viewer_seat": viewer.seat if viewer else None,
                "personal_hand_eval": personal_eval,
                "legal_actions": legal_actions,
                "last_winners": list(self.last_winners),
                "activity_log": self.activity_log[-30:],
                "hand_history": self.hand_history[:10],
                "chat_messages": self.chat_messages[-40:],
            }
