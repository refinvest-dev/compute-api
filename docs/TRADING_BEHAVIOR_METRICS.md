# Trading Review behavior metric engine

The pure analytics package is `src/refinvest/trading_review/analytics`. Its calculation
source of truth is Context [`TRADING_ANALYTICS.md`](../../../context/docs/TRADING_ANALYTICS.md)
§§1–14 and ADR-060 at Context revision
`bd1c26d5e63b4b3ba51932df77d1510362599a79`. Definition bundle
`TRADING_BEHAVIOR_METRICS_V1` contains version `1` of:

- `ENTRY_WITHIN_WINDOW_AFTER_LOSS`
- `INITIAL_EXPOSURE_CHANGE_AFTER_LOSS`
- `WIN_LOSS_HOLDING_DURATION_DIFFERENCE`

`calculate_behavior_metrics` takes pinned `FuturesPositionEpisode` objects, their
canonical records, pinned `InstrumentDescriptor` terms, and a validated V1
`AnalysisConfig`. These are normalized/reconstructed inputs; the package does not
parse CSV or retrieve current terms. The caller must supply the complete immutable
set from one LedgerRevision and manifest. Opening execution prices and the pinned
multiplier produce initial notional. Outcome uses the normalized, fee-adjusted
`net_trading_pnl` and episode-scoped capabilities.

Each `MetricResult` carries definition/version, config and period, one of
`AVAILABLE`, `INSUFFICIENT_SAMPLE`, `MISSING_CAPABILITY`, or `NOT_APPLICABLE`,
finding eligibility, required capabilities, population/eligible/excluded counts,
reason counts, ordered subject/comparison/excluded memberships, scalar inputs,
metric values, and evidence references. Relation candidates retain selected,
later, same-time, reversal, and after-window roles. Evidence references retain
episode, allocation/canonical record IDs, and opaque source row provenance.
Public API paging, persistence, generated IDs, and publication are outside this
package.

The V1 config pins UTC `[startInclusive,endExclusive)`, `PT30M`, outcome and
funding policies, baseline policy, and separate metric/finding minimums. Exact
Decimal products and integer microsecond durations feed reduced rational values.
Ordering follows the definition's loss, entry, baseline, and holding keys, so
input order and repeated execution do not affect the logical result. Canonical
JSON equality is tested using the existing normalization serializer. A new
Analytics result hash is intentionally deferred until its full projection and
artifact format are specified by Context; this package does not invent one.

Run the Context §13 boundary fixtures and determinism tests with:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_trading_review_analytics.py -q
```

The fixtures use synthetic normalized episodes and independent expected values
from all 28 Context boundary scenarios. They also exercise capability, quality,
empty period, invalid config, exact outcome sign, input shuffling, and Decimal
context changes. This package does not implement Core `TradingAnalysisRun`, HTTP,
worker orchestration, long-term persistence, finding ranking, or result delivery.
