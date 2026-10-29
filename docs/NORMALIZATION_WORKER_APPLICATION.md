# Normalization worker·실행 adapter — AR-05N

상태: AR-05N 완료. 구현·로컬·실 DB·고정 Core publication 검증을 마쳤다.
검증 run: `20261029T082404Z` (UTC), 작업일 2026-10-29 KST.

## 1. 범위와 검토·커밋 경계

Normalization만 분리한다. Analytics worker·Backtest executor·새 timeout 정책·범용 Job
framework는 이 범위에 포함하지 않는다. 한 기능 PR에서 다음 커밋 경계로 검토한다.

1. 실행 outcome·좁은 설정·repository/executor/metrics port, worker application·projection과
   관련 정책 테스트. 기존 entrypoint를 유지할 수 있는 additive 준비 단계다.
2. Artifact HTTP·spawn/IPC adapter와 검증된 입력 변환·계산·실패 분류 연결, fixed comparison·
   실제 spawn 테스트. 준비 경로를 production에 부분 활성화하지 않는다.
3. 기존 CLI 조립 연결, 이전 mixed module과 projection 경로 제거·consumer 전환, 실제 DB·
   Core 검증과 문서. 부모·자식은 같은 artifact의 canonical module을 사용한다.

선행 변경·PR 보조 파일은 보존한다. 완료 표시는 commit·push·배포 완료가 아니다.

## 2. 실제 구조

| 위치 | 책임 |
|---|---|
| app/trading_review_worker.py | 기존 설정 검증·DB/key/metrics/executor 조립·signal·CLI |
| application/normalization_worker.py | claim·cancellation·heartbeat·retry·atomic publication 조율 |
| application/normalization_execution.py | outcome·executor port·좁은 설정, pin/hash 검증·계산 입력 변환·Engine 호출·safe failure 분류 |
| application/normalization_payload.py | 기존 공개 projection·masking·V1/V2 section 구성 |
| adapters/normalization/artifacts.py | allowlist·redirect·expiry·타입·길이·SHA-256를 검사하는 bounded HTTP 접근 |
| adapters/normalization/process_executor.py | top-level child target·spawn·Pipe·deadline·poll/join·terminate/kill |

기존 `app/trading_review_normalization.py`와 `app/trading_review_payload.py`는 제거했다.
Runtime·테스트는 새 소유 모듈을 직접 참조하며 임시 re-export 모듈은 두지 않았다.
`refinvest-trading-review-worker` console entrypoint와 systemd·환경 변수는 유지한다.

Application worker는 TradingReviewWorkerRepository·NormalizationExecutor·metrics Protocol을
사용한다. Metrics 실패를 business 전이로 번역하지 않는 기존 best-effort guard와 logger/event
이름을 보존했다. Metrics registry·HTTP scrape 서버는 app/observability에 유지한다.
Publication 상한 오류 ResultPayloadLimitExceeded는 application이 소유하고 adapter가 같은
class를 사용한다. 기존 SQL·트랜잭션 본문은 변경하지 않았다.

Child에는 전체 Settings 대신 기존 속성 이름을 가진 frozen NormalizationExecutionSettings를
전달한다. 필드는 tzdb version·identity key·masking key·artifact hosts·payload 상한 다섯 개다.
DB URL·payload encryption key·internal API key는 spawn 인자에서 제외한다. OS 환경 전체를
격리했다는 의미는 아니다. Child target과 outcome은 새 canonical module에서 pickle 가능하며,
부모만 DB 상태를 바꾼다. 실행 중인 checkout을 서로 다른 소스 버전으로 교체하는 방식은 지원하지 않는다.

Artifact downloader는 application에 callable port로 전달한다. Application에는 URL opener·
Settings·DB·multiprocessing import가 없다. Result pin·input hash·필수 설정 검증 → grant
다운로드·길이/hash 검증 → instrument 변환·지원 VersionSet 확인 → 계산·projection·
canonicalization·전달 상한 확인 순서가 같다. IPC send/close와 실패 시 송신은
adapter에서 처리하며 safe error 분류는 application에 둔다.

## 3. 보존 기준과 검증

- Outcome dataclass AST, 입력 helper·artifact 검증 AST: 소유권 이름 변경 외 동일.
- Process executor: 설정 annotation 외 AST 동일. Spawn·0.2초 poll·deadline 시작점·join·5초
  종료 유예·강제 kill·EOF 분류를 유지한다.
- Worker host: concrete annotation을 port로 바꾸고 metrics guard 소유권을 옮긴 것 외 AST 동일.
- CLI 함수 AST와 projection source bytes: 동일. Factory는 기존 값에서 좁은 설정을 조립한다.
- 실제 계산 본문: sender 대신 return/callable 경계를 추출한 것 외 AST 동일.
- 실패 분류: 기존 except body를 같은 순서의 분류 함수로 추출했으며 AST 동일성을 확인했다.
- 고정 합성 입력·ID·시각의 V1 20,098 bytes / V2 22,259 bytes·전송 hash·resultHash·manifestHash 동일.
- 정확한 상한·상한 1 byte 초과, 미지원 hash·잘못된 pin·허용하지 않은 host의 오류 분류도 동일.

위 snapshot은 비교용 합성 입력이며 Context의 정본 vector를 대체하지 않는다.
새 image의 implementationDigest를 과거 값으로 위장하지 않으며, 실제 배포·지원 버전 등록은
정본 version 정책을 따라 별도로 수행한다.

검증 결과:

- 관련 기존 회귀와 추가 architecture 규칙: 85 passed.
- 새 실제 spawn/IPC·worker 정책 테스트: 11 passed.
- 전체 로컬 suite: 589 passed / 124 skipped.
- 실제 DB 직접 범위: 7 passed / 0 skipped. 기존 claim/reclaim·running cancellation·grant expiry
  3개와 새 application worker의 COMPLETED/CANCELLED/LEASE_LOST/PAYLOAD_LIMIT 4개다.
- Formatter 240개 파일·Ruff lint·Pyright strict·전후 contract-sync: PASS.
- 고정 Core publication 재실행: 실제 grant download·새 child 계산·결과 검증·발행·ACK 모두 PASS.

124 skip은 DB 37개와 폐기된 test-only PoC 87개다. 미변경 Job의 DB 전체 재실행으로 범위를
확대하지 않았다. 새 DB 테스트는 합성 terminal fixture와 executor만 고정하고 실제 repository와
application worker를 연결해 상태·descriptor·bytes를 확인한다. Core publication은 실제 child와
artifact download를 사용하는 별도 검증이다.

실제 spawn 테스트는 새 module import·outcome 왕복·EOF의 retryable 분류, 취소·일반 deadline
경로에서 child join을 확인한다. IPC send 오류의 safe 분류·sender close, spawn 설정에 부모
credential이 없는지, heartbeat 상실·취소 우선순위·stale publication metrics·metrics 장애도 검증한다.

최초 Core publication 실행은 migration 0001 기록 중 Windows DB 연결 오류로 중단됐고,
worker 시작 전 실패다. 실패 로그·partial schema를 보존하며 재실행 결과로 덮어쓰지 않는다.
새 schema·별도 포트의 publication 재실행은 통과했으며 생성한 관리 프로세스를 종료했다.

정본은 ADR-063/064/065/067/070, [Job 운영 문서](TRADING_REVIEW_JOB_RUNTIME.md),
[입력 identity](../../../context/docs/TRADING_NORMALIZATION_IDENTITY.md),
[전달 정책](../../../context/docs/TRADING_NORMALIZATION_RESULT_DELIVERY.md)다.
OpenAPI·migration·fixed fixture·거래 계산·Temporal/Execution 의미론은 변경하지 않았다.

## 4. 남은 운영 위험과 후속 단계

기존 EOFError 경로는 timeout 없는 process.join()을 호출한다. Child가 Pipe를 닫은 뒤에도
종료하지 않으면 parent가 deadline을 넘어 기다릴 수 있다. 이 구조 변경은 해당 동작을 고치지
않았으며 정상 timeout/cancel 테스트를 이 EOF 경로의 제한 시간 보장으로 해석하지 않는다.
EOF 대기의 deadline 적용·오류 분류와 spawn/예외 경로의 handle 회수는 별도 운영 보완 PR로
검토한다. 이 문서로 새 timeout/retry 정책을 정하지 않는다.

다음 구조 작업은 AR-05A Analytics worker 분리다. Backtest process adapter 이동은 AR-05B,
SQL 내부 책임 분리는 AR-06, root eager app bootstrap은 AR-11에 남는다.
기존 import 예외 6개는 Asset/Series의 AR-07 대상이며 이번에 추가한 예외는 없다.
새 adapter 규칙은 app·API·DB import를 막고, 순수 execution application의 process/파일 I/O
구현도 차단한다. 실제 경계는 [의존성 문서](ARCHITECTURE_DEPENDENCIES.md)에서 관리한다.

증적은 Integration의
[AR-05N manifest](../../../integration/.integration-data/architecture-refactoring/ar05n/20261029T082404Z/manifest.json),
[통과 publication](../../../integration/.integration-data/trading-normalization/c7fdb844a251410295bb74c2fdacd5c5/evidence.json),
[최초 migration 실패 로그](../../../integration/.integration-data/architecture-refactoring/ar05n/20261029T082404Z/normalization-publication.log)에
보관하며 Compute에 커밋하지 않는다. Core 대상은 기존 고정 206473c이며 원본 Core HEAD 조합,
V2 Core publication·Analytics Core E2E를 검증한 것으로 해석하지 않는다.
Determinism impact: none. Point-in-Time impact: none.
