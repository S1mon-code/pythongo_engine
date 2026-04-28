# CTA Live Trading Operational Issues Research

## 1. Daily P&L Reset / Trading Day Detection (交易日切换)

### Industry Standard Solution

Chinese futures night sessions (21:00-23:00/01:00/02:30) belong to the **next trading day**. The authoritative trading day comes from the CTP API.

**CTP TradingDay vs ActionDay by exchange (night session):**

| Field | SHFE/INE | DCE/GFEX | CZCE |
|-------|----------|----------|------|
| TradingDay | Next trading day (correct) | Next trading day (correct) | **Current calendar day** (wrong!) |
| ActionDay | Current calendar day (correct) | **Next trading day** (wrong!) | Current calendar day (correct) |

Only SHFE/INE returns both fields correctly. SimNow uses SHFE rules for all exchanges.

### Concrete Implementation Pattern

```python
from datetime import datetime, time, timedelta

class TradingDayDetector:
    """Detect trading day transitions for Chinese futures."""

    def __init__(self):
        self._current_trading_day: str = ""

    def get_trading_day(self) -> str:
        """
        Primary: from CTP TraderApi.GetTradingDay() after login.
        Fallback: derive from local clock.
        """
        if self._current_trading_day:
            return self._current_trading_day
        return self._derive_from_clock()

    def init_from_ctp(self, ctp_trading_day: str):
        """Called after CTP TraderApi login succeeds.
        CTP login response contains accurate TradingDay."""
        self._current_trading_day = ctp_trading_day

    def _derive_from_clock(self) -> str:
        """Fallback: current time + 4 hours gives correct trading day.
        Night session starts 21:00, so 21:00+4h = next day 01:00.
        Day session 09:00+4h = 13:00 same day. Both correct.
        Special handling for Fri night -> Monday."""
        now = datetime.now()
        shifted = now + timedelta(hours=4)
        # Skip weekends
        weekday = shifted.weekday()
        if weekday == 5:  # Saturday
            shifted += timedelta(days=2)
        elif weekday == 6:  # Sunday
            shifted += timedelta(days=1)
        return shifted.strftime("%Y%m%d")

    def is_new_trading_day(self, new_trading_day: str) -> bool:
        """Check if trading day has changed (triggers daily P&L reset)."""
        if new_trading_day != self._current_trading_day:
            self._current_trading_day = new_trading_day
            return True
        return False

    def detect_day_boundary(self, tick_time: time) -> bool:
        """Detect day session open as the P&L reset point.
        Reset at 08:55 (before 09:00 open) rather than midnight."""
        return time(8, 55) <= tick_time <= time(9, 1)
```

### China-Specific Considerations
- The **day session open (09:00)** is the natural P&L reset point, NOT midnight
- Night session P&L accumulates into the next trading day
- Holiday schedules (no night session before holidays) must be handled
- CTP TraderApi `GetTradingDay()` is the single source of truth after login


## 2. Night Session K-line Handling (夜盘日线合成)

### Industry Standard Solution

A "daily bar" for Chinese futures spans from **night session open (21:00 day T-1) to day session close (15:00 day T)**. This is the universal convention used by all major platforms (vnpy, TqSdk, CTP data vendors).

**Bar completion timing:**
- Daily bar completes at **15:00** (day session close)
- Night session data from the previous evening is part of the NEXT trading day's daily bar
- The daily bar OHLCV includes: night session (21:00-23:00/01:00/02:30) + morning (09:00-11:30) + afternoon (13:30-15:00)

### Concrete Implementation Pattern

```python
from dataclasses import dataclass, field
from datetime import datetime, time

@dataclass
class DailyBarSynthesizer:
    """Synthesize daily K-line from tick/minute data, handling night sessions."""

    symbol: str
    trading_day: str = ""
    open: float = 0.0
    high: float = float('-inf')
    low: float = float('inf')
    close: float = 0.0
    volume: int = 0
    turnover: float = 0.0
    _bar_started: bool = False

    # Night session end times by exchange category
    NIGHT_END = {
        "shfe_metals": time(1, 0),    # au, ag, cu, al, etc.
        "shfe_rubber": time(23, 0),    # ru, etc.
        "dce_oils": time(23, 0),       # p, y, m, etc.
        "czce_sugar": time(23, 30),    # SR, etc.
        "ine_crude": time(2, 30),      # sc
    }

    def on_tick(self, price: float, volume: int, tick_time: datetime,
                trading_day: str):
        """Process each tick to build daily bar."""
        # New trading day detected -> finalize previous bar, start new one
        if trading_day != self.trading_day and self._bar_started:
            completed_bar = self._finalize_bar()
            self._reset(trading_day)
            self._update(price, volume)
            return completed_bar

        if not self._bar_started:
            self._reset(trading_day)

        self._update(price, volume)
        return None

    def on_day_close(self) -> dict:
        """Called at 15:00 day session close. Finalizes daily bar."""
        return self._finalize_bar()

    def _update(self, price: float, volume: int):
        if not self._bar_started:
            self.open = price
            self._bar_started = True
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume

    def _finalize_bar(self) -> dict:
        if not self._bar_started:
            return None
        return {
            "trading_day": self.trading_day,
            "symbol": self.symbol,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }

    def _reset(self, trading_day: str):
        self.trading_day = trading_day
        self.open = self.high = self.low = self.close = 0.0
        self.high = float('-inf')
        self.low = float('inf')
        self.volume = 0
        self._bar_started = False
```

### China-Specific Considerations
- Instruments WITHOUT night session: daily bar = 09:00-15:00 only
- DCE ActionDay is wrong during night session (shows next trading day), use SHFE contract's ActionDay or local clock for actual date
- Around midnight (00:00:00), vnpy had a known bug where `current_date` (updated once per second) could lag, causing wrong date assignment


## 3. State Persistence (状态持久化)

### Industry Standard Solution

Production trading systems use **Snapshot + WAL (Write-Ahead Log)** pattern, identical to database recovery:

1. **Periodic Snapshots**: Full state dump every N minutes or at session boundaries
2. **WAL (Event Journal)**: Every state-changing event logged before execution
3. **Recovery**: Load latest snapshot, then replay WAL entries from snapshot point

### Concrete Implementation Pattern

```python
import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

@dataclass
class StrategyState:
    """Critical state that must survive crashes."""
    equity_high_water_mark: float = 0.0
    daily_start_equity: float = 0.0
    current_position: int = 0
    last_signal: float = 0.0
    trading_day: str = ""
    last_update_time: str = ""
    # Add any strategy-specific state here

class StatePersistence:
    """Crash-safe state persistence using atomic writes."""

    def __init__(self, strategy_name: str, data_dir: str = "./state"):
        self.strategy_name = strategy_name
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self.data_dir / f"{strategy_name}_state.json"
        self._backup_file = self.data_dir / f"{strategy_name}_state.bak"
        self._wal_file = self.data_dir / f"{strategy_name}_wal.jsonl"

    def save_state(self, state: StrategyState) -> None:
        """Atomic save: write to temp, then rename (atomic on most OS)."""
        state.last_update_time = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp_file = self._state_file.with_suffix('.tmp')

        data = json.dumps(asdict(state), ensure_ascii=False, indent=2)

        # Write to temp file
        with open(tmp_file, 'w') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())  # Force to disk

        # Backup current state
        if self._state_file.exists():
            self._state_file.replace(self._backup_file)

        # Atomic rename
        tmp_file.rename(self._state_file)

    def load_state(self) -> Optional[StrategyState]:
        """Load state, falling back to backup if primary is corrupted."""
        for path in [self._state_file, self._backup_file]:
            if path.exists():
                try:
                    with open(path, 'r') as f:
                        data = json.load(f)
                    return StrategyState(**data)
                except (json.JSONDecodeError, KeyError):
                    continue
        return None

    def append_wal(self, event: dict) -> None:
        """Append event to write-ahead log (for audit trail)."""
        event["_ts"] = time.time()
        with open(self._wal_file, 'a') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
            f.flush()

    def save_at_boundaries(self, state: StrategyState, tick_time: str):
        """Save state at natural session boundaries."""
        # Save at: night open, morning open, afternoon close
        boundaries = ["21:00", "09:00", "15:00"]
        current_minute = tick_time[:5]
        if current_minute in boundaries:
            self.save_state(state)
```

### Key Principles
- **Atomic writes**: Always write-to-temp-then-rename, never overwrite in place
- **fsync**: Force data to disk before considering write complete
- **Dual files**: Primary + backup prevents corruption from mid-write crashes
- **Session boundary saves**: Natural save points at 09:00, 15:00, 21:00
- **On every trade**: Save state after every fill confirmation
- **VeighNa/vnpy approach**: Serializes stop orders and condition orders to JSON files, reloads on strategy restart (see vnpy `CtaEngine.save_stop_orders()`)


## 4. Contract Rollover (换月)

### Industry Standard Solution

Three standard rollover strategies, ranked by automation level:

**A. Volume/OI-based (most common for Chinese futures):**
- Monitor when the next contract's volume or open interest exceeds the current contract
- Trigger rollover on the day this crossover occurs
- Typically happens 5-15 trading days before expiry

**B. Calendar-based:**
- Pre-defined rollover dates per product (e.g., "roll 10 days before delivery month")
- Simpler but less adaptive

**C. Liquidity-based with spread control (professional):**
- Monitor bid-ask spread on both contracts
- Execute when spread on new contract is acceptable
- Use spread orders when available (exchange combo contracts)

### Concrete Implementation Pattern

```python
from dataclasses import dataclass
from datetime import datetime

@dataclass
class RolloverManager:
    """Manage contract rollover for Chinese futures."""

    product: str  # e.g., "rb", "i", "IF"
    current_contract: str = ""  # e.g., "rb2510"
    next_contract: str = ""     # e.g., "rb2601"
    warmup_bars: int = 60       # Indicator warmup period

    # Rollover state
    _roll_triggered: bool = False
    _warmup_complete: bool = False
    _bars_loaded: int = 0

    def check_rollover_signal(self, contracts_data: dict) -> bool:
        """Check if rollover should be triggered.
        contracts_data: {contract: {"volume": int, "open_interest": int}}
        """
        if not self.current_contract or not self.next_contract:
            return False

        curr = contracts_data.get(self.current_contract, {})
        next_ = contracts_data.get(self.next_contract, {})

        curr_oi = curr.get("open_interest", 0)
        next_oi = next_.get("open_interest", 0)

        # Classic criterion: next contract OI > current contract OI
        if next_oi > curr_oi and next_oi > 0:
            return True

        # Safety: force roll if within 5 days of delivery month
        if self._near_delivery(self.current_contract, days=5):
            return True

        return False

    def execute_rollover(self, engine) -> dict:
        """Execute the rollover: close old, open new, update strategy.
        Returns summary of actions taken."""
        actions = []

        # Step 1: Record current logical position
        old_pos = engine.get_position(self.current_contract)

        # Step 2: Close position on old contract
        if old_pos != 0:
            engine.close_position(self.current_contract)
            actions.append(f"Closed {old_pos} lots on {self.current_contract}")

        # Step 3: Open equivalent position on new contract
        if old_pos != 0:
            engine.open_position(self.next_contract, old_pos)
            actions.append(f"Opened {old_pos} lots on {self.next_contract}")

        # Step 4: Update strategy subscription
        old_contract = self.current_contract
        self.current_contract = self.next_contract
        self.next_contract = self._infer_next_contract(self.current_contract)

        # Step 5: Start indicator warmup
        self._warmup_complete = False
        self._bars_loaded = 0

        actions.append(f"Rolled {old_contract} -> {self.current_contract}")
        return {"actions": actions, "warmup_needed": self.warmup_bars}

    def load_warmup_data(self, engine) -> None:
        """Load historical data for new contract to warm up indicators."""
        bars = engine.query_history(
            symbol=self.current_contract,
            count=self.warmup_bars + 20  # Extra buffer
        )
        for bar in bars:
            engine.strategy.on_bar(bar)  # Feed to indicators
            self._bars_loaded += 1

        self._warmup_complete = self._bars_loaded >= self.warmup_bars

    def _near_delivery(self, contract: str, days: int) -> bool:
        """Check if contract is near delivery month."""
        # Extract YYMM from contract code, compare with current date
        # Implementation depends on exchange rules
        return False  # Placeholder

    def _infer_next_contract(self, current: str) -> str:
        """Infer next dominant contract code.
        Rules vary by product - some are monthly, some quarterly."""
        # Product-specific logic needed
        return ""  # Placeholder
```

### China-Specific Considerations
- **vnpy v2.3.0** introduced a built-in "Rollover Assistant" that handles position + strategy migration in one click
- Professional approach: use exchange standard spread/combo contracts (DCE, GFEX, CZCE support these) for atomic rollover
- **Indicator warmup** is critical - load `warmup_bars` of history on the new contract before generating signals
- Shinnytech (TqSdk) offers TargetPosTask which handles rollover execution automatically
- Average slippage for manual rollover is 15-30bp higher than automated execution


## 5. Feishu/Notification Non-blocking

### Industry Standard Solution

Use **fire-and-forget with a background thread** or **async queue**. Never block the main trading loop for notifications.

### Concrete Implementation Pattern

```python
import threading
import queue
import requests
import time
from typing import Optional

class NonBlockingNotifier:
    """Fire-and-forget notification sender. Never blocks trading thread."""

    def __init__(self, webhook_url: str, max_queue_size: int = 1000):
        self._webhook_url = webhook_url
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def send(self, title: str, content: str, level: str = "info") -> None:
        """Non-blocking send. Drops message if queue is full."""
        msg = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "red" if level == "error" else "blue"
                },
                "elements": [{
                    "tag": "markdown",
                    "content": content
                }]
            }
        }
        try:
            self._queue.put_nowait(msg)
        except queue.Full:
            pass  # Drop message rather than block trading

    def _worker(self):
        """Background worker that sends queued messages."""
        while True:
            try:
                msg = self._queue.get(timeout=1.0)
                self._safe_send(msg)
                # Rate limit: max 5 messages per second for Feishu
                time.sleep(0.2)
            except queue.Empty:
                continue

    def _safe_send(self, msg: dict, retries: int = 2) -> None:
        """Send with retry, never raises."""
        for attempt in range(retries):
            try:
                resp = requests.post(
                    self._webhook_url,
                    json=msg,
                    timeout=5
                )
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)

# Alternative: simplest possible fire-and-forget using threading
def fire_and_forget_notify(url: str, payload: dict) -> None:
    """One-liner non-blocking HTTP POST."""
    threading.Thread(
        target=lambda: requests.post(url, json=payload, timeout=5),
        daemon=True
    ).start()
```

### Key Principles
- **Daemon thread**: Automatically killed when main process exits
- **Bounded queue**: Prevents memory leak if notifications pile up
- **Drop rather than block**: Trading logic takes absolute priority
- **Rate limiting**: Feishu webhook has rate limits (~5/s per bot)
- **Timeout on HTTP**: Always set timeout (5s) to prevent hanging


## 6. Order Management (订单管理)

### Industry Standard Solution

CTP order flow follows a strict state machine:

```
ReqOrderInsert -> [CTP Broker validation]
  -> FAIL: OnRspOrderInsert (broker rejected) + OnErrRtnOrderInsert
  -> PASS: OnRtnOrder(status=Unknown) -> [Exchange validation]
     -> FAIL: OnRtnOrder(status=Canceled, "InsertRejected")
     -> PASS: OnRtnOrder(status=NoTradeQueueing) -> [Matching]
        -> Partial fill: OnRtnOrder(PartTradedQueueing) + OnRtnTrade
        -> Full fill: OnRtnOrder(AllTraded) + OnRtnTrade
```

### Concrete Implementation Pattern

```python
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Callable

class OrderStatus(Enum):
    PENDING = "pending"         # Sent, waiting for broker ack
    QUEUED = "queued"           # Accepted by exchange, in order book
    PARTIAL = "partial_fill"    # Partially filled
    FILLED = "filled"           # Fully filled
    CANCELLED = "cancelled"     # Cancelled
    REJECTED = "rejected"       # Rejected by broker or exchange
    LIMIT_MOVE = "limit_move"   # Hit limit up/down

@dataclass
class ManagedOrder:
    order_id: str
    symbol: str
    direction: str  # "buy" or "sell"
    price: float
    volume: int
    filled: int = 0
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    timeout_seconds: int = 30
    retry_count: int = 0
    max_retries: int = 3

class OrderManager:
    """Production order management with retry, timeout, limit-move handling."""

    def __init__(self, engine, notifier=None):
        self._engine = engine
        self._notifier = notifier
        self._orders: Dict[str, ManagedOrder] = {}

    def send_order(self, symbol: str, direction: str, price: float,
                   volume: int) -> str:
        """Send order with tracking."""
        order_id = self._engine.send_order(symbol, direction, price, volume)
        self._orders[order_id] = ManagedOrder(
            order_id=order_id, symbol=symbol,
            direction=direction, price=price, volume=volume
        )
        return order_id

    def on_order_update(self, order_id: str, status: str,
                        filled: int = 0, msg: str = ""):
        """Handle CTP OnRtnOrder callback."""
        order = self._orders.get(order_id)
        if not order:
            return

        order.filled = filled

        if "AllTraded" in status:
            order.status = OrderStatus.FILLED

        elif "PartTradedQueueing" in status:
            order.status = OrderStatus.PARTIAL

        elif "Canceled" in status:
            if order.filled < order.volume:
                # Partial fill + cancel -> handle remaining
                self._handle_partial_cancel(order)

        elif "InsertRejected" in status or "Rejected" in status:
            order.status = OrderStatus.REJECTED
            if "涨跌停" in msg or "price" in msg.lower():
                self._handle_limit_move(order)
            else:
                self._handle_rejection(order, msg)

    def check_timeouts(self):
        """Called periodically to handle stale orders."""
        now = datetime.now()
        for order in list(self._orders.values()):
            if order.status in (OrderStatus.PENDING, OrderStatus.QUEUED):
                elapsed = (now - order.created_at).total_seconds()
                if elapsed > order.timeout_seconds:
                    self._handle_timeout(order)

    def _handle_partial_cancel(self, order: ManagedOrder):
        """Handle partially filled then cancelled order."""
        remaining = order.volume - order.filled
        if remaining > 0 and order.retry_count < order.max_retries:
            # Re-send for remaining volume at market price
            order.retry_count += 1
            new_price = self._engine.get_market_price(
                order.symbol, order.direction
            )
            self.send_order(
                order.symbol, order.direction, new_price, remaining
            )

    def _handle_limit_move(self, order: ManagedOrder):
        """Handle limit-up/down rejection."""
        # Option A: Queue at limit price
        limit_price = self._engine.get_limit_price(
            order.symbol, order.direction
        )
        if limit_price:
            self.send_order(
                order.symbol, order.direction,
                limit_price, order.volume
            )
        # Option B: Notify and wait for next bar
        if self._notifier:
            self._notifier.send(
                "Limit Move Alert",
                f"{order.symbol} hit limit, order {order.order_id} rejected"
            )

    def _handle_rejection(self, order: ManagedOrder, msg: str):
        """Handle generic rejection."""
        order.status = OrderStatus.REJECTED
        if self._notifier:
            self._notifier.send("Order Rejected", f"{order.symbol}: {msg}")

    def _handle_timeout(self, order: ManagedOrder):
        """Cancel and retry timed-out orders."""
        self._engine.cancel_order(order.order_id)
        # Will be re-sent in on_order_update when cancel confirmed
```

### China-Specific Considerations
- **CTP flow control**: Max orders per second is configurable by broker (commonly 3-5/s). Error: "CTP:下单频率限制"
- **SHFE/INE require explicit close-today vs close-yesterday** offset
- **Limit up/down**: Order at limit price may queue but not fill; consider waiting for next bar
- **OnRtnTrade volume** is per-fill volume, NOT cumulative. Track cumulative yourself
- CTP撤单 using ExchangeID + OrderSysID is the recommended method (future-proof for CTP 7.0)


## 7. Dynamic Margin Rate

### Industry Standard Solution

Margin rates are queried via CTP API and change infrequently (typically around holidays or high volatility). The system should:

1. Query margin rates at startup
2. Re-query periodically (daily)
3. React to exchange notifications

### Concrete Implementation Pattern

```python
from dataclasses import dataclass
from typing import Dict

@dataclass
class MarginInfo:
    symbol: str
    long_margin_ratio: float   # By money (e.g., 0.10 = 10%)
    short_margin_ratio: float
    long_margin_per_lot: float  # By volume (fixed per lot)
    short_margin_per_lot: float

class MarginManager:
    """Manage dynamic margin rates from CTP."""

    def __init__(self):
        self._margins: Dict[str, MarginInfo] = {}

    def query_margin_rate(self, engine, symbol: str) -> None:
        """Query via CTP ReqQryInstrumentMarginRate.
        Response in OnRspQryInstrumentMarginRate.
        Note: empty InstrumentID returns rates for all positions."""
        engine.ctp_trader.ReqQryInstrumentMarginRate(symbol)

    def on_margin_rate_response(self, data: dict):
        """Handle CTP OnRspQryInstrumentMarginRate callback."""
        symbol = data["InstrumentID"]
        self._margins[symbol] = MarginInfo(
            symbol=symbol,
            long_margin_ratio=data["LongMarginRatioByMoney"],
            short_margin_ratio=data["ShortMarginRatioByMoney"],
            long_margin_per_lot=data["LongMarginRatioByVolume"],
            short_margin_per_lot=data["ShortMarginRatioByVolume"],
        )

    def calc_margin_required(self, symbol: str, price: float,
                             volume: int, multiplier: int,
                             direction: str = "long") -> float:
        """Calculate margin required for a position."""
        info = self._margins.get(symbol)
        if not info:
            return float('inf')  # Unknown margin -> refuse to trade

        if direction == "long":
            ratio = info.long_margin_ratio
            per_lot = info.long_margin_per_lot
        else:
            ratio = info.short_margin_ratio
            per_lot = info.short_margin_per_lot

        # CTP uses whichever is non-zero
        if ratio > 0:
            return price * multiplier * volume * ratio
        else:
            return per_lot * volume

    def get_max_position(self, symbol: str, available_capital: float,
                         price: float, multiplier: int,
                         direction: str = "long",
                         usage_ratio: float = 0.8) -> int:
        """Calculate max position given available capital."""
        margin_per_lot = self.calc_margin_required(
            symbol, price, 1, multiplier, direction
        )
        if margin_per_lot <= 0:
            return 0
        return int(available_capital * usage_ratio / margin_per_lot)

    def refresh_all(self, engine, symbols: list):
        """Refresh all margin rates. Called daily at startup.
        CTP has 1s query interval limit between requests."""
        for symbol in symbols:
            self.query_margin_rate(engine, symbol)
            # Must wait ~1s between CTP queries
```

### China-Specific Considerations
- CTP returns **account-level** margin rates (broker's rate), not exchange minimums
- Exchange minimum rates available via `OnRspQryInstrument` fields
- Rates returned for empty InstrumentID = rates for currently held positions only
- Margin rates often change before holidays (exchanges increase rates)
- If response InvestorID is "000000", the rate is the broker default, not account-specific
- Query flow control: ~1 request per second for CTP queries


## 8. Minimum Position Change Threshold

### Industry Standard Solution

Robert Carver's **position buffering** (from "Systematic Trading" and pysystemtrade) is the industry standard:

**Core idea**: Only trade when the optimal position moves outside a buffer zone around the current position. This prevents churning from small signal changes.

**Buffer formula**:
```
buffer_width = round(optimal_position * buffer_fraction)
upper_buffer = current_position + buffer_width
lower_buffer = current_position - buffer_width
```

Common `buffer_fraction` values: **0.10 (10%)** for typical CTA, up to 0.20 for high-cost instruments.

**Rule**: Only trade if `new_optimal_position` falls outside `[lower_buffer, upper_buffer]`.

### Concrete Implementation Pattern

```python
import math

class PositionBuffer:
    """Carver-style position buffering to avoid excessive trading.

    Reference: Robert Carver, "Systematic Trading", Chapter 11.
    Also: pysystemtrade/systems/accounts/order_simulator/
    """

    def __init__(self, buffer_fraction: float = 0.10,
                 min_trade_size: int = 1):
        self.buffer_fraction = buffer_fraction
        self.min_trade_size = min_trade_size

    def calculate_buffered_position(
        self,
        optimal_position: float,
        current_position: int
    ) -> int:
        """Apply buffer and return target position (integer lots).

        Only changes position if optimal moves outside the buffer zone
        around current position.

        Args:
            optimal_position: Continuous (fractional) optimal position from model
            current_position: Current integer position held

        Returns:
            Target integer position to hold
        """
        # Buffer width scales with position size
        buffer_width = max(
            abs(optimal_position) * self.buffer_fraction,
            0.5  # Minimum buffer of 0.5 lots
        )

        upper = current_position + buffer_width
        lower = current_position - buffer_width

        # If optimal is within buffer, don't trade
        if lower <= optimal_position <= upper:
            return current_position

        # Optimal is outside buffer -> move to nearest buffer edge
        if optimal_position > upper:
            # Round down (conservative: don't overshoot)
            target = math.floor(optimal_position - buffer_width)
        else:
            # Round up (conservative: don't overshoot)
            target = math.ceil(optimal_position + buffer_width)

        # Check minimum trade size
        trade_size = abs(target - current_position)
        if trade_size < self.min_trade_size:
            return current_position

        return target

    def should_trade(self, optimal: float, current: int) -> tuple:
        """Returns (should_trade: bool, target_position: int, trade_size: int)."""
        target = self.calculate_buffered_position(optimal, current)
        trade = target - current
        return (trade != 0, target, trade)


# Example usage in a CTA strategy:
#
#   buffer = PositionBuffer(buffer_fraction=0.10, min_trade_size=1)
#
#   # Each bar:
#   optimal = model.get_signal() * capital / (volatility * multiplier * price)
#   should, target, trade = buffer.should_trade(optimal, current_pos)
#   if should:
#       engine.set_target_position(symbol, target)
```

### Professional CTA Thresholds
- **Robert Carver**: 10% buffer is standard, derived from cost-benefit analysis of turnover vs tracking error
- **Common heuristic**: Don't trade if change < 1 lot
- **Cost-aware**: For expensive instruments (e.g., crude oil), use 15-20% buffer
- **Rob Carver's blog data** shows fast rules (e.g., EWMAC4) have 2-3x turnover of slow rules, but buffering reduces actual trades by ~50% with minimal performance impact


## 9. Strategy Restart with Existing Position

### Industry Standard Solution

On restart, the system must **reconcile** its internal state with the broker's actual positions:

1. **Query actual positions** from CTP
2. **Load persisted state** (from Issue #3 above)
3. **Reconcile**: Compare and resolve discrepancies
4. **Resume**: Continue from reconciled state

### Concrete Implementation Pattern

```python
from dataclasses import dataclass
from typing import Dict, Optional

@dataclass
class PositionRecord:
    symbol: str
    long_volume: int = 0
    short_volume: int = 0
    long_today: int = 0   # SHFE/INE need today vs yesterday split
    short_today: int = 0

    @property
    def net(self) -> int:
        return self.long_volume - self.short_volume

class RestartReconciler:
    """Handle strategy restart with existing positions."""

    def __init__(self, strategy_name: str, persistence, notifier=None):
        self.strategy_name = strategy_name
        self.persistence = persistence
        self.notifier = notifier

    def reconcile_on_startup(self, engine) -> dict:
        """Full reconciliation flow on strategy restart.

        Returns reconciliation report.
        """
        report = {"status": "ok", "actions": []}

        # Step 1: Query actual positions from CTP
        actual_positions = self._query_ctp_positions(engine)

        # Step 2: Load persisted strategy state
        saved_state = self.persistence.load_state()

        if saved_state is None:
            # No saved state -> adopt actual positions as truth
            report["actions"].append("No saved state found, adopting CTP positions")
            return self._adopt_positions(actual_positions, report)

        # Step 3: Compare
        strategy_pos = saved_state.current_position
        actual_net = sum(p.net for p in actual_positions.values())

        if strategy_pos == actual_net:
            report["actions"].append(
                f"Positions match: strategy={strategy_pos}, CTP={actual_net}"
            )
            return report

        # Step 4: Discrepancy detected
        report["status"] = "warning"
        report["discrepancy"] = {
            "strategy_position": strategy_pos,
            "ctp_position": actual_net,
            "difference": actual_net - strategy_pos
        }

        # Resolution strategy: TRUST CTP (actual broker positions)
        # Update strategy state to match reality
        saved_state.current_position = actual_net
        self.persistence.save_state(saved_state)

        msg = (f"Position discrepancy detected!\n"
               f"Strategy thinks: {strategy_pos}\n"
               f"CTP actual: {actual_net}\n"
               f"Adopted CTP position as truth.")
        report["actions"].append(msg)

        if self.notifier:
            self.notifier.send("Position Reconciliation Warning", msg, "error")

        return report

    def _query_ctp_positions(self, engine) -> Dict[str, PositionRecord]:
        """Query all positions from CTP.
        Uses ReqQryInvestorPosition, waits for all responses."""
        raw_positions = engine.query_positions()  # Blocking query
        result = {}
        for pos in raw_positions:
            symbol = pos["InstrumentID"]
            if symbol not in result:
                result[symbol] = PositionRecord(symbol=symbol)

            rec = result[symbol]
            if pos["PosiDirection"] == "Long":  # '2'
                rec.long_volume = pos["Position"]
                rec.long_today = pos.get("TodayPosition", 0)
            elif pos["PosiDirection"] == "Short":  # '3'
                rec.short_volume = pos["Position"]
                rec.short_today = pos.get("TodayPosition", 0)

        return result

    def _adopt_positions(self, positions: Dict[str, PositionRecord],
                         report: dict) -> dict:
        """When no saved state exists, create state from CTP positions."""
        for symbol, pos in positions.items():
            report["actions"].append(
                f"Adopted {symbol}: long={pos.long_volume}, "
                f"short={pos.short_volume}, net={pos.net}"
            )
        return report

    def safe_restart_flow(self, engine) -> None:
        """Complete restart flow:
        1. Connect CTP
        2. Wait for login + settlement confirm
        3. Query positions
        4. Reconcile
        5. Load historical data for indicator warmup
        6. Subscribe market data
        7. Resume trading
        """
        # 1-2: Engine handles CTP connection
        engine.wait_for_login()

        # 3-4: Reconcile
        report = self.reconcile_on_startup(engine)

        # 5: Indicator warmup
        engine.load_history(count=engine.strategy.warmup_bars)

        # 6: Subscribe
        engine.subscribe(engine.strategy.symbol)

        # 7: Resume
        engine.strategy.trading = True

        if self.notifier:
            self.notifier.send(
                "Strategy Restarted",
                f"{self.strategy_name} restarted.\n"
                f"Reconciliation: {report['status']}\n"
                f"Actions: {report['actions']}"
            )
```

### China-Specific Considerations
- **CTP reconnection**: CTP API has built-in auto-reconnect for network drops. After reconnect, must re-subscribe market data and re-authenticate
- **vnpy approach**: CTA strategies store `pos` (logical position) in JSON. On restart, `pos` is restored from file but actual CTP position may differ if manual trades were made
- **Settlement confirmation** is required before any trading after login (`ReqSettlementInfoConfirm`)
- **SHFE/INE position split**: Must track today-position vs yesterday-position separately for correct close order offset
- **Key principle**: Always trust CTP actual positions over saved state. The saved state is for strategy logic (indicators, signals), not for position truth
- CTP断线重连 is handled at API level - OnFrontDisconnected triggers, then re-login + re-subscribe on reconnect

---

## Summary: Priority Implementation Order

| Priority | Issue | Complexity | Impact |
|----------|-------|-----------|--------|
| P0 | #9 Restart Recovery | Medium | Critical - prevents position mismatch |
| P0 | #3 State Persistence | Low | Critical - survives crashes |
| P0 | #6 Order Management | High | Critical - handles real money |
| P1 | #1 Trading Day Detection | Low | Required for daily P&L |
| P1 | #5 Non-blocking Notifications | Low | Prevents trading delays |
| P1 | #8 Position Buffer | Low | Saves transaction costs |
| P2 | #2 Daily Bar Synthesis | Medium | Required for D1 strategies |
| P2 | #4 Contract Rollover | High | Required for multi-month holding |
| P2 | #7 Dynamic Margin | Low | Prevents over-leveraging |
