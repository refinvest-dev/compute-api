# Normalization 계약 모듈 — AR-02N

상태: AR-02N 완료. 계약 추출·consumer 전환·고정 Core E2E 검증을 마쳤다.
검증 run: `20261028T145837Z` (UTC). 로컬 작업일은 2026-10-29 KST다.

## 1. 변경 범위

| 기존 위치 | 현재 위치·책임 |
|---|---|
| app/trading_review_identity.py | contracts/normalization/identity.py: identity DTO·canonicalization·input hash·정확한 지원 registry 확인 |
| app/trading_review_result_integrity.py | contracts/normalization/result_integrity.py: V1/V2 공개 결과의 검증·canonicalization·digest |
| app/trading_review_result_delivery.py | contracts/normalization/result_delivery.py: pinned 전달 policy·정확한 바이트 상한 |
| app/trading_review_payload.py의 MEDIA_TYPE | contracts/normalization/result_delivery.py의 동일한 상수 |

API schema·route, normalization worker·child executor, payload builder, PostgreSQL adapter와
테스트가 새 위치를 직접 사용한다. 이전 세 app 모듈은 제거했고 임시 re-export는 두지 않았다.
Integration/Context/Core source 검색에서 이전 Python 경로를 사용하는 consumer는 발견되지 않았다.
외부 HTTP 요청·응답, console script, worker 진입점은 유지한다.

이 변경은 계약 모듈 이동·모든 consumer 전환·해당 legacy 예외 제거를 함께 검토하는 하나의
범위다. 서로 연결되지 않은 production 경로를 활성화하지 않는다. Analytics 계약 이동은
AR-02A, application use case 분리는 AR-03, projection/worker 실행 분리는 AR-05에서 수행한다.

## 2. 보존한 경계

세 모듈의 class·함수 본문은 그대로 이동했다. MEDIA_TYPE 추가를 제외한 module AST가
이동 전과 일치한다. DTO의 field/default/validation, V1/V2 버전 쌍, UTF-8·Decimal·timestamp·배열
정규화, 오류 코드와 바이트 상한 정책을 변경하지 않았다.
여섯 production consumer도 import와 MEDIA_TYPE 소유권을 제외한 module body AST가
기준 commit과 동일한지 확인했다. SQL·transaction·worker 실행 본문을 변경하지 않았다.

`app/trading_review_payload.py`의 projection은 그대로 유지한다. 그 모듈이 사용하는 공개 결과
검증 함수와 상수의 소유권만 계약으로 옮겼다. Protocol DTO와 Engine 결과를 하나의 타입으로
합치지 않는다. 계약 모듈의 direct import는 AR-01 경계 검사를 따른다.

PostgreSQL adapter는 새 계약의 동일한 validator와 전달 policy를 호출한다. 소유권·pin 확인,
취소·lease 조건, 결과/descriptor/상태 저장의 transaction scope, 암호화·ACK·retention을 유지한다.
Persisted request는 JSONB이고 terminal payload는 bytes다. 이동한 identity model의 Python
module 경로를 DB에 저장하지 않는다. Child target과 outcome dataclass의 위치도 유지한다.

정본은 Context의 ADR-065/066/067/070과 다음 문서다.

- [입력 identity snapshot](TRADING_NORMALIZATION_IDENTITY.md)
- [정본 결과 무결성](../../../context/docs/TRADING_NORMALIZATION_RESULT_INTEGRITY.md)
- [정본 전달 정책](../../../context/docs/TRADING_NORMALIZATION_RESULT_DELIVERY.md)
- [실행·발행 경계](TRADING_REVIEW_JOB_RUNTIME.md)

OpenAPI, 정본 fixture, migration, Fee/Slippage·Temporal 계산, retry/deadline 정책은 변경하지 않았다.

## 3. 동일성 검증

이동 전에 고정된 합성 입력·Job/Run ID·시각·masking key로 snapshot을 만들고 이동 후
동일 입력을 다시 실행했다. 아래 결과는 이 합성 비교 입력의 값이며 정본 vector를 대체하지 않는다.

| 비교 | 전후 결과 |
|---|---|
| Identity canonical bytes·input hash | 동일, SHA-256 prefix `1499e16878c4` |
| V1 terminal bytes·전송 SHA-256·resultHash | 동일, 20,114 bytes, 9 section digests |
| V2 terminal bytes·전송 SHA-256·resultHash | 동일, 22,275 bytes, 10 section digests |
| CreateTradingNormalizationJobTransport JSON schema | 동일, SHA-256 prefix `a9ff7c84051c` |
| 세 계약 모듈의 AST | 동일; 이동한 MEDIA_TYPE assignment만 비교에서 분리 |

Context fixed vector와 잘못된 입력·버전·해시·전달 크기 경계는 기존 regression을 재사용했다.
Runtime class 위치에 의존하는 임시 alias 대신 모든 내부 consumer를 전환했다.

검증 결과:

- 구현 전 contract-sync `-IncludeWeb`: PASS.
- 구현 후 contract-sync `-IncludeWeb`: PASS.
- Formatter·Ruff lint·Pyright strict: PASS.
- 전체 로컬 pytest 재실행: 504 passed / 117 skipped.
- 격리 PostgreSQL: migration 0001~0017, 세 Job 통합 테스트 30 passed / 0 skipped.
- 고정 Core E2E 재실행: PASS, 정상 계산·발행·ACK, V1 pin과 ORCH-024~028.
- Skip 117개는 별도 실행한 DB 테스트 30개와 폐기된 test-only PoC 87개다.

최초 로컬 실행은 Windows socketpair WinError 10014 두 건과 AST recursion SystemError 한 건으로
실패했다. 변경 없이 새 process에서 재실행해 통과했다. 초기 실패 로그도 보존한다.
첫 Core E2E는 정상 계산·발행·ACK와 ORCH-024~026을 통과했지만 ORCH-028의 서버 재시작에서
Windows socketpair 오류가 3회 연속 발생해 중단됐다. 별도 포트·새 schema로 같은 harness를
재실행하여 전체 통과했고, 첫 실패 증적도 보존했다. 재시도 횟수나 production 정책을 바꾸지 않았다.

Core E2E는 AR-00과 같은 고정 Core `206473c`를 사용했다. 현재 원래 Core 작업 트리의
별도 변경을 이 호환성 조합에 섞지 않는다. 현재 Core HEAD의 새 조합 검증으로 해석하지 않는다.
V2 Core publication은 기존 harness의 범위 밖이며 V2 Compute 계산·검증 통과와 구분한다.
ORCH-027은 기존 autocommit 경계에서 migration 0017을 검증했다. Atomic runner의
SQLSTATE 55006은 harness가 기록하는 기존 배포 제약이며 이번 변경으로 해소되지 않는다.
새 배포 artifact의 implementationDigest·VersionSet pin은 Context의 배포 정책에 따라 검토한다.
이번 작업은 새 image 배포나 새로운 지원 버전 등록을 포함하지 않는다.

## 4. 의존성 이행과 남은 작업

AR-02N에 속한 6개 import group·11개 심볼 예외를 제거했다.
남은 예외는 5개 파일, 12개 group, 20개 심볼이다. 정확한 목록은
[legacy registry](../tests/architecture_legacy_imports.json)와
[의존성 운영 문서](ARCHITECTURE_DEPENDENCIES.md)에서 관리한다.

`refinvest/__init__.py`가 FastAPI app을 eager import하는 기존 동작은 유지한다.
계약 모듈 자체의 direct 의존성은 분리했지만 package root의 bootstrap까지 분리한 것은 아니다.
Framework 없이 package를 독립 import·배포하는 변경은 AR-11에서 별도로 검토하며, root export나
console entrypoint의 호환성을 이번 계약 추출에 섞지 않는다.

검증 증적은 Integration의
[AR-02N manifest](../../../integration/.integration-data/architecture-refactoring/ar02n/20261028T145837Z/manifest.json)에
별도로 보관한다. [통과 E2E](../../../integration/.integration-data/trading-normalization/e8c1fc7097b740569c423dd743037ea3/evidence.json)와
[초기 실패 E2E](../../../integration/.integration-data/trading-normalization/c3167d58c41a48b895bf9adbb7220146/evidence.json)를
함께 유지한다. Manifest·전후 snapshot·log·JUnit은 로컬 증적이며 Compute에 커밋하지 않는다.
Determinism impact: none. Point-in-Time impact: none.
