## 요약

- 고정된 `DatasetSnapshot`을 입력으로 받아 결정론적인 Engine pipeline을 실행하는 Daily Bar `BacktestJobExecutor`를 추가했습니다.
- FastAPI transport model에 의존하지 않고 canonical job payload를 복원하며, 문자열로 저장된 `Decimal` 값을 보존합니다.
- 요청 기간과 전략 관련 자산 전체의 snapshot 가용 구간 교집합을 이용해 실제 실행 기간을 결정합니다.
- 완료 결과는 `Decimal` 문자열 및 UTC ISO-8601 timestamp 형식으로 직렬화합니다.
- BTC 주말 signal이 다음 TQQQ 거래 세션에서 체결되는 end-to-end 회귀 테스트를 추가했습니다.

## 동작

- Executor는 `SnapshotExecutionDataLoader`를 통해서만 데이터를 받으며, vendor API 또는 mutable latest data를 조회하지 않습니다.
- Loader가 반환한 데이터의 snapshot ID는 worker가 claim한 고정 snapshot ID와 일치해야 합니다.
- Calendar 및 expected session data 오류는 terminal failure로 처리하고, 정의된 fatal error 범주로 매핑합니다.
- Expected session 데이터 결측은 fail-fast하며, 값을 채우거나 대체하지 않습니다.

## 검증

- `uv run pytest` — 141 passed, 2 skipped
- `uv run ruff check .` — 통과
- `uv run pyright` — 오류 없음

## 후속 작업

- Ingestion/object storage 기반 `SnapshotExecutionDataLoader` 구현
- 배포 환경에서 worker process 구성 및 실행
- Immutable snapshot artifact 영속화 이후 PostgreSQL 기반 end-to-end worker 실행 테스트 추가

## 결정론성 / Point-in-Time

- Determinism impact: changed — 하나의 고정 snapshot을 기반으로 전체 Engine pipeline 결과를 조립합니다.
- Point-in-Time impact: changed — signal, metric, execution scheduling이 모두 고정된 snapshot calendar와 market data만 사용합니다.
