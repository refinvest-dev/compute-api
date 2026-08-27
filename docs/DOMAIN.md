# DOMAIN.md — refinvest-compute

## 1. Compute Domain Boundary

Compute는 다음 입력만으로 계산한다.

```text
StrategyVersion Payload
FeeModel
Requested Period
Dataset Snapshot
Engine Version
```

Compute는 다음을 알지 않는다.

- Member
- Subscription
- Strategy owner
- Plan quota
- UI state

## 2. Asset Universe

MVP:

```text
QQQ
SPY
TQQQ
SOXL
BTCUSDT
VIX
```

Execution 가능:

```text
QQQ
SPY
TQQQ
SOXL
BTCUSDT
```

VIX는 Signal/Reference 전용이다.

Daily Bar만 지원한다.

## 3. StrategyVersion Payload

```text
StrategyVersionPayload
├── primarySignalAsset
├── conditions
├── executionAsset
├── lag
├── exit.holdingSignalSessions
└── positionPolicy
```

Condition:

```text
Condition
├── operator: LT | GT | LTE | GTE
├── logicalCombinator: AND | OR | null
├── operandA: MetricReference
└── operandB: MetricReference | Literal
```

MetricReference:

```text
asset
metric: SIMPLE | RETURN | CHANGE
window: int | null
```

불변식:

- Primary Signal Asset은 명시적
- conditions ≥ 1
- RETURN / CHANGE window > 0
- SIMPLE window는 없음
- lag ≥ 0
- holdingSignalSessions > 0
- executionAsset에 VIX 불가
- PositionPolicy는 Long Only / Single Position / Duplicate Ignore

## 4. Temporal Semantics

### Metric Window

Referenced Asset 자신의 Trading Session을 사용한다.

### Cross-Calendar Anchor

Signal Timestamp에 Referenced Asset 세션이 없으면 해당 timestamp 이전에 완료된 가장 최근 세션을 사용한다.

미래 세션을 선택하지 않는다.

### Signal Timestamp

Primary Signal Asset Calendar에서 평가한다.

### Lag / Holding

Primary Signal Asset의 Signal Session 수로 계산한다.

### Execution

Entry/Exit 모두 Execution Asset의 Next Available Session에서 체결한다.

## 5. Missing Session

정상 Calendar 차이와 데이터 결측을 구분한다.

예:

```text
QQQ 토요일 세션 없음
→ 정상 calendar 차이

QQQ 평일 expected session인데 데이터 없음
→ fatal
```

Expected Session 결측은 보정하지 않는다.

## 6. DatasetSnapshot

```text
DatasetSnapshot
├── id
├── createdAt
├── source
├── coverage
├── adjustmentPolicy
├── storagePath
├── calendarSource
├── calendarVersion
└── calendarHash
```

Snapshot은 immutable이다.

동일 Backtest는 계산 중 Snapshot을 변경하지 않는다.

Calendar artifact의 source, version, content hash도 Snapshot에 고정된다. Engine은 해당
artifact의 `EXPECTED` / `NON_TRADING` 분류만 사용하며, 실행 중 exchange calendar나 vendor의
latest 데이터를 조회하지 않는다.

## 7. Execution Semantics

Reference Price:

```text
Execution Session Open
```

Slippage:

```text
Buy  = Open × (1 + slippage)
Sell = Open × (1 - slippage)
```

Commission은 FeeModel의 고정 비율을 사용한다.

Intraday 가격 추론은 하지 않는다.

## 8. Position Model

MVP:

```text
Long Only
Single Active Position
100% Capital Allocation
Duplicate Entry = IGNORE
```

이미 Position이 열려 있을 때 발생한 Entry Signal은 queue하거나 교체하지 않고 무시한다.

Exit은 Time-based만 지원한다.

## 9. Backtest Result

결과는 최소한 다음 의미를 보존한다.

```text
BacktestResult
├── metrics
├── equityCurve
├── trades
├── benchmark
├── signalExecutionDelay
├── sampleSizeWarning
└── dataIntegrityStatus
```

Benchmark:

- Primary: Execution Asset Buy & Hold
- Secondary: Signal Asset Buy & Hold (필요한 cross-market 전략)

## 10. Sample Semantics

```text
tradeCount == 0
→ 정상 완료
→ ZERO warning / empty-state semantics
```

0건을 Win Rate 0%, Sharpe 0처럼 의미가 다른 숫자로 변환하지 않는다.

```text
0 < tradeCount < 10
→ LOW sample warning
```

## 11. Fatal Errors

OpenAPI `FatalErrorCode`를 정본으로 사용한다.

대표 의미:

```text
MISSING_REQUIRED_DATA
DATASET_CORRUPTION
CALENDAR_RESOLUTION_FAILED
PRICE_DATA_MISSING
DSL_INVALID
```

`DSL_INVALID`는 실행 전 validation에서 차단한다.

나머지 데이터 오류는 실행 중 fail-fast할 수 있다.

## 12. Determinism Contract

다음이 같으면 결과가 같아야 한다.

```text
StrategyVersion
DatasetSnapshotId
EngineVersion
FeeModel
Requested/Actual Period semantics
```

이를 자동 regression test로 보장한다.

Engine의 wall clock, vendor latest response, 처리 순서의 비결정적 차이가 결과에 영향을 주면 안 된다.

## 13. Canonical Scenarios

최소 regression scenarios:

1. QQQ 단일 자산 조건
2. QQQ Signal → TQQQ Execution
3. QQQ + VIX AND
4. Primary Signal Asset이 Condition에 직접 없을 수 있음
5. BTC Signal → TQQQ Execution
6. BTC 주말 Signal → 다음 TQQQ Session
7. US Equity expected session 결측 → fail-fast
8. Crypto expected session 결측 → fail-fast
9. BTC 토요일 Relative에서 QQQ는 직전 완료 세션 사용
10. Relative + Lag는 BTC Signal Calendar 기준
11. Multiple reference + Lag도 Primary Signal Calendar 기준
12. Duplicate Entry Ignore
13. Zero Trades 정상 완료
14. Low Sample Warning
15. Corporate Action adjusted data
16. Snapshot 재실행 Determinism
