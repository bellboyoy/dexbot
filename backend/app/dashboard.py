"""PAPER dashboard. The page is static; numbers come from the JSON endpoints."""
from __future__ import annotations

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DexBot PAPER</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; background: #12140f; color: #e7e2d4; }
  header { background: #8a5a00; color: #fff8e8; padding: 16px 20px; }
  header strong { font-size: 22px; letter-spacing: 0.08em; }
  header p { margin: 6px 0 0; }
  main { padding: 16px 20px 40px; max-width: 1100px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
  .card { background: #1c1f16; border: 1px solid #34382c; border-radius: 8px; padding: 10px 12px; }
  .card span { display: block; color: #b7b29f; font-size: 12px; }
  .card b { font-size: 18px; font-weight: 650; }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #34382c; font-variant-numeric: tabular-nums; }
  h2 { font-size: 16px; margin: 22px 0 8px; }
  .bad { color: #ffb4a8; }
  .ok { color: #b7e3b1; }
  #error { color: #ffb4a8; }
</style>
</head>
<body>
<header>
  <strong>PAPER TRADING</strong>
  <p>Simulated funds only. This process cannot sign, approve tokens, or send a live order.</p>
</header>
<main>
  <p id="error"></p>
  <section class="grid" id="stats"></section>
  <h2>Open positions</h2>
  <table id="positions"><thead><tr>
    <th>Symbol</th><th>Chain</th><th>Strategy</th><th>Size</th><th>Entry</th><th>Mark</th><th>Unrealised</th><th>Stop</th><th>Target</th>
  </tr></thead><tbody></tbody></table>
  <h2>Per strategy</h2>
  <table id="strategies"><thead><tr>
    <th>Strategy</th><th>Paper trades</th><th>Wins</th><th>Losses</th><th>Win rate</th><th>Paper net</th><th>Open</th><th>Backtest OOS trades</th><th>Backtest OOS net</th><th>Backtest OOS exp</th><th>Passed</th>
  </tr></thead><tbody></tbody></table>
  <h2>System</h2>
  <pre id="system"></pre>
</main>
<script>
const money = (v) => v == null ? "n/a" : v;
const pct = (v) => v == null ? "n/a" : (Number(v) * 100).toFixed(1) + "%";
function card(label, value) {
  return `<div class="card"><span>${label}</span><b>${value}</b></div>`;
}
async function refresh() {
  const err = document.getElementById("error");
  let data;
  try {
    const res = await fetch("/api/paper/summary", {cache: "no-store"});
    if (!res.ok) throw new Error("ledger unavailable (" + res.status + ")");
    data = await res.json();
  } catch (e) {
    err.textContent = String(e.message || e);
    return;
  }
  err.textContent = "";
  const s = data.system || {};
  document.getElementById("stats").innerHTML = [
    card("Mode", "PAPER"),
    card("Simulated balance", money(data.balance_usd)),
    card("Available capital", money(data.available_capital_usd)),
    card("Capital deployed", money(data.capital_deployed_usd)),
    card("Realised P&L", money(data.realized_pnl_usd)),
    card("Unrealised P&L", money(data.unrealized_pnl_usd)),
    card("Net P&L after costs", money(data.net_pnl_usd)),
    card("Trades", data.trade_count),
    card("Wins", data.wins),
    card("Losses", data.losses),
    card("Win rate", pct(data.win_rate)),
    card("Average win", money(data.average_win_usd)),
    card("Average loss", money(data.average_loss_usd)),
    card("Fees paid", money(data.fees_usd)),
    card("Gas paid", money(data.gas_usd)),
    card("Slippage paid", money(data.slippage_usd)),
  ].join("");
  const pos = document.querySelector("#positions tbody");
  pos.innerHTML = (data.open_positions || []).map(p => `<tr>
    <td>${p.symbol || ""}</td><td>${p.chain}</td><td>${p.strategy}</td>
    <td>${money(p.size_usd)}</td><td>${money(p.entry_price_usd)}</td><td>${money(p.mark_price_usd)}</td>
    <td>${money(p.unrealized_pnl_usd)}</td><td>${money(p.stop_loss_price_usd)}</td><td>${money(p.take_profit_price_usd)}</td>
  </tr>`).join("") || `<tr><td colspan="9">No open positions</td></tr>`;
  const strat = document.querySelector("#strategies tbody");
  strat.innerHTML = (data.strategies || []).map(r => `<tr>
    <td>${r.strategy}</td><td>${r.trades}</td><td>${r.wins}</td><td>${r.losses}</td>
    <td>${pct(r.win_rate)}</td><td>${money(r.net_pnl_usd)}</td><td>${r.open_positions}</td>
    <td>${r.backtest_oos_trades ?? "n/a"}</td><td>${money(r.backtest_oos_net_pnl)}</td>
    <td>${money(r.backtest_oos_expectancy)}</td><td>${r.backtest_passed === true ? "yes" : r.backtest_passed === false ? "no" : "n/a"}</td>
  </tr>`).join("") || `<tr><td colspan="11">No strategies</td></tr>`;
  document.getElementById("system").textContent = JSON.stringify({
    trading_mode: s.trading_mode, execution: s.execution, kill_switch: s.kill_switch,
    kill_reason: s.kill_reason, cooldown_until: s.cooldown_until, last_scan_at: s.last_scan_at,
    last_error: s.last_error, scan_count: s.scan_count, last_cycle: s.last_cycle,
    validated_strategies: data.validated_strategies, validation_note: data.validation_note,
    providers: data.providers
  }, null, 2);
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
