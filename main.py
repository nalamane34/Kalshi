"""Kalshi Trading Bot — CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from dotenv import load_dotenv

from kalshi_bot.config import load_config
from kalshi_bot.engine import TradingEngine
from kalshi_bot.log import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kalshi Prediction Market Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                          # Run with defaults (demo mode)
  python main.py --env demo              # Explicit demo mode
  python main.py --dry-run               # Log signals without placing orders
  python main.py --markets TICKER-1 TICKER-2  # Trade specific markets
  python main.py --env production        # REAL MONEY (requires confirmation)
        """,
    )
    parser.add_argument(
        "--config", default="config/default.yaml",
        help="Path to config YAML file (default: config/default.yaml)",
    )
    parser.add_argument(
        "--env", choices=["demo", "production"], default=None,
        help="Override environment (default: from config)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run without placing orders — log signals only",
    )
    parser.add_argument(
        "--markets", nargs="+", default=None,
        help="Override market tickers to trade",
    )
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=None,
        help="Override log level",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    load_dotenv()
    config = load_config(args.config)

    if args.env:
        config.environment = args.env
    if args.markets:
        config.engine.markets = args.markets
    if args.log_level:
        config.logging.level = args.log_level

    setup_logging(config.logging.level, config.logging.format, config.logging.file)

    engine = TradingEngine(config, dry_run=args.dry_run)

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def handle_signal() -> None:
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Run engine in background, wait for shutdown signal
    engine_task = asyncio.create_task(engine.start())

    # Wait for shutdown signal or engine completion
    done, _ = await asyncio.wait(
        [engine_task, asyncio.create_task(shutdown_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    await engine.stop()

    # Check if engine raised an exception
    for task in done:
        if task.exception() and not isinstance(task.exception(), asyncio.CancelledError):
            raise task.exception()


def main() -> None:
    args = parse_args()

    # Safety check for production mode
    is_production = args.env == "production"
    if is_production:
        print("\n" + "=" * 60)
        print("  WARNING: PRODUCTION MODE — REAL MONEY")
        print("=" * 60)
        if args.dry_run:
            print("  (dry-run enabled, no orders will be placed)")
        else:
            confirm = input("  Type 'YES' to confirm: ").strip()
            if confirm != "YES":
                print("  Aborted.")
                sys.exit(1)
        print()

    print(f"Starting Kalshi Trading Bot...")
    print(f"  Environment: {args.env or 'demo (default)'}")
    print(f"  Dry run: {args.dry_run}")
    print(f"  Config: {args.config}")
    if args.markets:
        print(f"  Markets: {', '.join(args.markets)}")
    print()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nShutdown complete.")
    except Exception as e:
        print(f"\nFatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
