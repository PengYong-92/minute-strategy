# Restore Observation-Profile Order Volume Plan

> **Goal:** Restore the pre-regression order source while preserving threshold audit data, two-order concurrency, and wave runtime observability.

## Behavior Boundary

- Daily-selected observation candidates may become executable signals again.
- A daily-selected candidate bypasses its original dynamic score threshold because its independent settled-sample profile is the admission condition.
- `TRADE_SCORE_THRESHOLD` remains accepted for deployment compatibility but is audit-only and cannot promote a primary `WAIT` signal.
- The one-minute wave model and wave-batch state remain recorded and displayed, but are disabled as order blockers by default.
- The rolling-edge guard and result-sequence loss guard remain the active loss controls.
- Production defaults remain two concurrent orders with a two-minute minimum entry gap.

## Implementation

1. Update `tests/test_state.py` first to cover observation-candidate promotion, audit-only manual threshold behavior, and non-blocking wave defaults.
2. Restore daily-profile candidate selection in `app/state.py` and restore `Signal.actionable` support for daily-selected profiles in `app/models.py`.
3. Change the default wave guard and wave-batch guard to observation-only; re-enable the result-sequence guard by default.
4. Update `app/server.py` and `scripts/run.sh` defaults and Chinese parameter descriptions.
5. Run targeted tests, the complete unit suite, Python compilation, and JavaScript syntax checks.
6. Commit and push without rewriting history.
7. Deploy a new release, set the daily profile admission threshold to 60%, remove the score override, clear only simulated-order/progression state, and restart the service.
8. Record the release, runtime configuration, and new sample boundary in the handoff document.
