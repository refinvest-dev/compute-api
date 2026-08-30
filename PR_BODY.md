## Summary

- 별도 실행 가능한 `refinvest-backtest-worker` worker entrypoint를 추가했습니다.
- PostgreSQL job/snapshot repository, filesystem snapshot loader, Daily Bar executor, worker host를 조립했습니다.
- worker ID, engine version, lease, heartbeat, poll interval을 환경 변수로 설정할 수 있습니다.
- SIGINT/SIGTERM 수신 시 신규 claim을 중단하고 현재 lease가 recovery 가능하도록 graceful shutdown 합니다.
- PostgreSQL + manifest-backed snapshot 기반의 end-to-end worker integration test를 추가했습니다.

## Behavior

- worker는 worker 전용 process로 실행되며 API process와 분리됩니다.
- 실행 중에는 이미 claim 및 pin된 `DatasetSnapshot`만 사용합니다.
- lease heartbeat는 lease duration보다 짧아야 하며, 잘못된 runtime 설정은 시작 시 거부합니다.
- `REFINVEST_DATABASE_URL`이 없으면 worker는 시작하지 않습니다.

## Validation

- `uv run pytest` — 148 passed, 3 skipped
- `uv run ruff check .` — 통과
- `uv run pyright` — 오류 없음

## Follow-up

- 배포 환경에 worker process를 추가하고 `REFINVEST_DATABASE_URL` 및 worker 환경 변수를 설정
- Object storage provider adapter 구현 (filesystem adapter와 동일한 manifest contract 준수)
- CI에서 `REFINVEST_TEST_DATABASE_URL`을 설정해 PostgreSQL integration test 실행

## Determinism / Point-in-Time

- Determinism impact: none — worker는 기존의 pinned snapshot 및 engine version을 그대로 실행 경계에 연결합니다.
- Point-in-Time impact: none — worker는 immutable snapshot loader 외의 market data source를 사용하지 않습니다.
