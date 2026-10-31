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
│   └── config.py
│
├── api/
│   ├── routes/
│   │   ├── backtests.py
│   │   └── assets.py
│   ├── schemas/
│   ├── dependencies/
│   ├── exception_handlers.py
│   ├── http_metrics.py
│   └── security.py
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
│       └── cli.py
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

Package root는 `main()` console 진입점만 노출하며 호출 시 HTTP composition을 import한다.
계산·worker 모듈을 import할 때 HTTP app을 조립하지 않는다. ASGI 객체의 정식 경로는
`refinvest.app.main:app`이며 root의 이전 FastAPI `app` 별칭은 제거했다.
`refinvest.app`은 하위 package namespace로 유지한다. 호환 범위와 cold import·spawn 검증은
[Package 진입점 경계](PACKAGE_ENTRYPOINT_BOUNDARY.md)를 따른다.

Core 전용 HTTP 인증은 `api/security.py`가 소유한다. Route는 이 inbound dependency를
사용하고 `app.config`에서 읽은 설정만 전달받는다. 인증 모듈은 DB·application·route·provider
구현을 조립하지 않는다. `app/`에는 HTTP 인증의 이전 경로나 re-export를 남기지 않는다.
공개 health probe와 Trading Review의 JSON 중복 key 사전 검사·HTTP 오류 투영은 기존 경계를 유지한다.

전역 request validation·HTTP 예외의 응답 투영은 `api/exception_handlers.py`가 소유한다.
`app/main.py`는 framework 예외와 handler를 등록한다. 기존 contract schema mapper는
`api/schemas/errors.py`, route 고유의 오류 투영은 해당 route에 유지하며 새 catch-all 정책은 추가하지 않는다.

HTTP 관측 middleware와 bounded operation/status label 분류는 `api/http_metrics.py`가 소유한다.
App은 process-local registry를 만들고 listener lifespan과 middleware 등록을 조립한다.
요청 완료 시점의 enabled 설정은 app이 전달하는 콜백으로 읽으며 API 모듈은 app/config를 import하지 않는다.
Prometheus collector와 loopback listener 구현은 기존 `observability/metrics.py`에 유지한다.

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

배포 진단의 snapshot 선택·catalog coverage·최근 QQQ/BTCUSDT series 검사는
`application/reference_data_diagnostics.py`가 소유한다. 저장소 port와 지연 loader callback을 받고
한 번 선택한 snapshot만 검사한다. `app/reference_data_smoke.py`는 DB 설정 검증·구현체 조립과
CLI 입출력을 맡는다. 기존 7일 진단 창과 fail-fast를 보존하며 계산의 Temporal Rule을 변경하지 않는다.
상세 경계와 검증은 [Reference-data 진단 정리](REFERENCE_DATA_DIAGNOSTICS_BOUNDARY.md)를 따른다.

## 7. Ingestion

수동 snapshot CLI의 인자 해석·coverage/override 검증·ID 생성·발행 lifecycle은
`ingestion/snapshot/cli.py`가 소유한다. `app/snapshot_ingestion.py`는 Settings와 clock/nonce,
storage path·coordinator의 lazy factory를 연결하고 vendor·DB·storage 구현을 조립한다.
기존 Mvp coordinator의 artifact 발행→metadata 등록 순서는 유지한다.
경계와 보존 검증은 [Ingestion CLI 정리](INGESTION_CLI_BOUNDARY.md)에 기록한다.

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

Core↔Compute 계약의 정본은 sibling Context의 `openapi/compute-api.yaml`이다.
이 저장소의 같은 경로는 그 동기화 snapshot이며 독자적으로 변경하지 않는다.

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

Benchmark CLI는 `app/benchmark.py`에서 인자·출력·종료 코드와 executor/version 조립을 맡는다.
고정 fixture·scenario 로딩부터 warm 측정·correctness report 저장까지의 조율은
`benchmarking/runner.py`가 소유한다. Measurement·report adapter와 API transport DTO의 기존
경계는 유지한다. 상세 검증은 [Benchmark CLI 경계](BENCHMARK_CLI_BOUNDARY.md)에 기록한다.

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
