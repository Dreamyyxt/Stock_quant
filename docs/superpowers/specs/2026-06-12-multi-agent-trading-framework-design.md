# Multi-Agent Trading Framework Design

Date: 2026-06-12
Project: Stock_quant

## 1. Purpose

This document defines a practical multi-agent trading framework for `Stock_quant`.
It is based on two reference articles:

- `https://mp.weixin.qq.com/s/osJiVbXi0zSxt0H1ky-KYw`
- `https://mp.weixin.qq.com/s/nfR0GbWjPOkbhcl2HWGLTw`

The goal is not to copy an external framework directly. The goal is to upgrade the
current system from a low-frequency portfolio rebalancer into a modular trading
platform where AI agents assist research, signal interpretation, risk review,
execution supervision, and post-trade learning.

The first implementation target should be conservative:

- keep the existing long-term portfolio system intact;
- add a separate intraday/paper-trading agent layer;
- let LLM agents analyze and explain, but not directly submit live orders;
- give deterministic risk rules final veto power before execution.

## 2. Reference Article Takeaways

### 2.1 Hermes / OPC Article

The Hermes article frames AI quant trading as a long-term "human plus AI" workflow,
not a one-shot trading bot. The useful ideas for this project are:

- AI agents should be split by responsibility so context does not become polluted.
- Long-term memory matters: trade preferences, mistakes, lessons, and review notes
  should become reusable knowledge.
- AI is a leverage layer. It improves strategy iteration and research speed, but it
  also amplifies unclear thinking.
- Backtest results are only evidence, not proof. Any strategy should run in a
  simulated environment before touching live capital.
- Every loss should leave an artifact: what was believed, why the trade was taken,
  what failed, and how the rule set should change.

### 2.2 TradingAgents Article

The TradingAgents article argues for a multi-agent financial decision process. The
most useful pattern is to split trading decisions into specialist agents:

- data collection;
- market analysis;
- strategy generation;
- risk management.

For `Stock_quant`, this maps naturally to a pipeline where agents produce structured
reports, a decision layer combines them, and a risk layer approves or rejects any
trade plan.

## 3. Current System Context

The existing project already has a useful foundation:

- `core/data_fetcher.py` fetches account, position, quote, and historical data.
- `execution/order_manager.py` places, cancels, and syncs broker orders.
- `execution/fixed_rebalancer.py` manages the current fixed-weight portfolio.
- `execution/rebalancer.py` manages dynamic strategy rebalancing.
- `risk/risk_manager.py` handles stop losses, drawdown checks, and risk reports.
- `scheduler/task_scheduler.py` runs monitor and rebalance jobs.
- `backtest/` contains historical backtesting entry points.
- `strategies/` contains momentum, quality/value, combined strategy, and adaptive
  overlay logic.

The current system is mostly a portfolio rebalancing system. It is intentionally
low-frequency: with `FIXED_PORTFOLIO["use_fixed_portfolio"] = True`, trades only
occur when weights drift enough or risk rules fire. The multi-agent layer should
therefore be added alongside the existing system, not by weakening the current
long-term rebalancer.

## 4. Design Principles

1. Separate thinking from execution.
   Agents may analyze, explain, and propose. Only execution modules submit orders.

2. Risk has veto power.
   The risk layer can reject or reduce any trade, regardless of signal strength.

3. Long-term and intraday capital are isolated.
   Long-term holdings should not be churned by intraday signals.

4. Start with paper trading.
   The first version should produce trade plans and simulated fills before live
   intraday execution.

5. Store every decision.
   Each signal, approval, rejection, trade, and post-trade review should be logged
   in a form that can be audited later.

6. Prefer small, testable modules.
   Each agent should have a clear input schema and output schema.

## 5. Target Architecture

```text
Market Data Hub
  -> Research Agent Layer
  -> Strategy Agent Layer
  -> Decision Committee
  -> Risk Agent
  -> Execution Agent
  -> Monitor Agent
  -> Review and Memory Layer
```

## 6. Module Responsibilities

### 6.1 Market Data Hub

Purpose:
Collect market, account, position, event, and news data into normalized snapshots.

Initial modules:

```text
core/market_context.py
core/news_fetcher.py
core/macro_fetcher.py
core/intraday_data_fetcher.py
```

Core output:

```python
MarketSnapshot(
    symbol="NVDA",
    timestamp=...,
    price=...,
    intraday_bars=...,
    daily_bars=...,
    account=...,
    position=...,
    open_orders=...,
    news=[],
    macro_events=[],
)
```

First version scope:

- use Tiger quote/account/position data where available;
- use existing historical price fetching where possible;
- defer expensive news/social integrations until the trading loop is stable.

### 6.2 Research Agent Layer

Purpose:
Produce independent analysis reports. These agents do not create orders.

Agents:

```text
TechnicalAnalystAgent
FundamentalAnalystAgent
NewsSentimentAgent
MacroAgent
PositionContextAgent
```

Example output:

```python
AnalysisReport(
    agent="TechnicalAnalystAgent",
    symbol="NVDA",
    horizon="intraday",
    bias="bullish",
    confidence=0.72,
    reasons=[
        "price above VWAP",
        "opening range breakout",
        "volume above 20-bar average",
    ],
    risks=[
        "market index weakening",
        "spread wider than normal",
    ],
)
```

First version scope:

- implement `TechnicalAnalystAgent` with deterministic indicators;
- implement `PositionContextAgent` using current account and position data;
- keep `MacroAgent` and `NewsSentimentAgent` as report stubs or manual-input
  modules until reliable data sources are selected.

### 6.3 Strategy Agent Layer

Purpose:
Convert analysis reports into candidate opportunities.

Two strategy tracks should coexist:

```text
LongTermPortfolioAgent
IntradayOpportunityAgent
```

`LongTermPortfolioAgent` wraps the current fixed/dynamic rebalancing logic. It
continues to operate on the existing schedule.

`IntradayOpportunityAgent` only manages a separate intraday risk budget. It should
not change long-term target weights.

Initial intraday strategies:

```text
OpeningRangeBreakoutStrategy
VWAPMomentumStrategy
IntradayReversalStrategy
SignalCombiner
```

First version rule set:

- do not trade during the first 15 minutes after market open;
- trade only liquid symbols such as `SPY`, `QQQ`, `AAPL`, `MSFT`, `NVDA`, `AMD`,
  and `TSLA`;
- require price direction, VWAP state, volume confirmation, and market index
  direction to agree;
- create trade plans only, not live orders.

### 6.4 Decision Committee

Purpose:
Combine reports and candidate signals into a single `TradePlan`.

The decision layer should be transparent. It should show which agents supported the
trade, which opposed it, and why.

Example output:

```python
TradePlan(
    symbol="NVDA",
    action="BUY",
    horizon="intraday",
    confidence=0.74,
    entry_type="limit",
    entry_price=...,
    stop_loss=...,
    take_profit=...,
    max_hold_minutes=120,
    source_signals=[
        "opening_range_breakout",
        "above_vwap",
        "volume_expansion",
    ],
    reasons=[...],
    risks=[...],
)
```

The decision layer can use LLMs to explain and synthesize, but it should not be the
only source of truth. The first version should use deterministic scoring and store
LLM commentary as an explanation field.

### 6.5 Risk Agent

Purpose:
Approve, reject, or resize each `TradePlan`.

The risk agent has final veto power.

Initial hard rules:

```text
intraday_capital_fraction: 0.05 to 0.15 of account equity
max_risk_per_trade: 0.001 to 0.003 of account equity
max_intraday_loss: 0.005 to 0.01 of account equity
max_intraday_trades: 3 to 6
max_symbol_exposure: 0.20 to 0.30 of intraday capital
no_new_trades_after: 15:30 ET
force_flat_after: 15:45 ET
stop_after_consecutive_losses: 2
```

Risk output:

```python
RiskDecision(
    status="APPROVE" | "REJECT" | "REDUCE_SIZE",
    approved_quantity=...,
    reason=...,
    violated_rules=[],
)
```

### 6.6 Execution Agent

Purpose:
Execute approved plans and supervise broker order state.

Initial modules:

```text
execution/trade_plan_executor.py
execution/intraday_executor.py
```

Responsibilities:

- verify the market is open;
- check duplicate orders;
- place limit orders;
- cancel stale orders;
- sync order status;
- emit structured events;
- never override risk rejection.

First version scope:

- paper execution only;
- live execution disabled by default;
- reuse `OrderManager` once live execution is explicitly enabled.

### 6.7 Monitor Agent

Purpose:
Track active intraday plans and enforce exits.

Responsibilities:

- stop loss;
- take profit;
- time stop;
- stale order cancellation;
- end-of-day flattening;
- account drawdown guard.

This agent should run more frequently than the long-term monitor.

### 6.8 Review and Memory Layer

Purpose:
Turn trading activity into reusable learning artifacts.

Initial files:

```text
memory/trade_journal.jsonl
memory/mistake_log.md
memory/strategy_lessons.md
memory/agent_preferences.yaml
```

Each trade journal entry should record:

- timestamp;
- symbol;
- proposed action;
- approved action;
- agents that supported the trade;
- agents that opposed the trade;
- entry, stop, target, and exit;
- planned risk and realized P&L;
- whether the plan was followed;
- review notes.

Weekly review questions:

- Which signals produced avoidable losses?
- Which agent was too optimistic or too conservative?
- Which rejected trades would have worked?
- Did the risk rules prevent bad outcomes?
- Should any symbol, time window, or signal be disabled next week?

## 7. Proposed Directory Structure

```text
agents/
  __init__.py
  base_agent.py
  schemas.py
  technical_analyst.py
  fundamental_analyst.py
  news_sentiment_agent.py
  macro_agent.py
  position_context_agent.py
  decision_agent.py
  risk_agent.py
  review_agent.py

strategies/
  intraday/
    __init__.py
    opening_range_breakout.py
    vwap_momentum.py
    reversal.py
    signal_combiner.py

risk/
  intraday_risk_manager.py

execution/
  trade_plan_executor.py
  intraday_executor.py

core/
  market_context.py
  intraday_data_fetcher.py
  news_fetcher.py
  macro_fetcher.py

scheduler/
  intraday_scheduler.py

memory/
  trade_journal.jsonl
  mistake_log.md
  strategy_lessons.md
  agent_preferences.yaml
```

## 8. Data Flow

### 8.1 Intraday Flow

```text
IntradayScheduler tick
  -> MarketDataHub builds MarketSnapshot
  -> TechnicalAnalystAgent analyzes snapshot
  -> PositionContextAgent adds account/position state
  -> IntradayOpportunityAgent creates candidate signal
  -> DecisionAgent creates TradePlan
  -> RiskAgent approves/rejects/resizes
  -> PaperExecutor records simulated order
  -> MonitorAgent tracks exit rules
  -> ReviewAgent writes journal entry
```

### 8.2 Long-Term Flow

```text
Existing scheduled rebalance
  -> FixedPortfolioRebalancer or PortfolioRebalancer
  -> Existing RiskManager
  -> Existing OrderManager
  -> EventLogger
  -> ReviewAgent weekly summary
```

The long-term flow should remain stable while the intraday flow is introduced.

## 9. Scheduler Design

Suggested intraday schedule in US Eastern time:

```text
09:25 ET   build premarket context and watchlist
09:30 ET   market opens, observe only
09:45 ET   allow first intraday signal scan
09:45-11:30 ET   main opportunity window
11:30-13:30 ET   reduced activity
13:30-15:30 ET   high-quality signals only
15:30 ET   block new entries
15:45 ET   force flat intraday positions
16:05 ET   write daily review
```

## 10. Configuration

Add a separate intraday config block rather than mixing it into the long-term
strategy config:

```python
INTRADAY_CONFIG = {
    "enabled": False,
    "paper_trading": True,
    "symbols": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD", "TSLA"],
    "bar_interval": "5min",
    "capital_fraction": 0.05,
    "max_risk_per_trade": 0.001,
    "max_daily_loss": 0.005,
    "max_daily_trades": 3,
    "no_trade_before": "09:45",
    "no_new_entries_after": "15:30",
    "force_flat_after": "15:45",
    "live_execution_enabled": False,
}
```

## 11. Error Handling

The system should fail closed.

Reject or pause trading when:

- market data is missing or stale;
- account summary cannot be fetched;
- current positions cannot be fetched;
- active order state is unknown;
- current time is outside allowed trading windows;
- daily risk limit is breached;
- duplicate open orders exist for the same symbol;
- broker API returns an unclear or failed order response.

Every rejection should be logged with a machine-readable reason.

## 12. Testing Strategy

First implementation should include tests for:

- indicator calculations;
- strategy signal generation;
- decision scoring;
- risk approval/rejection rules;
- paper execution state transitions;
- end-of-day flattening behavior;
- journal entry creation.

Backtesting should be added only after the first deterministic strategy interfaces
are stable. Paper trading comes before live trading.

## 13. Implementation Phases

### Phase 1: Foundations

- Add schemas for `MarketSnapshot`, `AnalysisReport`, `TradePlan`, and
  `RiskDecision`.
- Add `INTRADAY_CONFIG`.
- Add paper-only journal writer.
- Add deterministic `TechnicalAnalystAgent`.

### Phase 2: Paper Intraday Loop

- Add opening-range breakout and VWAP momentum strategies.
- Add `DecisionAgent` with deterministic scoring.
- Add `IntradayRiskManager`.
- Add `PaperTradePlanExecutor`.
- Add `intraday` run mode in `main.py`.

### Phase 3: Monitoring and Review

- Add `MonitorAgent` for stop, target, time stop, and force-flat logic.
- Add daily and weekly review reports.
- Add mistake log generation.

### Phase 4: LLM-Assisted Analysis

- Add optional LLM commentary for trade plans.
- Add macro/news reports as context.
- Keep LLM output advisory only.

### Phase 5: Controlled Live Execution

- Enable live execution only behind explicit config flags.
- Start with very small capital fraction.
- Require risk approval and duplicate-order checks.
- Keep paper shadow mode running beside live trades.

## 14. Non-Goals for the First Version

- No autonomous live LLM trading.
- No options trading.
- No high-frequency trading.
- No automatic strategy parameter optimization from small samples.
- No weakening of the existing fixed portfolio system.
- No live execution until paper trading produces auditable logs.

## 15. Open Questions

1. Should the first intraday version trade only ETFs (`SPY`, `QQQ`) or include
   single names such as `NVDA`, `AMD`, and `TSLA`?
2. Which data source should provide reliable intraday bars for paper trading?
3. Should LLM usage start with daily review only, or also produce premarket macro
   notes?
4. What is the maximum capital fraction allowed for intraday trading once live
   execution is enabled?

## 16. Recommended First Build

Build the smallest safe loop:

```text
5-minute bars
  -> TechnicalAnalystAgent
  -> OpeningRangeBreakoutStrategy
  -> DecisionAgent
  -> IntradayRiskManager
  -> PaperTradePlanExecutor
  -> trade_journal.jsonl
```

This gives the project a real multi-agent shape while keeping risk contained. Once
the paper loop is producing useful, inspectable logs, add LLM analysis and review
around it.
