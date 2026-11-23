"""Long-only EMA trend bot for the BTC race (agent-fun/bars-replay/v1).

Idea, in one paragraph: on one-minute cbBTC bars the price spends most of
the day chopping sideways and makes its money in one or two sustained
multi-hour runs, while every round trip costs roughly 40 bps in fees,
slippage and pool impact. So this bot trades rarely and only with the
trend: it watches a fast EMA (12 bars) against a slow EMA (26 bars) of the
closes, buys with all its cash the first time the fast EMA crosses above
the slow one, and then simply holds unless the trade goes wrong. It exits
only on a hard stop (3% below the fill price) or a trailing stop (2% below
the highest close since entry). There is no exit on a cross back down,
because on 1-minute bars the cross whipsaws and each whipsaw pays the
round-trip cost for nothing. At most one entry is ever taken in a window:
a second entry after a stopped-out first one is, on this data, how a flat
or falling day turns one small loss into several. No entry is taken in the
last 60 bars, where there is no time left for a trend to pay the costs.

Everything is deterministic: integer prices from the bars, integer EMA
state updated once per bar, no clock, no network, no randomness. The bot
answers every batch with exactly one reply through the vendored shim.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# EMA lengths, in bars (1-minute closes).
FAST_LEN = 12
SLOW_LEN = 26
# Bars of history needed before the EMAs are trusted for an entry.
MIN_BARS = 30
# Last bar index (seq) at which a new entry may still be taken.
LAST_ENTRY_SEQ = 1380
# Stop levels, in basis points: hard stop under the fill, trailing stop
# under the highest close since entry.
STOP_BPS = 300
TRAIL_BPS = 200
# Max slippage vs the latest close an order tolerates. A fill lands about
# 2 bps of modelled slippage plus ~10-20 bps of pool impact above the
# open, and the open itself sits at most ~72 bps from the last close on
# this window (the largest one-bar move in the dev data), so 120 bps
# accepts every ordinary fill and rejects only a wild gap.
MAX_SLIPPAGE_BPS = 120
# At most one entry per window (see module docstring).
MAX_ENTRIES = 1


class TrendBot:
    """Keeps the EMA state and the one open trade's reference prices."""

    def __init__(self):
        self.bars_seen = 0
        self.ema_fast = None
        self.ema_slow = None
        self.prev_fast = None
        self.prev_slow = None
        # Fill price of the open trade (the open of the bar its buy filled
        # on), and the highest close since entry; None while flat.
        self.entry_px = None
        self.peak_px = None
        self.entries_used = 0
        self.buy_pending = False

    def _update_emas(self, close):
        """Fold one close into both EMAs, remembering the previous pair."""
        self.prev_fast = self.ema_fast
        self.prev_slow = self.ema_slow
        if self.ema_fast is None:
            self.ema_fast = close
            self.ema_slow = close
            return
        # EMA factor 2 / (n + 1), in integer math scaled by 1_000_000 so
        # the state stays exact and identical across runs.
        kf = 2_000_000 // (FAST_LEN + 1)
        ks = 2_000_000 // (SLOW_LEN + 1)
        self.ema_fast = (close * kf + self.ema_fast * (1_000_000 - kf)) // 1_000_000
        self.ema_slow = (close * ks + self.ema_slow * (1_000_000 - ks)) // 1_000_000

    def _crossed_up(self):
        """True when the fast EMA crossed from at/below to above the slow."""
        if self.prev_fast is None or self.prev_slow is None:
            return False
        return self.prev_fast <= self.prev_slow and self.ema_fast > self.ema_slow

    def on_batch(self, state, batch):
        # The batch carries one bar per symbol; this universe has one.
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b
                break
        if bar is None:
            return []
        close = int(bar["c"])
        open_px = int(bar["o"])
        self.bars_seen += 1
        self._update_emas(close)

        # Learn what the previous reply's orders did: a filled buy sets
        # the entry reference to the price it filled at (this bar's open);
        # a filled sell clears the trade. A rejected buy frees the entry
        # slot again instead of burning it.
        for fill in batch["fills"]:
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("status") != "filled":
                if fill.get("side") == "buy":
                    self.buy_pending = False
                continue
            if fill.get("side") == "buy":
                self.buy_pending = False
                self.entries_used += 1
                self.entry_px = open_px
                self.peak_px = open_px
            elif fill.get("side") == "sell":
                self.entry_px = None
                self.peak_px = None

        if state.warming_up:
            return []

        holding = state.positions.get(SYMBOL)
        if holding is not None and holding.qty > 0:
            # Track the peak close and exit only on the stops.
            if self.peak_px is None:
                self.peak_px = close
            elif close > self.peak_px:
                self.peak_px = close
            entry_px = self.entry_px if self.entry_px is not None else close
            stop_px = entry_px * (10_000 - STOP_BPS) // 10_000
            trail_px = self.peak_px * (10_000 - TRAIL_BPS) // 10_000
            if close <= stop_px or close <= trail_px:
                return [{
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": holding.qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            return []

        # Flat: the single entry, on a fresh fast-over-slow cross, early
        # enough in the window for the trend to pay its costs.
        if (
            self.entries_used < MAX_ENTRIES
            and not self.buy_pending
            and self.bars_seen >= MIN_BARS
            and state.seq <= LAST_ENTRY_SEQ
            and self._crossed_up()
            and state.cash > 1_000_000
        ):
            self.buy_pending = True
            return [{
                "side": "buy",
                "symbol": SYMBOL,
                "notional_quote_units": state.cash,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []


_BOT = TrendBot()


def on_batch(state, batch):
    return _BOT.on_batch(state, batch)


makefun_bars.run(on_batch)
