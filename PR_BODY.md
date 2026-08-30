## Summary

- immutable DatasetSnapshot artifact를 생성하는 filesystem publisher를 추가했습니다.
- canonical calendar를 `calendar.json`, Daily Bar를 `daily-bars.ndjson`, integrity metadata를 `manifest.json`으로 직렬화합니다.
- artifact content의 SHA-256 hash를 manifest에 기록합니다.
- 기존 `storage_path`의 overwrite를 거부해 Snapshot immutability를 보장합니다.
- writer가 생성한 artifact를 기존 loader가 재읽는 round-trip 회귀 테스트를 추가했습니다.

## Behavior

- publisher는 calendar provenance가 DatasetSnapshot metadata와 정확히 일치하는지 검증합니다.
- calendar와 Daily Bar는 안정된 asset/session 순서로 직렬화되어, 동일 입력은 동일 artifact bytes를 생성합니다.
- manifest는 artifact가 모두 준비된 뒤 마지막에 publish되어 loader의 진입점 역할을 합니다.
- 불완전하거나 기존에 존재하는 storage path를 자동으로 수정·덮어쓰지 않습니다.

## Validation

- `uv run pytest` — 152 passed, 3 skipped
- `uv run ruff check .` — 통과
- `uv run pyright` — 오류 없음

## Follow-up

- Object storage provider adapter 구현 (filesystem adapter와 동일한 manifest contract 준수)
- publisher와 DatasetSnapshot metadata insert를 하나의 ingestion workflow로 연결
- CI에서 `REFINVEST_TEST_DATABASE_URL`을 설정해 PostgreSQL integration test 실행

## Determinism / Point-in-Time

- Determinism impact: changed — canonical input을 안정된 순서와 형식으로 immutable artifact로 publish합니다.
- Point-in-Time impact: none — publisher는 ingestion이 제공한 canonical calendar completion time과 Daily Bar를 변형하지 않습니다.
