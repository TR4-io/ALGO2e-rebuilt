# ALGO2e adaptive market maker

A rebuilt Python REST strategy for the RSM434 ALGO2e simulation. It combines passive market making with selective liquidity-taking, inventory management, participation pacing and execution-quality feedback. Parameters are explicit hypotheses; no live competitive performance has been measured for this rebuild.

## Run

Use Python 3.10+ on the computer running the RIT Client. Enable API orders, connect to ALGO2e, and start flat with no open orders. Use one bot and no manual trading on this account. Restart the process for each run.

```sh
python -m pip install -r requirements.txt
python strategy.py --check
python strategy.py
```

`--check` reads case, securities, orders and limits; it never submits or cancels orders. The last command trades in the connected simulation. Connection defaults are `http://localhost:9999/v1` and the primer's `Rotman` API key. Override with `RIT_BASE_URL` and `RIT_API_KEY`; never commit personal keys.

Ctrl+C once requests liquidation. Ctrl+C again exits immediately after best-effort cancellation; remaining inventory needs attention in RIT. Missing liquidity or connectivity can prevent liquidation.

Options:

```sh
python strategy.py --config config.example.json
python strategy.py --no-taker
python analyze_run.py logs/run-YOUR-RUN.jsonl
python -m unittest discover -s tests -v
python stress_check.py
```

## The volume requirement is per run

The supplied **RSM434H1F Fall 2026 Performance Evaluation Methodology**, p. 1, lists five ranked ALGO2e runs and a 100,000-share minimum. Page 2 explicitly applies the minimum in each simulation run. Buys plus sells count; automatic closing transactions do not. The earlier initial run was a separate nonzero-P&L participation exercise.

Default minimum is therefore **100,000 per run**, with a 110,000 target by tick 240. `--minimum-volume NUMBER` or the JSON setting can override it if the instructor confirms a change. This program does not assume 100,000 spread across five runs.

The final grade depends on per-run P&L ranks and can be reduced by a downside-deviation grade. Lower-risk participation matters, but a no-loss record does not automatically produce a high P&L rank. The bot cannot observe everyone else's final scores or directly optimize the unknown live rank.

## Strategy decisions

1. **Signals from multiple book levels.** Remove all known own order IDs, aggregate remaining quantities, and use the first five price levels within five cents. Combine weighted depth imbalance, top-level microprice, snapshot order-flow imbalance, short momentum and volatility. Smooth rapidly changing quantities and discount unstable depth. A data gap resets the signal warmup.
2. **Per-security economics.** CNR has taker fee 0.0027 and maker rebate 0.0023; RY has taker fee -0.0014 and maker rebate -0.0020; AC has taker fee 0.0015 and maker rebate 0.0011, in dollars/share. Fee signs come from the supplied case brief, p. 2. A RY rebate alone does not justify crossing a spread.
3. **Selective passive quotes.** Shift the quote centre with inventory, widen with volatility and adverse post-fill drift, and reduce size under risk pressure. Join the best price by default. Improve by one cent only with sufficient spread and participation pressure or an inventory-reducing purpose. Do not keep improving merely because a competitor does.
4. **Preserve queue priority.** An unchanged attractive order has no arbitrary four-second expiry. Cancel when its estimated raw edge turns negative, exposure exceeds the current dynamic inventory target, market data becomes invalid, or a material price/phase change warrants replacement. Ordinary repricing has a minimum delay; risk cancellations do not. This uses visible competing depth, not an exact queue position estimate.
5. **Selective taker trades.** Require signal warmup, persistent direction, manageable churn, a cooldown, and expected edge after TWO taker fees, a bid/ask exit estimate, volatility and an uncertainty buffer. Take at the observed best opposing price with a small clip. Cancel own quotes first, refresh the book and decision, and cancel unfilled remainder. REST cancellation emulates immediate-or-cancel only approximately; fills can race it. Speculative taking stops at tick 240 and under soft-loss pressure.
6. **Opportunity priority.** Service securities in estimated spread/fee/risk opportunity order, rotating ties. This is a heuristic priority rule, not a fitted fill-probability or optimal capital-allocation model. Every accepted order still reserves global and per-stock risk.
7. **Participation pacing.** Falling behind target increases clip sizes and permitted quote improvement, without deliberately accepting negative raw edge to manufacture volume. If still under 100,000 at tick 260, continue selective making until tick 275 with shrinking inventory targets. Otherwise switch to reduction at 260. Stop new exposure at 275 and actively liquidate at 280. Qualification is not guaranteed: crowded or unprofitable markets can still leave a shortfall.
8. **Risk controls.** Start with gross/net budgets of 18,000 shares and a 6,000 per-stock budget, capped by live limits with headroom. Outstanding buys and sells reserve independent worst-case fills; opposite orders are not assumed to cancel each other's risk. Volatility, time and losses shrink the desired stock inventory. Soft loss at $500 reduces risk and disables speculative taking. Hard loss at $1,500, or a $900 retreat after a peak of at least $500, triggers liquidation even if unqualified. These are uncalibrated defaults. Stops use available securities realized+unrealized values relative to startup, can overshoot, and may omit charges not represented in those fields.

Signals react to visible behaviour. They do not identify teams, know rival inventory, distinguish every cancellation from a trade, or infer intrinsic value. No separate trade tape is queried. Snapshot order-flow imbalance includes additions/cancellations and must not be interpreted as executed aggressive flow. The exchange API may truncate book responses beyond the requested 1,000 entries.

Existing inventory above the dynamic target, or substantial inventory facing a persistent adverse signal, also triggers small best-price risk-reducing trades before the final closeout. These may pay a spread even without predictive profit; they reduce exposure. `--no-taker` disables speculative trades only, not these risk reductions or final liquidation.

## Execution and liquidation

- Batch open-order reconciliation, with terminal-order lookup when an order disappears. Up to three position/fill snapshot reconciliations tolerate fills between separate reads.
- A cancellation is not proof of zero fills: retrieve final cumulative fills before freeing the order's risk. Accepted writes with uncertain outcomes are never replayed.
- Refresh after cancellation and recheck age before every submission. Locally known self-crosses are blocked. There is no exchange-side post-only guarantee: a quote can become marketable during network transit.
- On regular API errors, attempt cancellation before retrying. Authentication failure, repeated failure or unknown writes stop the process. Rate-limit waits obey the server; orders can remain exposed while requests are unavailable.
- Emergency sells need only bids; emergency buys need only asks. Sweep available displayed depth up to 5,000 shares per order, with a price cap ten cents from the current best, widening to thirty cents in the last eight seconds. These are per-order caps relative to the current book, not a cumulative liquidation-loss ceiling.
- Cleanup cancels all three securities on the exclusive account, checks remaining orders/positions and reports qualification. It does not submit blind market orders after an error. A case reset prevents reuse of old IDs.

## Logs and analysis

`logs/run-*.jsonl` records configuration, book signals and the first ten aggregate levels, submissions, cancellation reasons, confirmed fill deltas, status, errors and closeout. There is no API key in logs. Book reads are sequential REST snapshots, not an atomic exchange event stream. The request duration includes local processing and is not a precise exchange latency measurement.

At approximately 0.5, 1 and 3 seconds after detecting a fill, record subsequent price movement. Record actual observed delay; discard excessively late samples. If the API supplies valid cumulative order `vwap`, derive incremental fill price; otherwise execution price and gross fill markout stay null. Never substitute a marketable order's limit price for actual execution price. VWAP-based gross markouts exclude fees and may aggregate multiple fills between polls.

The adaptive penalty uses adverse midpoint movement **after fill detection**, for maker-intent orders only, with ten-sample shrinkage and a three-cent cap. This is a delayed toxicity proxy, not exact fill-time adverse selection or realized P&L. Intent is not authoritative liquidity classification: orders may cross during transit. If no sufficiently recent reference snapshot is available, skip that fill's markout.

The analyzer shows confirmed volume by security/intent, weighted drift by side/horizon, final reported P&L, qualification and errors. Volume counts this process's confirmed fills only. Unknown accepted writes can understate it. The official RIT report remains authoritative.

## Validation and practice protocol

Tests cover independent-fill risk combinations, fee signs, depth signals, queue preservation, negative-edge cancellation, one-sided liquidation, stale data, self-cross protection, partial fills, cancellation races, rate limiting, uncertain submissions and resets. Seeded full rounds stress crowded queues, trends, changing quotes and one-sided books. The simulator accounts for simple queue-ahead quantities, partial fills and fees, but its liquidity and competitors are stylized. **These checks do not establish expected P&L, a winning configuration or live qualification probability.**

Before grading:

1. Run `--check`, then competitive practice with the new version. Record official final P&L, volume, residual positions and any fines or errors for every run.
2. Compare paired groups of fresh runs with and without taker trading, then vary queue repricing or signal weights one component at a time. Do not tune on the same runs used to judge improvement.
3. Inspect fill drift, API errors and shortfall timing. If volume is low, inspect whether prices are behind the market or queues are crowded before raising size. If drift is persistently adverse, reduce that side's risk and investigate signals.
4. Judge five-run batches using the course's actual grade formula when peer results are available, alongside qualification frequency and loss severity. Preserve bad runs in the dataset.

## Files

`strategy.py`: CLI and shutdown; `engine.py`: lifecycle and reconciliation; `execution.py`: REST, ledger and risk; `signals.py`: book estimates; `policy.py`: quote/taker/phase decisions; `config.py`: validated settings; `analyze_run.py`: offline logs; `stress_check.py`: synthetic checks.

Course PDFs are referenced above and are not redistributed. This rebuild retains the original draft's fee convention, REST transport and independent-fill risk approach while replacing its strategy and lifecycle logic.
