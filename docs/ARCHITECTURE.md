# ARCHITECTURE.md — refinvest-compute

## 1. System Boundary

```text
refinvest-web
      ↓
refinvest-core
      ↓ REST + polling
refinvest-compute
      ↓
Postgres / Object Storage
```

Compute는 Core가 계산을 위임하는 내부 서비스다.

Compute는 Core의 User, Subscription, Strategy Aggregate를 소유하지 않는다.
Core는 StrategyVersion 전체 payload를 Compute에 전달한다.

## 2. Recommended Repository Structure

```text
refinvest-compute/
├── app/
│   ├── main.py
│   ├── config/
│   └── security/
│
├── api/
│   ├── routes/
│   │   ├── backtests.py
│   │   └── assets.py
│   ├── schemas/
│   └── dependencies/
│
├── engine/
│   ├── domain/
│   ├── validation/
│   ├── temporal/
│   ├── conditions/
│   ├── execution/
│   ├── position/
│   ├── metrics/
│   ├── benchmark/
│   └── pipeline.py
│
├── marketdata/
│   ├── domain/
│   ├── ports/
│   └── adapters/
│
├── ingestion/
│   ├── vendors/
│   ├── calendars/
│   ├── corporate_actions/
│   └── snapshot/
│
├── persistence/
│   ├── postgres/
│   └── object_storage/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── regression/
│
├── docs/
└── openapi/
```

정확한 package 수는 실제 구현 규모에 맞게 조정한다. 디렉토리를 채우기 위해 abstraction을 만들지 않는다.

## 3. Dependency Direction

핵심 계산은 FastAPI, vendor SDK, DB 구현에 의존하지 않는다.

개념적 방향:

```text
api
 ↓
application / pipeline
 ↓
engine domain + ports
 ↑
marketdata/persistence adapters
```

금지:

```text
engine → FastAPI
engine → SQLAlchemy concrete repository
engine → vendor SDK
engine → HTTP client
engine → latest mutable dataset
```

Engine 계산 함수는 가능한 한 명시적인 입력을 받아 순수하게 계산한다.

현재 import 경계의 자동 검사, 정확한 심볼별 이행 예외와 제거 단계는
[`ARCHITECTURE_DEPENDENCIES.md`](ARCHITECTURE_DEPENDENCIES.md)에서 관리한다.

## 4. Engine Pipeline

Backtest pipeline은 개념적으로 다음 순서를 따른다.

```text
Request Contract Validation
        ↓
DSL Semantic Validation
        ↓
Dataset Snapshot Resolution
        ↓
Asset Availability / Calendar Validation
        ↓
Metric Evaluation
        ↓
Signal Evaluation
        ↓
Lag Resolution
        ↓
Execution Session Resolution
        ↓
Entry / Position / Exit
        ↓
Fee / Slippage
        ↓
Trades / Equity Curve
        ↓
Metrics / Benchmark
        ↓
Data Integrity Metadata
        ↓
Backtest Result
```

순서를 임의로 합쳐 계산 의미론을 숨기지 않는다.

## 5. Temporal Modules

`engine/temporal`은 다음 책임을 명시적으로 표현해야 한다.

- Referenced Asset session window
- Cross-calendar metric anchor
- Primary Signal Calendar
- Lag / Holding signal-session count
- Execution Asset next available session
- expected vs non-existent session 구분

Calendar resolution은 계산 곳곳에 산재시키지 않는다.

## 6. Market Data Boundary

Engine은 vendor를 모른다.

```text
Engine
  ↓
MarketData Port
  ↓
DatasetSnapshot-backed Adapter
```

Backtest 중 동일 Snapshot의 데이터만 읽는다.

`latest` lookup은 Backtest pipeline 시작 전에 snapshot resolution에서만 허용한다.

## 7. Ingestion

Ingestion의 책임:

```text
Vendor Raw Data
    ↓
Validation
    ↓
Calendar normalization
    ↓
Corporate Action normalization
    ↓
Canonical Daily Data
    ↓
Immutable DatasetSnapshot
```

기존 Snapshot을 overwrite하지 않는다.

벤더별 필드나 SDK 타입을 canonical market-data model 밖으로 노출하지 않는다.

## 8. Persistence Ownership

Compute가 소유:

- Asset reference data
- Calendar/reference metadata
- DatasetSnapshot metadata
- Snapshot-backed market data
- Compute job runtime state가 필요하다면 최소 범위

Core가 소유:

- Member/User
- Subscription
- Strategy / StrategyVersion
- BacktestRun의 제품 도메인 상태
- BacktestResult의 장기 영속화

공유 Postgres를 사용하더라도 테이블 소유권을 넘나들며 직접 수정하지 않는다.

## 9. API

`openapi/compute-api.yaml`이 Core↔Compute 계약의 정본이다.

주요 API:

```text
POST /backtests
GET  /backtests/{runId}

GET /assets
GET /assets/{symbol}/availability
GET /series
```

Compute API는 Core만 호출한다.

API transport schema와 Engine domain model을 동일 타입으로 강제하지 않는다. 변환이 실제 경계를 명확히 할 때 분리한다.

## 10. Backtest Execution State

API 계약상 Compute는 polling 가능한 실행 상태를 제공한다.

```text
PENDING
→ RUNNING
→ COMPLETED | FAILED
```

역방향 상태 전이는 허용하지 않는다.

Core의 제품 도메인 BacktestRun과 Compute 내부 실행 상태를 같은 persistence model로 공유하지 않는다.

## 11. Performance

초기 구현에서는 correctness-first.

성능 최적화 우선순위:

1. 알고리즘/데이터 접근 구조
2. vectorized computation
3. snapshot-local caching
4. profiling 기반 최적화

Point-in-Time Correctness를 희생하는 사전 계산이나 전체 미래 데이터 접근 패턴은 금지한다.

## 12. Testing Architecture

### Unit

- Metric 계산
- Temporal Rule
- Condition Evaluation
- Execution Price
- Position lifecycle
- Metrics

### Regression

Context의 DSL canonical cases를 고정 테스트로 유지한다.

특히:

- BTC 주말 → US Equity execution
- Relative cross-calendar anchor
- Lag 기준 calendar
- Missing expected session
- Duplicate entry
- Zero trades
- Low sample
- Snapshot determinism

### Integration

- Snapshot adapter
- Postgres/Object Storage
- FastAPI contract
- internal API key
