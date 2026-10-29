# Analytics application 경계 — AR-03A

상태: AR-03A 완료. Application·provider·HTTP 연결과 로컬·실 DB 검증을 마쳤다.
검증 run: `20261029T045756Z` (UTC), 작업일 2026-10-29 KST.

## 1. 범위와 커밋·PR 경계

Analytics Job의 접수·status·grant 갱신·취소·descriptor 조회를
`application/analytics_jobs.py`에 둔다. Normalization의 상태·정책·DTO로 통합하지 않는다.
공용 payload·ACK 조율은 AR-04, worker 실행과 projection은 AR-05A에 남긴다.

하나의 기능 PR에서 다음 두 커밋 경계로 검토한다.

1. Application 소유 오류·port·use case와 정책 테스트. Adapter와 오류 consumer는 같은
   application class를 사용하며 SQL·worker 본문은 유지한다. 기존 HTTP 경로도 동작한다.
2. Dependency provider·다섯 HTTP 경로 연결·공용 전달 경로의 import/타입 참조 전환·legacy
   예외 제거와 관련 회귀·DB HTTP 검증·문서. 미연결 production 경로를 활성화하지 않는다.

선행 작업의 미커밋 변경과 PR 보조 파일을 보존했다. 완료 표시는 commit·push·배포 완료를 뜻하지 않는다.

## 2. 실제 경계

| 요소 | 책임 |
|---|---|
| AnalyticsJobRepository | create·get·update_grants·cancel·result_descriptor의 5개 capability |
| AnalyticsResultDeliveryRepository | 기존 payload_bytes·acknowledge capability |
| AnalyticsRepository | Provider가 조립하는 두 port의 합성; worker capability는 포함하지 않음 |
| submit_analytics_job | 입력 크기·identity·배포 digest/VersionSet·fingerprint와 atomic create 연결 |
| get_analytics_job_status | 기존 get 호출과 결과 반환 |
| get_analytics_result | Job 존재·COMPLETED 확인 후 descriptor 조회·만료 분류 |
| cancel_analytics_job | 명령 key 해싱·취소 후 status 조회, 단계별 오류 범위 보존 |
| update_analytics_grants | key 해싱과 기존 grant CAS 호출 |
| api/dependencies/trading_analytics_jobs.py | 기존 Settings·key·Postgres adapter 조립 및 optional/required provider |

Application에는 FastAPI·설정 객체·PostgreSQL import가 없다. API는 기존 Pydantic wire 검증·
중복 JSON key 거절·인증·헤더와 HTTP 응답을 소유한다. Application은 검증된 JSON projection
하나와 설정의 primitive 값만 받는다. 동일 payload를 새 대형 DTO나 wrapper로 복사하지 않는다.
비HTTP caller는 동일한 wire 타입·필수 필드 검증을 보장해야 하며 임의 dict를 지원하는 API가 아니다.

기존 AnalyticsJobConflict·AnalyticsJobCapacity·AnalyticsJobUnavailable를 application으로
이동했다. Adapter·worker·HTTP·테스트가 같은 class identity를 사용한다. 별도 wrapper adapter나
동일 이름의 이중 오류 class를 만들지 않았다. DB는 JSON·bytes·safe code를 저장하며 Python
exception module 경로를 durable identity로 사용하지 않는다.

공용 전달 handler도 새 provider와 전달 port를 직접 사용한다. 그 결과 두 Job route의
concrete repository·route 간 provider import를 AR-04보다 먼저 제거했다. 공유 handler의
업무 본문은 그대로이며, 어느 기능의 payload/ACK를 선택할지는 아직 HTTP에 남아 있다.

## 3. 보존한 정책과 오류 범위

```text
유효 Idempotency-Key
→ 두 artifact의 byteSize 상한
→ input/config/artifact identity
→ 배포 implementationDigest 존재·exact 지원 VersionSet
→ fingerprint·key hash → 기존 atomic create
```

크기 초과는 잘못된 identity보다 먼저 기존 retryable 503을 반환한다. Identity 오류는
누락된 배포 digest보다 먼저 422를 반환한다. 신규 create는 202이며 replay는 기존 Job의
현재 상태를 200으로 반환한다. Terminal replay를 PENDING으로 바꾸지 않는다.
Grant expiry를 application에서 선검사하지 않는다. Replay·capacity·fresh grant 검사의
순서는 DB transaction에 유지하며, 만료된 원래 grant의 동일 replay를 먼저 거절하지 않는다.

Status의 기존 get은 retry cutoff 만료를 DB clock으로 반영할 수 있다. 이를 순수 조회로
가정하거나 호출을 생략하지 않는다. 취소의 bool 반환값으로 새 정책을 만들지 않고 기존대로
후속 status를 조회한다. 취소 명령의 unavailable/conflict만 404/409로 분류하고 후속 조회
실패를 같은 범주로 감싸지 않는다. Descriptor 단계의 unavailable만 만료 404로 변환하며
그 앞 status 조회 장애를 만료로 오인하지 않는다.

| 결과·오류 | 기존 HTTP 표현 |
|---|---|
| AnalyticsContractError | 422, 원래 safe code |
| 입력 상한·digest 미설정 | 503 TEMPORARY_DEPENDENCY_FAILURE·retryable=true |
| AnalyticsJobCapacity | 503 TRADING_REVIEW_CAPACITY_EXCEEDED·Retry-After |
| create·grant conflict | 409, 원래 safe code |
| Job 미존재·취소 대상 없음 | 404 JOB_NOT_FOUND |
| 미완료·descriptor 없음 | 409 RESULT_NOT_AVAILABLE |
| descriptor unavailable | 404 RESULT_PAYLOAD_EXPIRED |
| 취소 명령 conflict | 409 JOB_STATE_CONFLICT |

기존에 매핑하지 않던 일반 DB/조회 장애를 새 retryable 오류나 404로 바꾸지 않는다.
Provider의 optional None과 required 503 동작도 같다.
Adapter의 class/함수 본문은 오류 class 소유권 외에 AST가 동일하다. SQL·advisory lock·공유
capacity·CAS·grant 암호화·lease·발행·ACK·cleanup·DB clock·transaction scope를 변경하지 않았다.
Worker 정의와 공용 전달·Normalization HTTP handler 본문도 동일하다.
API schema·OpenAPI·migration·fixture·계약 hash·Metric 계산·Temporal/Execution 정책은 그대로다.

정본은 Context ADR-064/069/070과 [Job protocol](../../../context/docs/TRADING_ANALYTICS_JOB_PROTOCOL.md),
[input identity](../../../context/docs/TRADING_ANALYTICS_INPUT_IDENTITY.md),
[result integrity](../../../context/docs/TRADING_ANALYTICS_RESULT_INTEGRITY.md),
[delivery](../../../context/docs/TRADING_ANALYTICS_RESULT_DELIVERY.md)다.

## 4. 검증 결과와 한계

- 고정 ID·시각의 handler 반환값 20개와 Create·Grant 요청 schema: 전후 동일.
  실행마다 바뀌는 오류 traceId는 제외했다. 실제 HTTP status는 아래 API 회귀와 DB smoke로 확인했다.
- 기존 의존성·Analytics·Normalization 관련 회귀: 71 passed.
- 새 application 정책 테스트: 23 passed.
- 전체 로컬 suite: 546 passed / 118 skipped. Skip은 DB 31개와 폐기된 PoC 87개다.
- 실 DB: Normalization 20개·Analytics 기존 4개·새 HTTP/worker smoke 1개, 25 passed / 0 skipped.
- Formatter 231개 파일·Ruff lint·Pyright strict: PASS.
- 구현 전·후 contract-sync `-IncludeWeb`: PASS.

DB smoke는 새 격리 schema에 migration 0001~0017을 적용했다. 실제 provider·application·
Postgres adapter를 사용하고, 두 합성 artifact의 download만 고정했다. ASGI HTTP로 접수
202·replay 200·grant 갱신·worker claim/계산/발행·descriptor·exact payload 길이/SHA-256·
완료 replay·ACK replay·bytes 제거·별도 Job 취소를 검증한다. 외부 network나 production
fixture에 의존하지 않는다. HTTP socket 서버나 Core dispatch·Core publication E2E는 아니다.

Analytics 실제 Core dispatch·publication·ACK harness는 현재 없으며 실행하지 않았다.
이 공백은 기존 AR-00/02A와 같이 Integration 후속 작업·AR-10 운영 완료 gate로 유지한다.
새 timeout·retry·배포 정책, image 배포·지원 버전 등록도 수행하지 않는다.

의존성 예외는 14개에서 6개로 감소했다. 남은 2개 파일·6개 group·6개 심볼은 Asset/Series
조회의 AR-07 대상이다. [registry](../tests/architecture_legacy_imports.json)와
[의존성 문서](ARCHITECTURE_DEPENDENCIES.md)에서 관리한다.
정적 예외 제거만으로 AR-04의 공용 전달 use case 분리가 끝난 것은 아니다.
Worker application 전환은 AR-05A, root eager app bootstrap은 AR-11에 남는다.

로컬 증적은 Integration의
[AR-03A manifest](../../../integration/.integration-data/architecture-refactoring/ar03a/20261029T045756Z/manifest.json)에
별도 보관하며 Compute에 커밋하지 않는다.
Determinism impact: none. Point-in-Time impact: none.
