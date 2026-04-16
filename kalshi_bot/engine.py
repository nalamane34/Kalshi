"""Main trading engine — orchestrates all components."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta

import structlog

from kalshi_bot.client.auth import KalshiAuth
from kalshi_bot.client.rest import KalshiRestClient, KalshiNotFoundError
from kalshi_bot.client.websocket import KalshiWSClient
from kalshi_bot.config import BotConfig
from kalshi_bot.models.market import MarketSnapshot
from kalshi_bot.models.order import (
    Fill, Order, OrderAction, OrderStatus, Side,
)
from kalshi_bot.portfolio import PortfolioTracker
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.storage.database import Database
from kalshi_bot.strategies import STRATEGY_REGISTRY
from kalshi_bot.strategies.base import BaseStrategy, StrategySignal
from kalshi_bot.strategies.market_maker import MarketMakerStrategy


class TradingEngine:
    """Core orchestrator that ties all components together."""

    def __init__(self, config: BotConfig, dry_run: bool = False):
        self._config = config
        self._dry_run = dry_run
        self._log = structlog.get_logger("kalshi.engine")

        # Components (initialized in start())
        self._auth: KalshiAuth | None = None
        self._rest: KalshiRestClient | None = None
        self._ws: KalshiWSClient | None = None
        self._db: Database | None = None
        self._risk: RiskManager | None = None
        self._portfolio: PortfolioTracker | None = None
        self._strategies: list[BaseStrategy] = []

        # State
        self._running = False
        self._simulated = False
        self._market_snapshots: dict[str, MarketSnapshot] = {}
        self._active_tickers: list[str] = []
        self._tasks: list[asyncio.Task] = []

        # Paper trading state
        self._paper_orders: list[dict] = []  # pending paper orders
        self._paper_positions: dict[str, int] = {}  # ticker -> net contracts
        self._paper_pnl: int = 0  # realized P&L in cents
        self._paper_fills: int = 0  # total fill count
        self._paper_order_counter: int = 0

    async def start(self) -> None:
        """Full startup sequence."""
        self._log.info(
            "engine_starting",
            environment=self._config.environment,
            dry_run=self._dry_run,
        )

        # 1. Database
        self._db = Database(self._config.database.path)
        await self._db.initialize()

        # 2. Auth (may fail if keys are for different env)
        try:
            self._auth = KalshiAuth(
                key_id=self._config.api_key_id,
                private_key_path=self._config.private_key_path,
            )
        except Exception as e:
            self._log.warning("auth_setup_failed", error=str(e))
            self._auth = None

        # 3. REST client — uses production for public market data
        # For trading we use the configured base_url (demo or production)
        self._rest = KalshiRestClient(
            base_url=self._config.api.production_base_url,
            auth=self._auth,
            read_rate_limit=self._config.api.read_rate_limit,
            write_rate_limit=self._config.api.write_rate_limit,
            request_timeout=self._config.api.request_timeout,
            max_retries=self._config.api.max_retries,
            retry_backoff_base=self._config.api.retry_backoff_base,
        )
        await self._rest.start()

        # 4. Try to verify connectivity with authenticated balance call
        self._simulated = False
        try:
            balance = await self._rest.get_balance()
            self._log.info(
                "api_connected",
                balance=balance.available_dollars,
                portfolio_value=balance.portfolio_dollars,
            )
        except Exception as e:
            self._log.warning("balance_fetch_failed", error=str(e), msg="Running with simulated balance")
            from kalshi_bot.models.portfolio import Balance as BalanceModel
            balance = BalanceModel(available_balance=100_000, portfolio_value=100_000)
            self._simulated = True

        # 5. Risk manager
        self._risk = RiskManager(self._config.risk)
        try:
            positions = await self._rest.get_positions()
            pos_map = {p.ticker: p.position for p in positions}
        except Exception:
            pos_map = {}
        self._risk.initialize(balance.available_balance, pos_map)

        # 6. Portfolio tracker
        self._portfolio = PortfolioTracker(self._rest, self._risk, self._db)
        if self._simulated:
            # Set simulated balance directly so dashboard reflects it
            self._portfolio._balance = balance
            self._log.info("portfolio_simulated", balance=balance.available_dollars)
        else:
            try:
                await self._portfolio.sync()
            except Exception:
                self._log.warning("portfolio_sync_skipped", msg="Using simulated portfolio")
                self._portfolio._balance = balance

        # 7. Discover markets (uses public endpoints — no auth needed)
        self._active_tickers = await self._discover_markets()
        if not self._active_tickers:
            self._log.warning("no_markets_found", msg="No tradeable markets available")
            await self._rest.close()
            await self._db.close()
            return

        self._log.info("markets_selected", count=len(self._active_tickers), tickers=self._active_tickers[:10])

        # 8. Fetch initial snapshots
        await self._refresh_all_snapshots()

        # 9. WebSocket
        try:
            self._ws = KalshiWSClient(self._config.ws_url, self._auth)
            await self._ws.connect()
            self._ws.on("ticker", self._on_ws_ticker)
            self._ws.on("fill", self._on_ws_fill)
            self._ws.on("orderbook_delta", self._on_ws_orderbook)
            await self._ws.subscribe(
                ["ticker", "orderbook_delta"],
                self._active_tickers,
            )
        except Exception as e:
            self._log.warning("ws_connect_failed", error=str(e), msg="Falling back to REST polling")
            self._ws = None

        # 10. Initialize strategies
        await self._init_strategies()

        # 11. Record initial PnL snapshot
        bal = self._portfolio.balance
        await self._db.record_pnl_snapshot(
            balance=bal.available_balance,
            portfolio_value=bal.portfolio_value,
            realized_pnl=0,
            total_positions=0,
        )

        # 12. Start main loop + periodic tasks
        self._running = True
        self._tasks = [
            asyncio.create_task(self._main_loop(), name="main_loop"),
            asyncio.create_task(self._periodic_sync(), name="periodic_sync"),
            asyncio.create_task(self._periodic_snapshot_refresh(), name="snapshot_refresh"),
            asyncio.create_task(self._log_status_loop(), name="status_log"),
        ]

        self._log.info("engine_started", strategies=[s.name for s in self._strategies])

        # Wait for all tasks
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Graceful shutdown."""
        if not self._running:
            return
        self._running = False
        self._log.info("engine_stopping")

        # Cancel running tasks
        for task in self._tasks:
            task.cancel()

        # Stop strategies, collect orders to cancel
        all_cancel_ids: list[str] = []
        for strategy in self._strategies:
            try:
                ids = await strategy.on_stop()
                all_cancel_ids.extend(ids)
            except Exception as e:
                self._log.error("strategy_stop_error", strategy=strategy.name, error=str(e))

        # Also cancel all open orders from portfolio
        all_cancel_ids.extend(self._portfolio.get_all_open_order_ids())
        unique_ids = list(set(all_cancel_ids))

        if unique_ids and not self._dry_run:
            self._log.info("canceling_all_orders", count=len(unique_ids))
            failed = await self._rest.batch_cancel_orders(unique_ids)
            if failed:
                self._log.warning("some_cancels_failed", count=len(failed))

        # Disconnect WebSocket
        if self._ws:
            await self._ws.disconnect()

        # Final sync + snapshot
        if self._portfolio and self._db:
            await self._portfolio.sync()
            bal = self._portfolio.balance
            await self._db.record_pnl_snapshot(
                balance=bal.available_balance,
                portfolio_value=bal.portfolio_value,
                realized_pnl=0,
                total_positions=len(self._portfolio._positions),
            )

        # Close REST + DB
        if self._rest:
            await self._rest.close()
        if self._db:
            await self._db.close()

        self._log.info("engine_stopped")

    # ─── Main Loop ────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        """Core trading loop."""
        tick_interval = self._config.engine.tick_interval

        while self._running:
            tick_start = time.monotonic()

            if self._risk and self._risk.is_halted:
                self._log.warning("risk_halted", reason=self._risk.halt_reason)
                await asyncio.sleep(tick_interval * 10)
                continue

            for ticker in self._active_tickers:
                snapshot = self._market_snapshots.get(ticker)
                if snapshot is None:
                    continue

                for strategy in self._strategies:
                    if ticker not in strategy._active_tickers:
                        continue
                    try:
                        signal = await strategy.on_tick(ticker, snapshot)
                        if signal:
                            await self._execute_signal(signal, strategy)
                    except Exception as e:
                        self._log.error(
                            "strategy_tick_error",
                            strategy=strategy.name,
                            ticker=ticker,
                            error=str(e),
                        )

            # Check paper fills after processing all tickers
            if self._dry_run and self._paper_orders:
                await self._check_paper_fills()

            elapsed = time.monotonic() - tick_start
            sleep_time = max(0, tick_interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def _execute_signal(self, signal: StrategySignal, strategy: BaseStrategy) -> None:
        """Execute a strategy signal with risk checks."""
        self._log.debug("executing_signal", strategy=strategy.name, reason=signal.reason)

        # Cancel orders first
        for order_id in signal.orders_to_cancel:
            try:
                if self._dry_run:
                    # Remove from paper orders list
                    self._paper_orders = [
                        o for o in self._paper_orders if o["order_id"] != order_id
                    ]
                    await self._db.update_order_status(order_id, "canceled")
                else:
                    await self._rest.cancel_order(order_id)
                self._risk.unregister_order()
            except KalshiNotFoundError:
                pass
            except Exception as e:
                self._log.warning("cancel_error", order_id=order_id, error=str(e))

        # Place new orders
        for req in signal.orders_to_place:
            check = self._risk.check_order(req)
            if not check.approved:
                self._log.info(
                    "order_rejected",
                    strategy=strategy.name,
                    ticker=req.ticker,
                    reason=check.reason,
                )
                continue

            # Apply adjusted size if risk manager reduced it
            if check.adjusted_size is not None:
                req.count = check.adjusted_size

            if self._dry_run:
                self._paper_order_counter += 1
                paper_id = f"paper-{self._paper_order_counter:06d}"
                price = req.yes_price or req.no_price
                self._log.info(
                    "dry_run_order",
                    strategy=strategy.name,
                    ticker=req.ticker,
                    action=req.action.value,
                    side=req.side.value,
                    price=price,
                    count=req.count,
                    paper_id=paper_id,
                )
                paper_order = {
                    "order_id": paper_id,
                    "ticker": req.ticker,
                    "action": req.action.value,
                    "side": req.side.value,
                    "price": price,
                    "count": req.count,
                    "strategy": strategy.name,
                    "placed_at": time.time(),
                }
                self._paper_orders.append(paper_order)
                # Track order ID so strategy can cancel it next cycle
                if isinstance(strategy, MarketMakerStrategy):
                    is_bid = req.action == OrderAction.BUY
                    strategy.track_order_id(req.ticker, paper_id, is_bid)
                # Record in DB so dashboard can see it
                await self._db.record_order({
                    "order_id": paper_id,
                    "client_order_id": "",
                    "ticker": req.ticker,
                    "action": req.action.value,
                    "side": req.side.value,
                    "type": "limit",
                    "price": price,
                    "count": req.count,
                    "status": "resting",
                    "strategy_name": strategy.name,
                })
                continue

            try:
                order = await self._rest.place_order(req)
                self._risk.register_order()

                # Track order IDs for market maker
                if isinstance(strategy, MarketMakerStrategy):
                    is_bid = req.action == OrderAction.BUY
                    strategy.track_order_id(req.ticker, order.order_id, is_bid)

                # Record in database
                await self._db.record_order({
                    "order_id": order.order_id,
                    "client_order_id": order.client_order_id,
                    "ticker": order.ticker,
                    "action": order.action.value,
                    "side": order.side.value,
                    "type": order.type.value,
                    "price": order.yes_price or order.no_price,
                    "count": order.initial_count,
                    "status": order.status.value,
                    "strategy_name": strategy.name,
                })

                await self._portfolio.on_order_update(order)

            except Exception as e:
                self._log.error(
                    "order_placement_failed",
                    strategy=strategy.name,
                    ticker=req.ticker,
                    error=str(e),
                )

    # ─── Market Discovery ─────────────────────────────────────────

    async def _discover_markets(self) -> list[str]:
        """Discover tradeable markets closing within 72 hours.

        Uses max_close_ts to fetch ALL open markets closing soon (sports,
        weather, econ, etc.), then batch-probes orderbooks to find the
        most liquid ones. No need to iterate series — the API handles
        time filtering directly.
        """
        configured = self._config.engine.markets
        if configured:
            return configured

        now = datetime.now(timezone.utc)
        cutoff_72h = now + timedelta(hours=72)
        max_close_ts = int(cutoff_72h.timestamp())
        min_close_ts = int(now.timestamp())

        # ── Fetch all open markets closing within 72h (exclude MVE bundles) ──
        self._log.info("discovering_markets_72h")
        all_short: list[tuple[str, str]] = []  # (ticker, title)
        cursor = None
        for page in range(3):  # Up to 3000 markets
            markets, cursor = await self._rest.get_markets(
                limit=1000, status="open",
                max_close_ts=max_close_ts,
                min_close_ts=min_close_ts,
                cursor=cursor,
                mve_filter="exclude",
            )
            for m in markets:
                all_short.append((m.ticker, m.title or ""))
            self._log.info("discovery_page", page=page + 1, markets=len(markets), total=len(all_short))
            if not cursor or not markets:
                break

        self._log.info("short_term_candidates", count=len(all_short))
        filtered = all_short  # Already filtered by mve_filter=exclude

        # ── Batch-probe orderbooks to find liquid markets ──
        tickers_to_probe = [t for t, _ in filtered[:500]]
        liquid: list[tuple[int, int, str, str]] = []  # (depth, -spread, ticker, title)

        if tickers_to_probe:
            try:
                ob_map = await self._rest.get_orderbooks_batch(
                    tickers_to_probe, depth=5,
                )
                for ticker, ob in ob_map.items():
                    has_bid = ob.best_bid is not None
                    has_ask = ob.best_ask is not None
                    if has_bid or has_ask:
                        depth = len(ob.yes_bids) + len(ob.yes_asks)
                        spread = 99
                        if has_bid and has_ask:
                            spread = ob.best_ask - ob.best_bid
                            # Prefer tight spreads (tradeable)
                            if spread <= 15:
                                depth += 50  # bonus for tight spread
                        title = next((ti for t, ti in filtered if t == ticker), "")
                        liquid.append((depth, -spread, ticker, title))
            except Exception as e:
                self._log.warning("batch_orderbook_failed", error=str(e),
                                  msg="Falling back to individual probes")
                for i in range(0, min(len(tickers_to_probe), 50), 5):
                    batch = tickers_to_probe[i:i + 5]
                    tasks = [self._rest.get_orderbook(t, depth=5) for t in batch]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for ticker, result in zip(batch, results):
                        if isinstance(result, Exception):
                            continue
                        if result.best_bid is not None or result.best_ask is not None:
                            depth = len(result.yes_bids) + len(result.yes_asks)
                            title = next((ti for t, ti in filtered if t == ticker), "")
                            liquid.append((depth, 0, ticker, title))

        liquid.sort(key=lambda x: (-x[0], x[1]))
        short_picks = [t for _, _, t, _ in liquid[:10]]

        for depth, neg_spread, ticker, title in liquid[:10]:
            self._log.info(
                "liquid_market",
                ticker=ticker,
                title=title[:50],
                depth=depth,
                spread=-neg_spread,
            )

        self._log.info("liquid_picks", count=len(short_picks))

        # ── Fallback: event-based for broader coverage ──
        event_picks: list[str] = []
        if len(short_picks) < 30:
            all_event_tickers = await self._rest.get_event_tickers(
                limit=200, status="open", max_pages=1,
            )
            self._log.info("discovered_events", count=len(all_event_tickers))

            all_markets = []
            for i in range(0, min(len(all_event_tickers), 50), 5):
                batch = all_event_tickers[i:i + 5]
                tasks = [
                    self._rest.get_markets(limit=50, event_ticker=et)
                    for et in batch
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        continue
                    markets, _ = result
                    all_markets.extend(markets)

            scored = []
            for m in all_markets:
                if m.close_time:
                    days_left = (m.close_time - now).total_seconds() / 86400
                    if days_left < 0:
                        continue
                else:
                    days_left = 999
                has_book = 1 if (m.yes_bid > 0 and m.yes_ask > 0) else 0
                spread = (m.yes_ask - m.yes_bid) if has_book else 99
                time_score = max(0, 100 - days_left)
                vol_score = min(m.volume, 10000)
                score = has_book * 100000 + time_score * 100 + vol_score
                scored.append((score, spread, days_left, m))

            scored.sort(key=lambda x: (-x[0], x[1]))
            event_picks = [m.ticker for _, _, _, m in scored[:15]]

        # ── Merge: liquid short-term first, then event picks ──
        seen = set()
        final: list[str] = []
        for t in short_picks + event_picks:
            if t not in seen:
                seen.add(t)
                final.append(t)
            if len(final) >= 15:
                break

        if not final:
            # Last resort: just grab whatever's available
            for t, _ in filtered[:10]:
                final.append(t)

        self._log.info("markets_discovered", count=len(final),
                        tickers=final[:10])
        return final

    # ─── Strategy Initialization ──────────────────────────────────

    async def _init_strategies(self) -> None:
        """Create and start enabled strategies."""
        strat_configs = self._config.strategies

        if strat_configs.market_maker.enabled:
            mm = MarketMakerStrategy(
                name="market_maker",
                params=strat_configs.market_maker.params,
            )
            tickers = strat_configs.market_maker.markets or self._active_tickers
            await mm.on_start(tickers)
            # Load persisted state
            for ticker in tickers:
                state_json = await self._db.load_strategy_state("market_maker", ticker)
                if state_json:
                    try:
                        mm.load_state(json.loads(state_json))
                    except Exception:
                        pass
            self._strategies.append(mm)

        if strat_configs.mean_reversion.enabled:
            from kalshi_bot.strategies.mean_reversion import MeanReversionStrategy
            mr = MeanReversionStrategy(
                name="mean_reversion",
                params=strat_configs.mean_reversion.params,
            )
            tickers = strat_configs.mean_reversion.markets or self._active_tickers
            await mr.on_start(tickers)
            self._strategies.append(mr)

        if strat_configs.value.enabled:
            from kalshi_bot.strategies.value import ValueStrategy
            vs = ValueStrategy(
                name="value",
                params=strat_configs.value.params,
            )
            tickers = strat_configs.value.markets or self._active_tickers
            vs.set_capital(self._portfolio.balance.available_balance)
            await vs.on_start(tickers)
            self._strategies.append(vs)

    # ─── WebSocket Handlers ───────────────────────────────────────

    async def _on_ws_ticker(self, msg: dict) -> None:
        """Handle ticker update from WebSocket."""
        ticker = msg.get("market_ticker", "")
        if ticker not in self._market_snapshots:
            return
        snapshot = self._market_snapshots[ticker]
        market = snapshot.market
        if "yes_bid" in msg:
            market.yes_bid = int(msg["yes_bid"])
        if "yes_ask" in msg:
            market.yes_ask = int(msg["yes_ask"])
        if "last_price" in msg:
            market.last_price = int(msg["last_price"])
        if "volume" in msg:
            market.volume = int(msg["volume"])

    async def _on_ws_orderbook(self, msg: dict) -> None:
        """Handle orderbook delta from WebSocket."""
        ticker = msg.get("market_ticker", "")
        if ticker in self._market_snapshots:
            # Refresh full orderbook on delta (simpler than applying deltas)
            try:
                ob = await self._rest.get_orderbook(ticker, depth=10)
                self._market_snapshots[ticker].orderbook = ob
            except Exception:
                pass

    async def _on_ws_fill(self, msg: dict) -> None:
        """Handle fill event from WebSocket."""
        fill = Fill(
            trade_id=msg.get("trade_id", ""),
            order_id=msg.get("order_id", ""),
            ticker=msg.get("ticker", ""),
            action=OrderAction(msg.get("action", "buy")),
            side=Side(msg.get("side", "yes")),
            count=int(msg.get("count", 0)),
            yes_price=int(msg.get("yes_price", 0)),
            no_price=int(msg.get("no_price", 0)),
        )
        await self._portfolio.on_fill(fill)

        # Notify strategies
        for strategy in self._strategies:
            if fill.ticker in strategy._active_tickers:
                try:
                    await strategy.on_fill(fill)
                except Exception as e:
                    self._log.error("strategy_fill_error", strategy=strategy.name, error=str(e))

    # ─── Periodic Tasks ───────────────────────────────────────────

    async def _periodic_sync(self) -> None:
        """Sync portfolio every 60s."""
        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                break
            if self._simulated:
                # In simulated mode, don't try API sync — just update paper P&L
                await self._update_paper_pnl()
                continue
            try:
                await self._portfolio.sync()
                # Update value strategy capital
                for s in self._strategies:
                    if hasattr(s, "set_capital"):
                        s.set_capital(self._portfolio.balance.available_balance)
            except Exception as e:
                self._log.error("periodic_sync_error", error=str(e))

    async def _periodic_snapshot_refresh(self) -> None:
        """Refresh market snapshots every 30s via REST as fallback."""
        while self._running:
            await asyncio.sleep(30)
            if not self._running:
                break
            try:
                await self._refresh_all_snapshots()
            except Exception as e:
                self._log.error("snapshot_refresh_error", error=str(e))

    async def _refresh_all_snapshots(self) -> None:
        """Fetch snapshots for all active tickers."""
        # Batch in groups of 5 to respect rate limits
        for i in range(0, len(self._active_tickers), 5):
            batch = self._active_tickers[i:i + 5]
            tasks = [self._rest.get_snapshot(t) for t in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for ticker, result in zip(batch, results):
                if isinstance(result, Exception):
                    self._log.warning("snapshot_fetch_failed", ticker=ticker, error=str(result))
                else:
                    self._market_snapshots[ticker] = result

    async def _log_status_loop(self) -> None:
        """Log status every 30s."""
        while self._running:
            await asyncio.sleep(30)
            if not self._running:
                break

            # Update paper P&L before logging
            if self._dry_run and self._simulated:
                await self._update_paper_pnl()

            risk_status = self._risk.get_status()
            paper_info = {}
            if self._dry_run:
                active_paper = {t: p for t, p in self._paper_positions.items() if p != 0}
                paper_info = {
                    "paper_pnl_cents": self._paper_pnl,
                    "paper_fills": self._paper_fills,
                    "paper_positions": active_paper,
                    "pending_paper_orders": len(self._paper_orders),
                }
            self._log.info(
                "status",
                **risk_status,
                **paper_info,
                active_markets=len(self._active_tickers),
                strategies=[s.name for s in self._strategies],
                dry_run=self._dry_run,
            )
            # Record PnL snapshot
            try:
                bal = self._portfolio.balance
                total_pos = len(self._paper_positions) if self._dry_run else len(self._portfolio._positions)
                await self._db.record_pnl_snapshot(
                    balance=bal.available_balance,
                    portfolio_value=bal.portfolio_value,
                    realized_pnl=self._paper_pnl if self._dry_run else 0,
                    total_positions=total_pos,
                )
            except Exception as e:
                self._log.warning("pnl_snapshot_error", error=str(e))

    # ─── Paper Trading Simulation ───────────────────────────────────

    async def _check_paper_fills(self) -> None:
        """Check if any paper orders would have been filled by current market prices.

        Only fills ONE order per ticker per tick to prevent avalanche fills.
        Enforces position limits before filling.
        """
        filled_indices = []
        filled_tickers: set[str] = set()  # Only one fill per ticker per tick
        max_pos = self._config.risk.max_position_per_market

        for i, order in enumerate(self._paper_orders):
            ticker = order["ticker"]

            # Only one fill per ticker per tick cycle
            if ticker in filled_tickers:
                continue

            snapshot = self._market_snapshots.get(ticker)
            if not snapshot or not snapshot.orderbook:
                continue

            ob = snapshot.orderbook
            price = order["price"]
            action = order["action"]
            side = order["side"]
            count = order["count"]

            # Check position limit before filling
            current_pos = self._paper_positions.get(ticker, 0)
            if action == "buy" and current_pos >= max_pos:
                continue
            if action == "sell" and current_pos <= -max_pos:
                continue

            # Check if market has crossed our price
            filled = False
            if action == "buy":
                if side == "yes" and ob.best_ask and ob.best_ask <= price:
                    filled = True
                elif side == "no":
                    no_ask = 100 - ob.best_bid if ob.best_bid else None
                    if no_ask and no_ask <= price:
                        filled = True
            else:  # sell
                if side == "yes" and ob.best_bid and ob.best_bid >= price:
                    filled = True
                elif side == "no":
                    no_bid = 100 - ob.best_ask if ob.best_ask else None
                    if no_bid and no_bid >= price:
                        filled = True

            if filled:
                filled_indices.append(i)
                filled_tickers.add(ticker)

                # Update paper positions
                if action == "buy":
                    delta = count if side == "yes" else -count
                else:
                    delta = -count if side == "yes" else count
                self._paper_positions[ticker] = current_pos + delta

                # Track cost (negative for buys, positive for sells)
                cost_cents = price * count
                if action == "buy":
                    self._paper_pnl -= cost_cents
                else:
                    self._paper_pnl += cost_cents
                self._paper_fills += 1

                fill_id = f"paper-fill-{self._paper_fills:06d}"
                self._log.info(
                    "paper_fill",
                    ticker=ticker,
                    action=action,
                    side=side,
                    price=price,
                    count=count,
                    net_position=self._paper_positions[ticker],
                    paper_pnl_cents=self._paper_pnl,
                )

                # Record fill in DB
                await self._db.record_fill({
                    "trade_id": fill_id,
                    "order_id": order["order_id"],
                    "ticker": ticker,
                    "action": action,
                    "side": side,
                    "count": count,
                    "price": price,
                })

                await self._db.update_order_status(order["order_id"], "executed")

                # Notify strategies
                fill = Fill(
                    trade_id=fill_id,
                    order_id=order["order_id"],
                    ticker=ticker,
                    action=OrderAction(action),
                    side=Side(side),
                    count=count,
                    yes_price=price if side == "yes" else 0,
                    no_price=price if side == "no" else 0,
                )
                for strategy in self._strategies:
                    if ticker in strategy._active_tickers:
                        try:
                            await strategy.on_fill(fill)
                        except Exception:
                            pass

        # Remove filled orders (reverse order to preserve indices)
        for i in reversed(filled_indices):
            self._paper_orders.pop(i)

        # Expire old paper orders (older than 2 minutes)
        now = time.time()
        expired = [o for o in self._paper_orders if now - o["placed_at"] > 120]
        for o in expired:
            self._paper_orders.remove(o)
            await self._db.update_order_status(o["order_id"], "canceled")

    async def _update_paper_pnl(self) -> None:
        """Compute current paper portfolio value and record snapshot."""
        base_balance = 100_000  # $1,000 starting
        # Mark-to-market open positions
        unrealized = 0
        for ticker, pos in self._paper_positions.items():
            if pos == 0:
                continue
            snapshot = self._market_snapshots.get(ticker)
            if snapshot and snapshot.orderbook:
                mid = snapshot.orderbook.mid_price
                if mid:
                    # Value of position: pos * mid_price (if long yes, mid is value)
                    unrealized += pos * mid

        total_value = base_balance + self._paper_pnl + unrealized
        self._portfolio._balance.available_balance = total_value
        self._portfolio._balance.portfolio_value = total_value

        self._risk.update_balance(total_value)

        self._log.debug(
            "paper_pnl_update",
            base=base_balance,
            realized_pnl=self._paper_pnl,
            unrealized=unrealized,
            total_value=total_value,
            positions=dict(self._paper_positions),
        )

    # ─── Save/Load Strategy State ─────────────────────────────────

    async def _save_strategy_states(self) -> None:
        """Persist all strategy states."""
        for strategy in self._strategies:
            state = strategy.get_state()
            for ticker in strategy._active_tickers:
                await self._db.save_strategy_state(
                    strategy.name, ticker, json.dumps(state),
                )
