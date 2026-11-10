# Rebuild validation

Validated locally on 2026-11-09 using Python 3.12 and requests 2.34.2. No RIT connection or live orders were used. The branch is a practice candidate, not a demonstrated profitable strategy.

The unit/integration suite covers risk reservation, fee economics, policy phases, depth signals, order reconciliation, cancellation races, stale-book and phase deadlines, uncertain writes, liquidation and log analysis. Risk property checks enumerate independent full/zero fill corners across 500 generated portfolios. Full-round integration includes ten seeded rounds.

An additional 25 synthetic full-round checks use five seeds for each stylized regime. All finished flat with no outstanding orders and consistent fill bookkeeping. Results:

| Regime | Met 100,000 shares | Executed-volume range | Hard stops |
|---|---:|---:|---:|
| Balanced | 5/5 | 244,148–251,118 | 0 |
| Crowded queues | 5/5 | 118,774–122,000 | 0 |
| Persistent trend | 0/5 | 12,818–17,730 | 5 |
| Quote churn | 5/5 | 124,356–138,688 | 0 |
| One-sided closeout | 5/5 | 244,148–251,118 | 0 |

The persistent-trend scenario exposed a real limitation: the strategy reached its hard loss stop and liquidated before qualifying. This must not be hidden by quoting only successful scenarios or changing the risk stop until the test passes. It motivates competitive practice, signal calibration and comparison of alternative inventory policies. The simulator is not fitted to RIT or classmates' algorithms, so its qualification fractions are not estimates of live probabilities and its P&L is not evidence of an edge.

Run `python -m unittest discover -s tests -v` and `python stress_check.py` to reproduce. Official RIT results and competitive run logs are still required before selecting final parameters. Changing minimum volume to 20,000 based on an unconfirmed interpretation of the grading document would conceal rather than resolve this limitation.
