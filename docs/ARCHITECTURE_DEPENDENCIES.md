# 의존성 규칙과 단계별 이행

정책·계약의 정본은 Context다. 실행 검사는
[test_architecture_dependencies.py](../tests/test_architecture_dependencies.py), 현재 예외의 정본은
[architecture_legacy_imports.json](../tests/architecture_legacy_imports.json)이다.

Engine과 Trading Review 계산 코어는 HTTP·DB·SDK·설정에 의존하지 않는다.
Application은 port와 순수 계약·모델에 의존하고, app/API provider가 concrete adapter를 조립한다.
Contracts는 승인된 기능별 DTO·identity·integrity를 소유한다. Artifact·process·storage I/O는 adapter에 둔다.
새 경계를 위한 synthetic import 검사는 해당 package가 생성되기 전부터 적용한다.
Application 전체의 multiprocessing·subprocess 금지는 기존 Backtest executor를 이동하는 AR-05B에서 적용한다.

예외는 정확한 파일·import 심볼·제거 단계·이유를 명시한다. glob·wildcard·중복·새 심볼·stale 예외는 실패한다.
후속 단계에서 예외를 새로 추가해 검사를 통과시키지 않는다.

현재 구현: AR05A까지. 남은 legacy import는 6개다.

| 파일 | 심볼 | 제거 단계 |
|---|---|---|
| `refinvest/api/routes/assets.py` | `refinvest.app.snapshot_storage.create_snapshot_execution_data_loader` | AR-07 |
| `refinvest/api/routes/assets.py` | `refinvest.persistence.object_storage.s3_snapshot_artifacts.SnapshotStorageExecutionDataLoader` | AR-07 |
| `refinvest/api/routes/assets.py` | `refinvest.persistence.postgres.dataset_snapshots.PostgresDatasetSnapshotRepository` | AR-07 |
| `refinvest/api/routes/series.py` | `refinvest.app.snapshot_storage.create_snapshot_execution_data_loader` | AR-07 |
| `refinvest/api/routes/series.py` | `refinvest.persistence.object_storage.snapshot_artifacts.SnapshotArtifactError` | AR-07 |
| `refinvest/api/routes/series.py` | `refinvest.persistence.postgres.dataset_snapshots.PostgresDatasetSnapshotRepository` | AR-07 |

완료 단계와 후속 연결은 [작업 계획](ARCHITECTURE_REFACTORING_PLAN.md)을 참고한다.
