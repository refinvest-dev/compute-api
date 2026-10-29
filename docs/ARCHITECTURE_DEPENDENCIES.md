# 의존성 규칙과 단계별 이행

현재 구현은 AR03N까지이며 legacy import 예외 14개를 추적한다.
정책·계약의 정본은 Context다. 검사 구현은
[test_architecture_dependencies.py](../tests/test_architecture_dependencies.py), 예외의 정본은
[architecture_legacy_imports.json](../tests/architecture_legacy_imports.json)이다.

Engine과 Trading Review 계산 코어는 HTTP·DB·SDK·설정에 의존하지 않는다.
Application은 port와 순수 계약·모델에 의존한다. app/API provider가 concrete adapter를 조립한다.
Contracts는 기능·버전별 DTO·identity·integrity를, adapter는 Artifact·process·storage I/O를 소유한다.
앞으로 생성할 package도 synthetic source로 경계 규칙을 검사한다.
Application 전체의 multiprocessing·subprocess 금지는 Backtest executor를 이동하는 AR-05B에서 적용한다.

예외는 정확한 파일·import 심볼·제거 단계·이유를 명시한다. glob·wildcard·중복·새 심볼·stale 예외는 실패한다.
예외를 새로 추가해 후속 변경의 검사를 통과시키지 않는다.

| 파일 | import 심볼 | 제거 단계 |
|---|---|---|
| `refinvest/api/routes/assets.py` | `refinvest.app.snapshot_storage.create_snapshot_execution_data_loader` | AR-07 |
| `refinvest/api/routes/assets.py` | `refinvest.persistence.object_storage.s3_snapshot_artifacts.SnapshotStorageExecutionDataLoader` | AR-07 |
| `refinvest/api/routes/assets.py` | `refinvest.persistence.postgres.dataset_snapshots.PostgresDatasetSnapshotRepository` | AR-07 |
| `refinvest/api/routes/series.py` | `refinvest.app.snapshot_storage.create_snapshot_execution_data_loader` | AR-07 |
| `refinvest/api/routes/series.py` | `refinvest.persistence.object_storage.snapshot_artifacts.SnapshotArtifactError` | AR-07 |
| `refinvest/api/routes/series.py` | `refinvest.persistence.postgres.dataset_snapshots.PostgresDatasetSnapshotRepository` | AR-07 |
| `refinvest/api/routes/trading_analytics_jobs.py` | `refinvest.persistence.postgres.trading_analytics_jobs.AnalyticsJobCapacity` | AR-03A |
| `refinvest/api/routes/trading_analytics_jobs.py` | `refinvest.persistence.postgres.trading_analytics_jobs.AnalyticsJobConflict` | AR-03A |
| `refinvest/api/routes/trading_analytics_jobs.py` | `refinvest.persistence.postgres.trading_analytics_jobs.AnalyticsJobUnavailable` | AR-03A |
| `refinvest/api/routes/trading_analytics_jobs.py` | `refinvest.persistence.postgres.trading_analytics_jobs.PostgresAnalyticsJobRepository` | AR-03A |
| `refinvest/api/routes/trading_review_jobs.py` | `refinvest.api.routes.trading_analytics_jobs.get_optional_analytics_repository` | AR-03A |
| `refinvest/api/routes/trading_review_jobs.py` | `refinvest.persistence.postgres.trading_analytics_jobs.AnalyticsJobConflict` | AR-03A |
| `refinvest/api/routes/trading_review_jobs.py` | `refinvest.persistence.postgres.trading_analytics_jobs.AnalyticsJobUnavailable` | AR-03A |
| `refinvest/api/routes/trading_review_jobs.py` | `refinvest.persistence.postgres.trading_analytics_jobs.PostgresAnalyticsJobRepository` | AR-03A |

진행 범위와 후속 연결은 [작업 계획](ARCHITECTURE_REFACTORING_PLAN.md)을 참고한다.
