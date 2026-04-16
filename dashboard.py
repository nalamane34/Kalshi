"""Live monitoring dashboard for the Kalshi trading bot.

Run alongside the bot:
    python dashboard.py [--port 8080]

Opens a web dashboard showing:
- Account balance and P&L
- Active positions
- Recent trades/fills
- Strategy status
- Risk metrics
- P&L chart over time
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template_string, jsonify

app = Flask(__name__)
DB_PATH = "kalshi_bot.db"


# ─── HTML Dashboard Template ─────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kalshi Bot Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
            background: #0a0e17;
            color: #e0e6f0;
            min-height: 100vh;
        }
        .header {
            background: linear-gradient(135deg, #1a1f35 0%, #0d1220 100%);
            border-bottom: 1px solid #2a3050;
            padding: 16px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header h1 {
            font-size: 18px;
            font-weight: 600;
            color: #7eb8ff;
        }
        .header .status {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
        }
        .status-dot {
            width: 8px; height: 8px;
            border-radius: 50%;
            background: #34d399;
            animation: pulse 2s infinite;
        }
        .status-dot.halted { background: #ef4444; animation: none; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
            padding: 20px 24px;
        }
        .card {
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 8px;
            padding: 16px;
        }
        .card-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #6b7280;
            margin-bottom: 8px;
        }
        .card-value {
            font-size: 28px;
            font-weight: 700;
        }
        .card-value.positive { color: #34d399; }
        .card-value.negative { color: #ef4444; }
        .card-value.neutral { color: #7eb8ff; }
        .card-sub {
            font-size: 12px;
            color: #6b7280;
            margin-top: 4px;
        }
        .section {
            padding: 0 24px 20px;
        }
        .section-title {
            font-size: 14px;
            font-weight: 600;
            color: #9ca3af;
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 1px solid #1f2937;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        th {
            text-align: left;
            padding: 8px 12px;
            color: #6b7280;
            font-weight: 500;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid #1f2937;
        }
        td {
            padding: 8px 12px;
            border-bottom: 1px solid #111827;
        }
        tr:hover { background: #1a1f35; }
        .tag {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }
        .tag-buy { background: #064e3b; color: #34d399; }
        .tag-sell { background: #7f1d1d; color: #fca5a5; }
        .tag-yes { background: #1e3a5f; color: #7eb8ff; }
        .tag-no { background: #3b1f4a; color: #c084fc; }
        .tag-resting { background: #1f2937; color: #9ca3af; }
        .tag-executed { background: #064e3b; color: #34d399; }
        .tag-canceled { background: #7f1d1d; color: #fca5a5; }
        .pnl-chart {
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 8px;
            padding: 16px;
            height: 200px;
            position: relative;
            overflow: hidden;
        }
        .chart-canvas { width: 100%; height: 100%; }
        .risk-bar {
            height: 6px;
            background: #1f2937;
            border-radius: 3px;
            overflow: hidden;
            margin-top: 8px;
        }
        .risk-fill {
            height: 100%;
            border-radius: 3px;
            transition: width 0.5s ease;
        }
        .risk-fill.safe { background: #34d399; }
        .risk-fill.warning { background: #fbbf24; }
        .risk-fill.danger { background: #ef4444; }
        .auto-refresh { color: #4b5563; font-size: 12px; }
        .empty-state {
            text-align: center;
            padding: 40px;
            color: #4b5563;
        }
        @media (max-width: 900px) {
            .grid { grid-template-columns: repeat(2, 1fr); }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>KALSHI BOT</h1>
        <div class="status">
            <div class="status-dot" id="statusDot"></div>
            <span id="statusText">Connecting...</span>
            <span class="auto-refresh">Auto-refresh: 5s</span>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <div class="card-title">Balance</div>
            <div class="card-value neutral" id="balance">--</div>
            <div class="card-sub" id="portfolioValue">Portfolio: --</div>
        </div>
        <div class="card">
            <div class="card-title">Daily P&L</div>
            <div class="card-value" id="dailyPnl">--</div>
            <div class="card-sub" id="dailyPnlPct">--%</div>
        </div>
        <div class="card">
            <div class="card-title">Drawdown</div>
            <div class="card-value" id="drawdown">--</div>
            <div class="card-sub" id="drawdownLimit">Limit: 15%</div>
        </div>
        <div class="card">
            <div class="card-title">Open Orders</div>
            <div class="card-value neutral" id="openOrders">--</div>
            <div class="card-sub" id="activeMarkets">Markets: --</div>
        </div>
    </div>

    <div class="section">
        <div class="section-title">Risk Gauges</div>
        <div class="card" style="display:grid; grid-template-columns: 1fr 1fr 1fr; gap: 24px;">
            <div>
                <div class="card-title">Daily Loss</div>
                <div class="risk-bar"><div class="risk-fill safe" id="dailyLossBar" style="width:0%"></div></div>
                <div class="card-sub" id="dailyLossText">0% / 5%</div>
            </div>
            <div>
                <div class="card-title">Max Drawdown</div>
                <div class="risk-bar"><div class="risk-fill safe" id="drawdownBar" style="width:0%"></div></div>
                <div class="card-sub" id="drawdownText">0% / 15%</div>
            </div>
            <div>
                <div class="card-title">Position Usage</div>
                <div class="risk-bar"><div class="risk-fill safe" id="positionBar" style="width:0%"></div></div>
                <div class="card-sub" id="positionText">0 / 50</div>
            </div>
        </div>
    </div>

    <div class="section">
        <div class="section-title">P&L Over Time</div>
        <div class="pnl-chart">
            <canvas id="pnlChart" class="chart-canvas"></canvas>
        </div>
    </div>

    <div class="section">
        <div class="section-title">Positions</div>
        <div class="card" style="padding:0; overflow: auto; max-height: 300px;">
            <table>
                <thead>
                    <tr><th>Ticker</th><th>Position</th><th>Exposure</th><th>P&L</th><th>Fees</th></tr>
                </thead>
                <tbody id="positionsTable">
                    <tr><td colspan="5" class="empty-state">No positions</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <div class="section">
        <div class="section-title">Recent Orders</div>
        <div class="card" style="padding:0; overflow: auto; max-height: 300px;">
            <table>
                <thead>
                    <tr><th>Time</th><th>Ticker</th><th>Action</th><th>Side</th><th>Price</th><th>Count</th><th>Status</th><th>Strategy</th></tr>
                </thead>
                <tbody id="ordersTable">
                    <tr><td colspan="8" class="empty-state">No orders yet</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <div class="section">
        <div class="section-title">Recent Fills</div>
        <div class="card" style="padding:0; overflow: auto; max-height: 300px;">
            <table>
                <thead>
                    <tr><th>Time</th><th>Ticker</th><th>Action</th><th>Side</th><th>Price</th><th>Count</th></tr>
                </thead>
                <tbody id="fillsTable">
                    <tr><td colspan="6" class="empty-state">No fills yet</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <script>
    function fmt$(cents) {
        if (cents == null) return '--';
        return '$' + (cents / 100).toFixed(2);
    }
    function fmtPct(val) {
        if (val == null) return '--%';
        return val.toFixed(2) + '%';
    }
    function riskClass(pct, limit) {
        let ratio = Math.abs(pct) / limit;
        if (ratio > 0.8) return 'danger';
        if (ratio > 0.5) return 'warning';
        return 'safe';
    }

    let pnlHistory = [];

    function drawChart(canvas, data) {
        let ctx = canvas.getContext('2d');
        let w = canvas.width = canvas.parentElement.clientWidth - 32;
        let h = canvas.height = canvas.parentElement.clientHeight - 32;
        ctx.clearRect(0, 0, w, h);

        if (data.length < 2) {
            ctx.fillStyle = '#4b5563';
            ctx.font = '13px monospace';
            ctx.textAlign = 'center';
            ctx.fillText('Collecting data...', w/2, h/2);
            return;
        }

        let values = data.map(d => d.balance);
        let min = Math.min(...values);
        let max = Math.max(...values);
        let range = max - min || 1;

        // Grid
        ctx.strokeStyle = '#1f2937';
        ctx.lineWidth = 1;
        for (let i = 0; i < 5; i++) {
            let y = h * i / 4;
            ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
        }

        // Line
        ctx.beginPath();
        ctx.strokeStyle = values[values.length-1] >= values[0] ? '#34d399' : '#ef4444';
        ctx.lineWidth = 2;
        for (let i = 0; i < values.length; i++) {
            let x = (i / (values.length - 1)) * w;
            let y = h - ((values[i] - min) / range) * h * 0.9 - h * 0.05;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();

        // Fill
        ctx.lineTo(w, h);
        ctx.lineTo(0, h);
        ctx.closePath();
        let grad = ctx.createLinearGradient(0, 0, 0, h);
        let color = values[values.length-1] >= values[0] ? '52, 211, 153' : '239, 68, 68';
        grad.addColorStop(0, `rgba(${color}, 0.15)`);
        grad.addColorStop(1, `rgba(${color}, 0.0)`);
        ctx.fillStyle = grad;
        ctx.fill();

        // Labels
        ctx.fillStyle = '#6b7280';
        ctx.font = '10px monospace';
        ctx.textAlign = 'left';
        ctx.fillText(fmt$(max), 4, 14);
        ctx.fillText(fmt$(min), 4, h - 4);
    }

    async function refresh() {
        try {
            let resp = await fetch('/api/status');
            let data = await resp.json();

            // Status
            let dot = document.getElementById('statusDot');
            let text = document.getElementById('statusText');
            if (data.halted) {
                dot.className = 'status-dot halted';
                text.textContent = 'HALTED: ' + data.halt_reason;
            } else {
                dot.className = 'status-dot';
                text.textContent = 'Running' + (data.dry_run ? ' (DRY RUN)' : '');
            }

            // Cards
            document.getElementById('balance').textContent = fmt$(data.balance);
            document.getElementById('portfolioValue').textContent = 'Portfolio: ' + fmt$(data.portfolio_value);

            let pnl = data.balance - data.starting_balance;
            let pnlEl = document.getElementById('dailyPnl');
            pnlEl.textContent = fmt$(pnl);
            pnlEl.className = 'card-value ' + (pnl >= 0 ? 'positive' : 'negative');
            document.getElementById('dailyPnlPct').textContent = fmtPct(data.daily_pnl_pct);

            let ddEl = document.getElementById('drawdown');
            ddEl.textContent = fmtPct(data.drawdown_pct);
            ddEl.className = 'card-value ' + (data.drawdown_pct > 10 ? 'negative' : data.drawdown_pct > 5 ? 'neutral' : 'positive');

            document.getElementById('openOrders').textContent = data.open_orders;
            document.getElementById('activeMarkets').textContent = 'Markets: ' + data.active_markets + ' | Fills: ' + (data.total_fills || 0);

            // Risk bars
            let dlPct = Math.min(Math.abs(data.daily_pnl_pct) / data.daily_loss_limit * 100, 100);
            let dlBar = document.getElementById('dailyLossBar');
            dlBar.style.width = dlPct + '%';
            dlBar.className = 'risk-fill ' + riskClass(data.daily_pnl_pct, data.daily_loss_limit);
            document.getElementById('dailyLossText').textContent = Math.abs(data.daily_pnl_pct).toFixed(1) + '% / ' + data.daily_loss_limit + '%';

            let ddPctBar = Math.min(data.drawdown_pct / data.max_drawdown * 100, 100);
            let ddBar = document.getElementById('drawdownBar');
            ddBar.style.width = ddPctBar + '%';
            ddBar.className = 'risk-fill ' + riskClass(data.drawdown_pct, data.max_drawdown);
            document.getElementById('drawdownText').textContent = data.drawdown_pct.toFixed(1) + '% / ' + data.max_drawdown + '%';

            let posCount = Object.keys(data.positions || {}).filter(k => data.positions[k] !== 0).length;
            let posBar = document.getElementById('positionBar');
            posBar.style.width = Math.min(posCount / 50 * 100, 100) + '%';
            posBar.className = 'risk-fill ' + riskClass(posCount, 50);
            document.getElementById('positionText').textContent = posCount + ' / 50';

            // Positions table
            let posTbody = document.getElementById('positionsTable');
            let positions = data.positions_detail || [];
            if (positions.length === 0) {
                posTbody.innerHTML = '<tr><td colspan="5" class="empty-state">No positions</td></tr>';
            } else {
                posTbody.innerHTML = positions.map(p => `
                    <tr>
                        <td>${p.ticker}</td>
                        <td style="color:${p.position > 0 ? '#34d399' : p.position < 0 ? '#ef4444' : '#6b7280'}">${p.position}</td>
                        <td>${fmt$(p.market_exposure)}</td>
                        <td style="color:${p.realized_pnl >= 0 ? '#34d399' : '#ef4444'}">${fmt$(p.realized_pnl)}</td>
                        <td>${fmt$(p.fees_paid)}</td>
                    </tr>
                `).join('');
            }

            // PnL chart
            pnlHistory = data.pnl_history || [];
            drawChart(document.getElementById('pnlChart'), pnlHistory);

        } catch(e) {
            document.getElementById('statusText').textContent = 'Connection error';
            document.getElementById('statusDot').className = 'status-dot halted';
        }

        try {
            // Orders
            let ordResp = await fetch('/api/orders');
            let orders = await ordResp.json();
            let ordTbody = document.getElementById('ordersTable');
            if (orders.length === 0) {
                ordTbody.innerHTML = '<tr><td colspan="8" class="empty-state">No orders yet</td></tr>';
            } else {
                ordTbody.innerHTML = orders.map(o => `
                    <tr>
                        <td>${o.created_at ? o.created_at.slice(11,19) : '--'}</td>
                        <td>${o.ticker}</td>
                        <td><span class="tag tag-${o.action}">${o.action.toUpperCase()}</span></td>
                        <td><span class="tag tag-${o.side}">${o.side.toUpperCase()}</span></td>
                        <td>${o.price}¢</td>
                        <td>${o.count}</td>
                        <td><span class="tag tag-${o.status}">${o.status}</span></td>
                        <td>${o.strategy_name || '-'}</td>
                    </tr>
                `).join('');
            }

            // Fills
            let fillResp = await fetch('/api/fills');
            let fills = await fillResp.json();
            let fillTbody = document.getElementById('fillsTable');
            if (fills.length === 0) {
                fillTbody.innerHTML = '<tr><td colspan="6" class="empty-state">No fills yet</td></tr>';
            } else {
                fillTbody.innerHTML = fills.map(f => `
                    <tr>
                        <td>${f.created_at ? f.created_at.slice(11,19) : '--'}</td>
                        <td>${f.ticker}</td>
                        <td><span class="tag tag-${f.action}">${f.action.toUpperCase()}</span></td>
                        <td><span class="tag tag-${f.side}">${f.side.toUpperCase()}</span></td>
                        <td>${f.price}¢</td>
                        <td>${f.count}</td>
                    </tr>
                `).join('');
            }
        } catch(e) {}
    }

    // Refresh every 5 seconds
    setInterval(refresh, 5000);
    refresh();
    window.addEventListener('resize', () => drawChart(document.getElementById('pnlChart'), pnlHistory));
    </script>
</body>
</html>
"""


# ─── Database Queries ─────────────────────────────────────────────

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def query_status():
    """Build status payload from database."""
    db = get_db()
    try:
        # Latest PnL snapshot
        row = db.execute(
            "SELECT * FROM pnl_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()

        # First snapshot today
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        first = db.execute(
            "SELECT * FROM pnl_snapshots WHERE timestamp LIKE ? ORDER BY id ASC LIMIT 1",
            (f"{today}%",),
        ).fetchone()

        # PnL history
        pnl_rows = db.execute(
            "SELECT balance_cents, portfolio_value_cents, timestamp FROM pnl_snapshots ORDER BY id DESC LIMIT 500"
        ).fetchall()
        pnl_history = [
            {"balance": r["balance_cents"], "timestamp": r["timestamp"]}
            for r in reversed(pnl_rows)
        ]

        balance = row["balance_cents"] if row else 0
        portfolio_value = row["portfolio_value_cents"] if row else 0
        starting_balance = first["balance_cents"] if first else balance
        total_positions = row["total_positions"] if row else 0

        daily_pnl_pct = 0.0
        if starting_balance > 0:
            daily_pnl_pct = (balance - starting_balance) / starting_balance * 100

        # Peak balance for drawdown
        peak_row = db.execute(
            "SELECT MAX(balance_cents) as peak FROM pnl_snapshots"
        ).fetchone()
        peak = peak_row["peak"] if peak_row and peak_row["peak"] else balance
        drawdown_pct = (peak - balance) / peak * 100 if peak > 0 else 0

        # Positions from strategy state
        positions = {}
        positions_detail = []
        state_rows = db.execute(
            "SELECT * FROM strategy_state"
        ).fetchall()
        for sr in state_rows:
            try:
                state = json.loads(sr["state_json"])
                inv = state.get("inventory", {})
                positions.update(inv)
            except (json.JSONDecodeError, KeyError):
                pass

        # Paper positions from fills
        fill_rows = db.execute(
            "SELECT ticker, action, side, count, price FROM fills ORDER BY created_at"
        ).fetchall()
        paper_positions = {}
        realized_pnl = 0
        for f in fill_rows:
            ticker = f["ticker"]
            action = f["action"]
            side = f["side"]
            count = f["count"]
            price = f["price"]
            if action == "buy":
                delta = count if side == "yes" else -count
                realized_pnl -= price * count
            else:
                delta = -count if side == "yes" else count
                realized_pnl += price * count
            paper_positions[ticker] = paper_positions.get(ticker, 0) + delta

        # Merge positions
        for ticker, pos in paper_positions.items():
            if pos != 0:
                positions[ticker] = pos
                positions_detail.append({
                    "ticker": ticker,
                    "position": pos,
                    "market_exposure": abs(pos) * 50,  # estimated mid
                    "realized_pnl": 0,
                    "fees_paid": 0,
                })

        # Open orders count
        open_count = db.execute(
            "SELECT COUNT(*) as cnt FROM orders WHERE status = 'resting'"
        ).fetchone()["cnt"]

        # Distinct markets with activity
        active_markets = db.execute(
            "SELECT COUNT(DISTINCT ticker) as cnt FROM orders"
        ).fetchone()["cnt"]

        # Total fills
        total_fills = db.execute(
            "SELECT COUNT(*) as cnt FROM fills"
        ).fetchone()["cnt"]

        return {
            "balance": balance,
            "portfolio_value": portfolio_value,
            "starting_balance": starting_balance,
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "open_orders": open_count,
            "active_markets": active_markets,
            "total_fills": total_fills,
            "positions": positions,
            "positions_detail": positions_detail,
            "halted": False,
            "halt_reason": "",
            "dry_run": True,
            "daily_loss_limit": 5.0,
            "max_drawdown": 15.0,
            "pnl_history": pnl_history,
        }
    finally:
        db.close()


def query_orders(limit=50):
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def query_fills(limit=50):
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM fills ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


# ─── Flask Routes ─────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/status")
def api_status():
    return jsonify(query_status())


@app.route("/api/orders")
def api_orders():
    return jsonify(query_orders())


@app.route("/api/fills")
def api_fills():
    return jsonify(query_fills())


# ─── Entry Point ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kalshi Bot Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="Dashboard port")
    parser.add_argument("--host", default="0.0.0.0", help="Dashboard host")
    parser.add_argument("--db", default="kalshi_bot.db", help="Database path")
    args = parser.parse_args()

    global DB_PATH
    DB_PATH = args.db

    print(f"Dashboard starting at http://{args.host}:{args.port}")
    print(f"Reading from database: {DB_PATH}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
