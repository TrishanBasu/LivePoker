import itertools
from collections import Counter
from typing import List, Tuple, Dict, Any

RANKS = "23456789TJQKA"
SUITS = "shdc"

RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}
RANK_NAME_SINGULAR = {
    2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8",
    9: "9", 10: "10", 11: "Jack", 12: "Queen", 13: "King", 14: "Ace"
}
RANK_NAME_PLURAL = {
    2: "2s", 3: "3s", 4: "4s", 5: "5s", 6: "6s", 7: "7s", 8: "8s",
    9: "9s", 10: "10s", 11: "Jacks", 12: "Queens", 13: "Kings", 14: "Aces"
}

HAND_CATEGORY_NAMES = {
    9: "ROYAL FLUSH",
    8: "STRAIGHT FLUSH",
    7: "FOUR OF A KIND",
    6: "FULL HOUSE",
    5: "FLUSH",
    4: "STRAIGHT",
    3: "THREE OF A KIND",
    2: "TWO PAIR",
    1: "ONE PAIR",
    0: "HIGH CARD",
}


def parse_card(card: str) -> Tuple[int, str]:
    if not isinstance(card, str) or len(card) != 2:
        raise ValueError(f"Invalid card format: {card}")
    r, s = card[0].upper(), card[1].lower()
    if r not in RANK_VALUE or s not in SUITS:
        raise ValueError(f"Invalid card value: {card}")
    return RANK_VALUE[r], s


def evaluate_5_cards(cards: List[str]) -> Tuple[Tuple[int, ...], str]:
    """
    Evaluates an exact 5-card poker hand.
    Returns (score_tuple, human_description) where higher score_tuple wins.
    """
    parsed = [parse_card(c) for c in cards]
    ranks = sorted([r for r, _ in parsed], reverse=True)
    suits = [s for _, s in parsed]

    is_flush = len(set(suits)) == 1

    # Check straight
    unique_ranks = sorted(set(ranks), reverse=True)
    is_straight = False
    straight_high = 0
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]
        elif unique_ranks == [14, 5, 4, 3, 2]:
            is_straight = True
            straight_high = 5

    if is_straight and is_flush:
        if straight_high == 14:
            return (9, 14), "ROYAL FLUSH — Ace to 10"
        low_card = "Ace" if straight_high == 5 else RANK_NAME_SINGULAR[straight_high - 4]
        return (8, straight_high), f"STRAIGHT FLUSH — {low_card} to {RANK_NAME_SINGULAR[straight_high]}"

    counts = Counter(ranks)
    # Sort by frequency descending, then rank value descending
    grouped = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    freq_pattern = [cnt for _, cnt in grouped]

    if freq_pattern == [4, 1]:
        quad_rank = grouped[0][0]
        kicker = grouped[1][0]
        return (7, quad_rank, kicker), f"FOUR OF A KIND — Four {RANK_NAME_PLURAL[quad_rank]}"

    if freq_pattern == [3, 2]:
        trips_rank = grouped[0][0]
        pair_rank = grouped[1][0]
        return (
            (6, trips_rank, pair_rank),
            f"FULL HOUSE — {RANK_NAME_PLURAL[trips_rank]} full of {RANK_NAME_PLURAL[pair_rank]}"
        )

    if is_flush:
        return (
            (5, *ranks),
            f"FLUSH — {RANK_NAME_SINGULAR[ranks[0]]} High"
        )

    if is_straight:
        low_card = "Ace" if straight_high == 5 else RANK_NAME_SINGULAR[straight_high - 4]
        return (
            (4, straight_high),
            f"STRAIGHT — {low_card} to {RANK_NAME_SINGULAR[straight_high]}"
        )

    if freq_pattern == [3, 1, 1]:
        trips_rank = grouped[0][0]
        kickers = [grouped[1][0], grouped[2][0]]
        return (
            (3, trips_rank, *kickers),
            f"THREE OF A KIND — Three {RANK_NAME_PLURAL[trips_rank]}"
        )

    if freq_pattern == [2, 2, 1]:
        high_pair = grouped[0][0]
        low_pair = grouped[1][0]
        kicker = grouped[2][0]
        return (
            (2, high_pair, low_pair, kicker),
            f"TWO PAIR — {RANK_NAME_PLURAL[high_pair]} and {RANK_NAME_PLURAL[low_pair]}"
        )

    if freq_pattern == [2, 1, 1, 1]:
        pair_rank = grouped[0][0]
        kickers = [grouped[1][0], grouped[2][0], grouped[3][0]]
        return (
            (1, pair_rank, *kickers),
            f"ONE PAIR — Pair of {RANK_NAME_PLURAL[pair_rank]}"
        )

    return (
        (0, *ranks),
        f"HIGH CARD — {RANK_NAME_SINGULAR[ranks[0]]} High"
    )


def evaluate_best_hand(cards: List[str]) -> Dict[str, Any]:
    """
    Evaluates the best 5-card hand from 2 to 7 available cards.
    Pre-flop (2 cards) evaluates hole card strength cleanly.
    Flop/Turn/River (5-7 cards) selects the highest-scoring 5-card combination.
    """
    if not cards or len(cards) < 2:
        return {
            "score": (-1,),
            "category": "WAITING",
            "description": "Waiting for cards",
            "best_five": []
        }

    if len(cards) < 5:
        # Pre-flop 2-card summary for private display
        parsed = sorted([parse_card(c) for c in cards], key=lambda x: x[0], reverse=True)
        r1, r2 = parsed[0][0], parsed[1][0]
        if r1 == r2:
            return {
                "score": (1, r1),
                "category": "ONE PAIR",
                "description": f"ONE PAIR — Pocket {RANK_NAME_PLURAL[r1]}",
                "best_five": list(cards)
            }
        return {
            "score": (0, r1, r2),
            "category": "HIGH CARD",
            "description": f"HIGH CARD — {RANK_NAME_SINGULAR[r1]} High",
            "best_five": list(cards)
        }

    best_score = None
    best_desc = ""
    best_combo = []

    for combo in itertools.combinations(cards, 5):
        score, desc = evaluate_5_cards(list(combo))
        if best_score is None or score > best_score:
            best_score = score
            best_desc = desc
            best_combo = list(combo)

    return {
        "score": best_score,
        "category": HAND_CATEGORY_NAMES[best_score[0]],
        "description": best_desc,
        "best_five": best_combo
    }
