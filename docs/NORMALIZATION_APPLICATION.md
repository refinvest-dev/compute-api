# Normalization application 경계 — AR-03N

상태: AR-03N 완료. Application·HTTP 연결과 로컬·실 DB·고정 Core E2E 검증을 마쳤다.
검증 run: `20261029T035454Z` (UTC), 작업일 2026-10-29 KST.

## 1. 범위와 검토 순서

기존 Normalization DTO·오류·atomic repository 계약을 유지하면서 HTTP handler의 수동
admission 정책과 Job 명령·조회 조율을 application으로 옮긴다. Analytics use case는 AR-03A,
공용 terminal payload·ACK의 기능 선택과 조율은 AR-04에서 수행한다.

이 기능은 하나의 PR 범위로 검토하며 다음 두 커밋 경계를 사용한다.

1. Application command·5개 capability port·use case·정책 테스트. 기존 HTTP 경로는 그대로
   동작할 수 있는 additive 단계이며, 새 use case는 아직 연결하지 않는다.
2. Dependency provider·HTTP 연결·기존 override 전환·legacy 예외 제거와 관련 검증·문서.
   다섯 경로를 모두 연결하고 전후 응답·실 DB·Core 경로를 검증한다.

선행 AR-00/01/02N/02A의 작업 트리 변경은 보존한다. 위 내용은 검토·커밋 분할안이며,
이 문서의 구현 완료가 commit·push·배포 완료를 뜻하지 않는다.

## 2. 실제 의존 방향

```text
api/routes/trading_review_jobs.py
    → application/normalization_jobs.py
    → application/trading_review_jobs.py의 NormalizationJobRepository
    ← persistence/postgres/trading_review_jobs.py

api/dependencies/trading_review_jobs.py
    → Settings·payload key·Postgres adapter 조립
```

| Application 진입점 | 보유하는 조율 |
|---|---|
| submit_normalization_job | VersionSet·artifact·timezone·instrument terms·input hash와 기존 atomic admission 연결 |
| get_normalization_job_status | Job 조회와 persisted identity 오류 분류 |
| update_normalization_grants | parsed URI 사실 검증, idempotency key 해싱, 기존 grant CAS 호출 |
| cancel_normalization_job | key 해싱과 기존 cancellation 호출 |
| get_normalization_result | Job 존재·COMPLETED 확인 후 descriptor 조회 |

NormalizationJobRepository는 이 다섯 capability만 노출한다. 기존 TradingReviewJobRepository는
이를 상속하고 payload·ACK capability를 유지한다. Worker port는 변경하지 않았다.
HTTP Job handler의 타입은 좁은 port이며 PostgreSQL class를 import하지 않는다.
Provider는 기존 설정·key·생성자 인자를 그대로 사용한다. 공용 전달 경로도 같은 provider를
사용하므로 Normalization concrete import는 AR-04보다 먼저 제거할 수 있었다.
기존 dependency override는 동일한 provider 함수 identity로 유지된다.

Application command는 API transport class를 사용하지 않는다. 검증된 identity 계약 객체,
artifact·instrument의 작은 불변 projection과 기존 durable JSON projection 하나를 받는다.
JSON payload 전체를 새 모델로 복제하지 않는다. URI 구문과 디코딩은 inbound adapter가
담당하고 credentials/fragment 존재 여부만 application에 전달한다. Application은 URI를
다시 해석하거나 URL·credential을 오류에 포함하지 않는다.

API에는 인증·중복 JSON key·헤더·Pydantic wire schema 및 기존 cross-field 일관성 검사,
result version 필수/지원 쌍의 HTTP precheck와 응답·metric 변환을 유지한다. 이 검사는
application command의 전제다. 새로운 비HTTP caller를 도입한다면 같은 wire 계약의
일관성을 보장해야 하며, 임의 dict를 검증된 command로 간주하지 않는다.

## 3. 보존한 순서와 오류

생성의 수동 정책 순서는 기존과 같다.

```text
유효 Idempotency-Key
→ V1 hash인 경우 exact VersionSet/terms registry
→ artifact role·서로 다른 ID·URI
→ timezone → instrument terms·settlement·grid
→ V1 input hash 계산 → 기존 atomic admission
```

미지원 hash specification은 기존대로 computed hash를 None으로 접수하고 worker에서
거절한다. 이 구조 변경에서 조기 거절 정책을 추가하지 않는다. Expected input hash를
계산값으로 덮어쓰지 않는다. Grant expiry를 application에서 선검사하지 않으며,
replay·capacity·fresh grant의 우선순위는 기존 DB transaction이 소유한다.

| Application 결과·오류 | 기존 HTTP 표현 |
|---|---|
| NormalizationRequestInvalid | 422, 기존 safe code·message |
| TradingReviewIdempotencyConflict | 생성 409 IDEMPOTENCY_PAYLOAD_CONFLICT, 명령별 기존 409 code |
| TradingReviewCapacityExceeded | 503 TRADING_REVIEW_CAPACITY_EXCEEDED·Retry-After·retryable=true |
| TradingReviewGrantInvalid | 422 ARTIFACT_REFERENCE_EXPIRED |
| NormalizationJobIdentityUnavailable | 409 VERSION_INCOMPATIBLE |
| 미존재 Job | 404 JOB_NOT_FOUND |
| 미완료·descriptor 없음 | 409 RESULT_NOT_AVAILABLE |

DB 고유 UniqueViolation을 application의 idempotency conflict로 변환하던 adapter 경계를
그대로 사용한다. 일반 psycopg 오류를 새 retryable 오류로 감싸는 정책은 추가하지 않는다.
Provider 설정 오류의 기존 503 응답도 유지한다.

Persistence source bytes는 작업 전과 같다. SQL·lock·DB clock·공유 capacity·CAS·lease·attempt,
grant 갱신·취소·발행·ACK·cleanup의 transaction scope를 변경하지 않는다.
Status/descriptor의 두 query를 새로운 transaction으로 묶거나 result 조회 실패를 자동 재시도하지 않는다.
공용 payload·ACK와 HTTP raw JSON/version precheck·status serializer·metric 함수의 본문 AST도
같다. API schema·OpenAPI·migration·fixture·계산·계약 모듈은 변경하지 않았다.

정본은 Context ADR-063/064/065/067/070,
[Normalization identity](../../../context/docs/TRADING_NORMALIZATION_IDENTITY.md),
[version pin](../../../context/docs/TRADING_VERSIONING.md),
[전달 정책](../../../context/docs/TRADING_NORMALIZATION_RESULT_DELIVERY.md)이며,
실제 runtime은 [기존 운영 문서](TRADING_REVIEW_JOB_RUNTIME.md)를 따른다.

## 4. 검증과 남은 경계

- 고정 ID·시각으로 직접 호출한 생성·replay·status·grant·취소·오류 20개 반환 필드: 전후 동일.
  실행마다 생성되는 오류 traceId는 비교에서 제외하며 실제 HTTP status는 기존 route 회귀와 E2E로 검증했다.
- Create·Grant 요청 JSON schema: 전후 동일.
- Application 정책 테스트 19개: 우선순위·호출 차단·pin/hash·키 해싱·descriptor 순서 확인.
- 기존 의존성·HTTP·Job·identity·Analytics 관련 테스트 72개: 통과.
- 격리 PostgreSQL Normalization 20개·Analytics 4개: 24 passed / 0 skipped.

- 전체 로컬 suite 재실행: 523 passed / 117 skipped.
- Formatter 228개 파일·Ruff lint·Pyright strict: PASS.
- 구현 전·후 contract-sync `-IncludeWeb`: PASS.
- 고정 Core E2E: 정상 계산·발행·ACK, V1 pin, ORCH-024~028 모두 PASS.

117 skip은 별도 DB 검증 대상 30개와 폐기된 test-only PoC 87개다. 이번 직접 영향 범위인
Normalization·Analytics 24개를 실제 DB로 실행했으며, 미변경 Backtest DB 6개는 재실행하지 않았다.
최초 전체 로컬 실행은 Windows socketpair WinError 10014와 AST recursion SystemError로
2 failed / 521 passed였다. 코드 변경 없이 새 process에서 전체 통과했고 실패 로그도 보존했다.
Core는 이전 비교와 같은 고정 commit 206473c를 사용하며 현재 원본 Core HEAD와의 호환성을
검증한 것으로 해석하지 않는다. V2 Core publication은 harness 범위 밖이다.
ORCH-027은 기존 autocommit migration 경계를 검증했다. Atomic runner의 SQLSTATE 55006
진단은 기존 배포 제약이며 이번 변경으로 해소하지 않는다. 새 image 배포·지원 버전 등록도 수행하지 않는다.

남은 import 예외는 4개 파일, 9개 group, 14개 심볼이다.
[registry](../tests/architecture_legacy_imports.json)와
[의존성 문서](ARCHITECTURE_DEPENDENCIES.md)에서 관리한다.
이 route의 Analytics concrete import·route 간 provider import와 공용 payload·ACK 조율은
AR-04에 남는다. Worker application 전환은 AR-05N이다. Root eager app import는 AR-11이다.

증적은 Integration의
[AR-03N manifest](../../../integration/.integration-data/architecture-refactoring/ar03n/20261029T035454Z/manifest.json)와
[고정 Core E2E](../../../integration/.integration-data/trading-normalization/bb951de56c6342bfa9407a082c059e07/evidence.json)에
보관하며 Compute에 커밋하지 않는다.
Determinism impact: none. Point-in-Time impact: none.
