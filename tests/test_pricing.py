"""Unit tests for modules.pricing.AggressivePricer."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.pricing import AggressivePricer


def _tick(last=100.0, bid=None, ask=None, bid_field="bid_price_1", ask_field="ask_price_1"):
    attrs = {"last_price": last}
    if bid is not None:
        attrs[bid_field] = bid
    if ask is not None:
        attrs[ask_field] = ask
    return SimpleNamespace(**attrs)


class TestUpdate:
    def test_last_price_captured(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        p.update(_tick(last=25500))
        assert p.last == 25500

    def test_bid_ask_captured(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        p.update(_tick(last=25500, bid=25495, ask=25505))
        assert p.bid1 == 25495
        assert p.ask1 == 25505
        assert p.has_book

    def test_fallback_when_no_book(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        p.update(_tick(last=25500))
        assert not p.has_book

    def test_alt_field_names_bid_price1_no_underscore(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        p.update(_tick(last=25500, bid=25495, ask=25505,
                       bid_field="bid_price1", ask_field="ask_price1"))
        assert p.bid1 == 25495

    def test_rejects_invalid_book_ask_below_bid(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        p.update(_tick(last=25500, bid=25505, ask=25495))   # inverted, reject
        assert not p.has_book


class TestTypicalSpread:
    def test_no_data_defaults_to_1_tick(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        assert p.typical_spread_ticks == 1

    def test_rolling_median(self):
        p = AggressivePricer(tick_size=5.0, spread_window=5, dump_first_tick=False)
        # spreads in ticks: 1,1,1,5,1 → median=1
        for last, bid, ask in [(100, 99.5, 100.5), (100, 99.5, 100.5),
                                 (100, 99.5, 100.5), (100, 97.5, 100.0),
                                 (100, 99.5, 100.5)]:
            p.update(_tick(last=last, bid=bid, ask=ask))
        # bid/ask spreads: 1, 1, 1, 0.5 (rejected since ask>bid required? actually
        # 100.0-97.5=2.5 ticks, spread=1,1,1,2,1 → median=1. But if last sample
        # stored, 5 samples → sorted middle idx 2 → 1
        assert p.typical_spread_ticks == 1

    def test_wide_spread_reflected(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        # All samples 4-tick wide
        for _ in range(20):
            p.update(_tick(last=100, bid=90, ask=110))   # spread=20=4 ticks
        assert p.typical_spread_ticks == 4


class TestPriceUrgency:
    @pytest.fixture
    def pricer_with_book(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        p.update(_tick(last=100, bid=99.5, ask=100.5))   # tight, spread=1 tick
        return p

    def test_passive_buy_uses_bid(self, pricer_with_book):
        assert pricer_with_book.price("buy", "passive") == 99.5

    def test_passive_sell_uses_ask(self, pricer_with_book):
        assert pricer_with_book.price("sell", "passive") == 100.5

    def test_normal_buy_uses_ask(self, pricer_with_book):
        assert pricer_with_book.price("buy", "normal") == 100.5

    def test_normal_sell_uses_bid(self, pricer_with_book):
        assert pricer_with_book.price("sell", "normal") == 99.5

    def test_cross_buy_adds_2_ticks_to_ask(self, pricer_with_book):
        # tick=5, cross +2 ticks above ask
        assert pricer_with_book.price("buy", "cross") == 100.5 + 2 * 5

    def test_urgent_buy_adds_5_ticks(self, pricer_with_book):
        assert pricer_with_book.price("buy", "urgent") == 100.5 + 5 * 5

    def test_critical_sell_subtracts_10_ticks(self, pricer_with_book):
        assert pricer_with_book.price("sell", "critical") == 99.5 - 10 * 5

    def test_invalid_direction_raises(self, pricer_with_book):
        with pytest.raises(ValueError):
            pricer_with_book.price("long", "urgent")

    def test_invalid_urgency_raises(self, pricer_with_book):
        with pytest.raises(ValueError):
            pricer_with_book.price("buy", "extreme")


class TestSpreadAdaptation:
    def test_wide_spread_adds_extra_to_cross_urgent(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        # Build typical spread = 4 ticks
        for _ in range(20):
            p.update(_tick(last=100, bid=90, ask=110))
        assert p.typical_spread_ticks == 4

        # Last tick: bid=90, ask=110, typical_spread=4, extra=3
        # cross: base=2 + extra=3 = 5 ticks above ask
        assert p.price("buy", "cross") == 110 + 5 * 5

        # urgent: base=5 + extra=3 = 8 ticks
        assert p.price("buy", "urgent") == 110 + 8 * 5

    def test_tight_spread_no_extra(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        for _ in range(20):
            p.update(_tick(last=100, bid=99.5, ask=100.5))  # spread=1 tick
        # cross: base=2, no extra
        assert p.price("buy", "cross") == 100.5 + 2 * 5

    def test_passive_normal_not_adapted_to_spread(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        for _ in range(20):
            p.update(_tick(last=100, bid=90, ask=110))  # spread=4 ticks
        # passive still uses bid1 regardless of spread
        assert p.price("buy", "passive") == 90
        # normal still uses ask1 without extra
        assert p.price("buy", "normal") == 110


class TestPriceWithUrgencyScore:
    @pytest.fixture
    def pricer_with_book(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        p.update(_tick(last=100, bid=99.5, ask=100.5))
        return p

    def test_urgency_zero_pegs_bid1_for_buy(self, pricer_with_book):
        # urgency=0 → 0 ticks → peg bid1
        assert pricer_with_book.price_with_urgency_score("buy", 0.0) == 99.5

    def test_urgency_zero_pegs_ask1_for_sell(self, pricer_with_book):
        assert pricer_with_book.price_with_urgency_score("sell", 0.0) == 100.5

    def test_urgency_one_crosses_max_ticks(self, pricer_with_book):
        # urgency=1.0 → 10 ticks → ask1 + 10*5 = 150.5
        assert pricer_with_book.price_with_urgency_score("buy", 1.0) == 100.5 + 10 * 5

    def test_urgency_middle_rounds(self, pricer_with_book):
        # urgency=0.5 → 5 ticks → ask1 + 25
        assert pricer_with_book.price_with_urgency_score("buy", 0.5) == 100.5 + 5 * 5

    def test_urgency_clamps_above_one(self, pricer_with_book):
        assert pricer_with_book.price_with_urgency_score("buy", 1.5) == 100.5 + 10 * 5

    def test_urgency_clamps_below_zero(self, pricer_with_book):
        assert pricer_with_book.price_with_urgency_score("buy", -0.5) == 99.5

    def test_custom_max_ticks(self, pricer_with_book):
        # urgency=0.5, max=4 → 2 ticks
        assert pricer_with_book.price_with_urgency_score("buy", 0.5, max_ticks=4) == 100.5 + 2 * 5

    def test_sell_subtracts_from_bid(self, pricer_with_book):
        # urgency=0.5 → 5 ticks → bid1 - 25
        assert pricer_with_book.price_with_urgency_score("sell", 0.5) == 99.5 - 5 * 5

    def test_no_book_fallback(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        p.update(_tick(last=25500))  # no bid/ask
        # urgency=0 → last
        assert p.price_with_urgency_score("buy", 0.0) == 25500
        # urgency=0.3 → 3 ticks → last + 15
        assert p.price_with_urgency_score("buy", 0.3) == 25500 + 3 * 5

    def test_invalid_direction_raises(self, pricer_with_book):
        with pytest.raises(ValueError):
            pricer_with_book.price_with_urgency_score("long", 0.5)


class TestFallback:
    def test_no_book_fallback_to_last_price_plus_ticks(self):
        p = AggressivePricer(tick_size=5.0, dump_first_tick=False)
        p.update(_tick(last=25500))  # no bid/ask
        # passive = last
        assert p.price("buy", "passive") == 25500
        # normal = last + 0
        assert p.price("buy", "normal") == 25500
        # cross = last + 2 * tick
        assert p.price("buy", "cross") == 25500 + 2 * 5
        # urgent = last + 5 * tick
        assert p.price("sell", "urgent") == 25500 - 5 * 5
