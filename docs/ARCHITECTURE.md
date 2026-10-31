# ARCHITECTURE.md — refinvest-compute

2026-11-01 KST, Compute `0a48403` 기준의 현재 구조다. 제품 정책·API 계약의 정본은 Context이며,
구조 개선 완료 범위와 남은 운영 gate는 [최종 점검](ARCHITECTURE_FINAL_AUDIT.md)에 기록한다.

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

Compute는 Core가 계산을 위임하는 내부 서비스다. Backtest, Trading Review Normalization,
Behavior Analytics와 immutable 시장 참조 데이터의 수집·조회를 담당한다.

Compute는 Core의 User, Subscription, Strategy Aggregate를 소유하지 않는다.
Core는 StrategyVersion 전체 payload를 Compute에 전달한다.

## 2. Current Repository Structure

```text
src/refinvest/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── worker.py
│   ├── trading_review_worker.py
│   ├── trading_analytics_worker.py
│   └── ... CLI·storage 조립
│
├── api/
│   ├── routes/
│   │   ├── backtests.py
│   │   ├── assets.py
│   │   ├── series.py
│   │   ├── trading_review_jobs.py
│   │   └── trading_analytics_jobs.py
│   ├── schemas/
│   ├── dependencies/
│   ├── exception_handlers.py
│   ├── http_metrics.py
│   └── security.py
│
├── engine/
│   ├── validation/
│   ├── temporal/
│   ├── conditions/
│   ├── signals/
│   ├── entries/
│   ├── execution/
│   ├── position/
│   ├── metrics/
│   ├── benchmark.py
│   ├── performance.py
│   ├── results.py
│   └── trades.py
│
├── trading_review/
│   ├── service.py
│   └── analytics/
│
├── contracts/
│   ├── normalization/
│   └── analytics/
│
├── application/
│   └── ... use case·port·worker 조율·입력 검증·결과 projection
│
├── adapters/
│   ├── backtest/
│   ├── normalization/
│   └── analytics/
│
├── marketdata/
│   ├── domain.py
│   ├── ports.py
│   └── adapters/
│
├── ingestion/
│   ├── vendors/
│   ├── calendars/
│   └── snapshot/
│       └── cli.py
│
├── persistence/
│   ├── postgres/
│   └── object_storage/
│
├── benchmarking/
└── observability/
```

루트의 `tests/`는 단위·계약·회귀 파일과 `integration/`, `fixtures/`를 포함한다.
`tools/`는 개발용 합성 workload·자원 실측, `deploy/systemd/`는 실행 unit·환경 예시를 소유한다.
`docs/`와 `openapi/`는 구현 설명과 Context 계약 snapshot이다. Sibling `../../integration`은
source root·Python runtime dependency가 아니다. 디렉터리를 채우기 위해 추상화를 추가하지 않는다.

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

DDD는 `AssetSession`·`DailyBar` 같은 값, Snapshot·Revision·VersionSet identity와 기능별
불변식·상태 전이에 적용한다. Core의 제품 Aggregate를 복제하거나 모든 기능을 공통 Job
Aggregate·generic repository에 맞추지 않는다. 같은 트랜잭션에서 보장할 Job 상태·terminal
descriptor·암호화 payload의 일관성은 해당 기능 repository가 소유한다.

`contracts/`는 versioned identity·canonicalization·integrity·전달 정책,
`application/`은 port 기반 조율과 검증된 입력·결과 projection을 담당한다.
HTTP·artifact fetch·프로세스·DB 구현은 바깥 adapter다. Backtest와 Normalization의
process adapter, Analytics의 현재 부모 내 실행을 같은 격리 수준으로 표현하지 않는다.

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
실제 단계 조율은 `application/backtest_executor.py`의 `DailyBarBacktestExecutor`가 맡고,
각 계산은 `engine/`의 순수 함수로 수행한다. 별도 `engine/pipeline.py` 파일은 없다.

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
- 기능별 durable job·attempt·lease·멱등성·grant·ACK 기록
- retention 내 terminal result/descriptor와 Trading Review의 암호화 payload bytes

Core가 소유:

- Member/User
- Subscription
- Strategy / StrategyVersion
- BacktestRun의 제품 도메인 상태
- BacktestResult의 장기 영속화

공유 Postgres를 사용하더라도 테이블 소유권을 넘나들며 직접 수정하지 않는다.
Compute의 제한 기간 runtime 결과와 Core의 장기 제품 결과는 다른 소유권이다.
Worker의 조건부 발행과 payload/descriptor 저장 원자성은 해당 repository가 유지한다.
Core artifact 접근은 짧은 grant의 HTTP fetch로 수행하며 Core DB를 조회하지 않는다.

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

POST /trading-review/normalization-jobs
GET  /trading-review/normalization-jobs/{jobId}
POST /trading-review/analytics-jobs
GET  /trading-review/analytics-jobs/{jobId}
```

Compute API는 Core만 호출한다.
Trading Review에는 기능별 result descriptor·grant update·cancel과 공용 terminal payload·ACK
operation도 있다. 전체 method/path·status·hash 버전은 OpenAPI 0.6.0을 따른다. Core는 bytes와
integrity를 검증하고 장기 결과를 commit한 후 ACK한다. Compute의 ACK 이후 bytes 정리와
terminal retention은 장기 결과 소유권의 이전을 대신하지 않는다.

API transport schema와 Engine domain model을 동일 타입으로 강제하지 않는다. 변환이 실제 경계를 명확히 할 때 분리한다.

## 10. Job Execution State

Backtest는 polling 가능한 다음 실행 상태를 제공한다.

```text
PENDING
→ RUNNING
→ COMPLETED | FAILED
```

역방향 상태 전이는 허용하지 않는다.

Core의 제품 도메인 BacktestRun과 Compute 내부 실행 상태를 같은 persistence model로 공유하지 않는다.
Normalization·Analytics는 PENDING/RUNNING에서 COMPLETED/FAILED/CANCELLED로 전이한다.
Grant 갱신·Core 지시 retry·attempt/token/deletion generation·lease 소유권은 각 기능의
application/repository와 계약을 따른다. Terminal 결과의 재실행은 허용하지 않는다.
Backtest의 자동 retry와 Analytics의 Core 지시 attempt 재시도를 공통 정책으로 합치지 않는다.

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

## 13. Processes and Operations

| 프로세스·명령 | 조립 | 실제 조율·구현 | 실행 특성 |
|---|---|---|---|
| HTTP API: refinvest | app.main·api.dependencies | api의 handler·application·repository | 수락·조회·전달, Engine 계산을 HTTP 안에서 실행하지 않음 |
| Backtest worker | app.worker | application.backtest_worker_host·adapters.backtest | 부모 lease/상태 관리, child 계산·deadline |
| Normalization worker | app.trading_review_worker | application.normalization_worker·adapters.normalization | 부모 상태·발행, child 입력 fetch·계산 |
| Analytics worker | app.trading_analytics_worker | application.analytics_worker·adapters.analytics | 현재 부모에서 fetch·검증·계산·projection, 별도 heartbeat thread |
| Snapshot 발행 | app.snapshot_ingestion | ingestion.snapshot.cli·mvp·workflow | vendor/calendar 생성·검증, artifact 후 metadata 발행 |
| Cleanup 명령 3개 | 각 app cleanup 모듈 | 기능별 PostgreSQL repository | 단일 bounded 호출, DB clock·ACK/retention 정책 유지 |
| Benchmark·reference 진단 | app.benchmark·reference_data_smoke | benchmarking.runner·application.reference_data_diagnostics | 수동 측정·읽기 검증 |

HTTP API·worker 3개는 별도 프로세스다. Snapshot 발행 timer와 cleanup timer 3개는 별도
단발 service를 기동한다. 이 저장소의 systemd template 존재와 실제 배포·활성화 여부는 다르다.
Listener·thread·metrics registry와 worker 수를 하나의 전역 singleton처럼 공유하지 않는다.

운영 정본은 [HTTP runbook](COMPUTE_API.md), [Backtest](BACKTEST_WORKER.md),
[Normalization](TRADING_REVIEW_JOB_RUNTIME.md), [Analytics](TRADING_ANALYTICS_RUNTIME.md),
[Snapshot](SNAPSHOT_INGESTION.md), [관측](OBSERVABILITY.md)이다.

`app/integration_fixture.py`는 비운영 고정 5자산 데이터의 생성·발행·재사용 검증 도구로 유지한다.
이 도구의 고정 가격·시각은 실제 시장 의미론이나 production ingestion의 대체값이 아니다.
구현 자체는 입력 DB가 integration 전용인지 강제하지 않으므로 운영 credential로 실행하지 않는다.
격리 DB·schema·fixture·실행 증적 관리는 sibling integration의 정본 절차를 따른다.
이 명시적인 도구 범위를 다른 app 모듈에 계산·업무 조율을 추가하는 허용으로 확대하지 않는다.

## 14. Completion and Remaining Gates

AR-00~07과 AR-11의 기본 구조 개선을 완료했다. API·계산·Job 원자성 보존의 검증은 단계별
기록과 [최종 점검](ARCHITECTURE_FINAL_AUDIT.md)에 연결한다. 이 문서 수정은 runtime 변경이나
새 전체 테스트·실 DB·E2E 수행을 뜻하지 않는다.

ADR-071은 DRAFT다. Analytics attempt 전체 deadline·강제 종료·OS 메모리 격리·부모 발행 예산과
실제 Core Analytics runtime E2E는 AR-08~10의 별도 미완료 gate다. 현재 입력/output bytes cap과
heartbeat를 실행 시간·RSS 상한으로 해석하지 않는다. [정책 검토](ANALYTICS_EXECUTION_POLICY_REVIEW.md)와
[Linux 실측](ANALYTICS_RESOURCE_LINUX.md)을 참고한다.

전역 cache·전체 feature package 이동·Temporal 최적화는 별도 후보다. Snapshot metadata의
Python 하위 dict까지 깊은 불변성이 강제되는 것은 아니므로 공유 cache를 도입할 때는 이를 별도로
검증해야 한다. 현재 call-local 재사용 검증을 전역 cache 안전성의 증거로 사용하지 않는다.
