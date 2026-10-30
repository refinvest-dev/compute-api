# Analytics 계약 모듈 — AR-02A

상태: AR-02A 완료. 계약 추출·consumer 전환·직접 영향 범위의 검증을 마쳤다.
검증 run: `20261029T032238Z` (UTC). 작업일은 2026-10-29 KST다.

## 1. 변경 범위와 PR 경계

| 기존 위치 | 현재 위치·책임 |
|---|---|
| app/trading_analytics_identity.py 전체 | contracts/analytics/identity.py: strict JSON·JCS·input/config hash·create fingerprint·입력 identity와 지원 버전 확인 |
| app/trading_analytics_result.py의 SECTIONS·SECTION_FIELDS·finish_sections | contracts/analytics/result_integrity.py: 공개 section 정의·metric/section/evidence/result digest 구성 |
| app/trading_analytics_result.py의 build_sections와 projection helper | 기존 위치 유지; application 이동은 AR-05A |

계약 추출, API·입력 adapter·worker·PostgreSQL adapter·테스트의 직접 import 전환,
관련 legacy 예외 제거와 검증을 하나의 커밋·PR 범위로 검토한다.
Analytics use case·port는 AR-03A, projection·worker 조율·I/O 분리는 AR-05A에서 수행한다.
이번 변경은 그 후속 경계를 활성화하지 않는다. 기존 작업 트리의 AR-00/01/02N 변경과
PR 보조 파일은 보존하며, 이 문서의 완료 표시는 commit·push·배포 완료를 뜻하지 않는다.

이전 identity 모듈은 제거했다. 임시 re-export를 추가하지 않고 모든 내부 consumer가
새 계약을 직접 사용한다. Integration·Context·Core source 검색에서 이전 identity 경로나
result 모듈을 사용하는 외부 Python consumer는 발견되지 않았다.

## 2. 보존한 경계

Identity 모듈 전체의 AST가 이동 전과 같다. JCS의 UTF-16 key 순서·safe integer 범위,
중복 JSON key 거절, Unicode 비정규화, 필드 projection, locator/expiry 제외,
오류 코드와 exact implementationDigest 검사를 변경하지 않았다.

추출한 두 상수와 finish_sections의 AST도 그대로다. 이 함수가 사용하는 세 줄의
UTF-16 정렬 helper는 계약 내부에 동일하게 두고 projection의 기존 helper도 유지한다.
공통 유틸 계층이나 두 기능의 공용 canonicalizer를 도입하지 않는다.

finish_sections는 기존대로 metricHash를 section item에 기록한 뒤 구조적 count와 digest를
계산한다. 이 함수만으로 전체 공개 schema·중첩 참조를 검증하는 것은 아니다.
독립적인 정본 vector 검증과 기존 API·입력 adapter 검사를 함께 유지한다.
결과 의미론 보완이나 새로운 검증 정책을 계약 이동에 섞지 않는다.

남은 projection의 모든 함수·상수 AST가 같다. 네 production consumer도 import 외의
module body가 기준 commit과 동일하다. SQL·transaction·lease·retry·publication·ACK·retention,
bounded JSON serialization, 전달 cap과 media type은 변경하지 않았다.
Engine 모델은 계약 내부로 들어오지 않는다. API transport 모델도 API에 유지한다.

정본은 Context ADR-060/069/070과 다음 문서다.

- [입력 identity](../../../context/docs/TRADING_ANALYTICS_INPUT_IDENTITY.md)
- [결과 무결성](../../../context/docs/TRADING_ANALYTICS_RESULT_INTEGRITY.md)
- [결과 전달](../../../context/docs/TRADING_ANALYTICS_RESULT_DELIVERY.md)
- [Job protocol](../../../context/docs/TRADING_ANALYTICS_JOB_PROTOCOL.md)
- [Compute runtime](TRADING_ANALYTICS_RUNTIME.md)

OpenAPI·migration·정본 fixture·VersionSet·지표 계산·Temporal/Execution 정책은 변경하지 않았다.
새 artifact의 implementationDigest와 기존 Job pin 처리는 정본 배포 정책을 따른다.
이번 작업은 새 image 배포나 지원 버전 등록을 포함하지 않는다.

## 3. 전후 비교와 검증

정본 합성 입력을 사용하고 Job/Run/attempt ID와 시각을 고정해 실제 worker를 in-memory
repository와 고정 artifact downloader로 실행했다. 전후 결과 bytes를 직접 비교했다.
이 합성 비교는 실제 Core dispatch·HTTP download·DB 발행의 E2E가 아니다.

| 비교 항목 | 전후 결과 |
|---|---|
| Identity canonical bytes·input hash | 동일, 1,363 bytes, SHA-256 prefix fd11891b4aa4 |
| AnalysisConfig hash·create fingerprint | 동일 |
| 실제 worker terminal bytes | 동일, 7,045 bytes, SHA-256 prefix add304fcc1c8 |
| worker metric/section/evidence/result digest | 동일, 6 section digests |
| 정본 결과 vector의 digest | 동일, resultHash prefix 29fc9f6665cf |
| CreateAnalyticsJobTransport JSON schema | 동일, SHA-256 prefix 0ada0df1207f |
| Identity 전체·추출 책임·남은 projection AST | 동일 |

검증 결과:

- Formatter·Ruff lint·Pyright strict: PASS.
- 의존성·Trading Review runtime·Analytics 회귀: 50 passed.
- 전체 로컬 suite 재실행: 504 passed / 117 skipped.
- 직접 영향 범위의 격리 PostgreSQL Analytics 단독 실행: 4 passed / 0 skipped.
- 구현 전·후 contract-sync `-IncludeWeb`: PASS.
- Integration Analytics 합성 fixture 10개와 tamper self-test: PASS.

117 skip은 DB 테스트 30개와 폐기된 test-only PoC 87개다. 실제 DB 검증을 별도로 수행했다.
Analytics DB 4개는 create/claim 동시성·재시작 fencing·retry, terminal 발행·descriptor·payload
재조회·ACK·cleanup, Normalization과의 공유 capacity, retry cutoff를 확인한다.

최초 전체 로컬 실행은 Windows socketpair WinError 10014와 AST recursion SystemError로
2개가 실패했다. 코드 변경 없이 새 process에서 전체 통과했다.
DB 초기화 첫 호출은 PowerShell의 상대 script 경로 해석에서 실패했으며 절대 경로와 명시적
parameter로 수정했다. Migration 0001~0017을 적용한 새 schema에서만 테스트했다.

검증을 세 Job 전체로 확장한 일괄 실행은 세 번 모두 Windows DB 연결 오류
`Bad address 10014`로 일부 실패했다(각 29/30, 28/30, 29/30 passed).
두 번째 실행은 Analytics 연결 실패로 남은 active Job이 후속 Normalization capacity 검사에도
영향을 줬다. 이 실행들을 PASS로 바꾸지 않는다. 마지막 일괄 실행의 Normalization 20개와
Analytics 4개, 첫 실행의 Backtest 6개는 각각 통과했지만, 이번 AR-02A에서 세 Job 30개가
한 번의 실행으로 모두 통과했다고 보고하지 않는다. 직접 변경한 Analytics 4개는 별도 새
schema·process에서 전부 통과해 완료 gate를 확정했고, 확장 검증의 환경 제약은 남긴다.
Production 코드나 테스트에 자동 재시도·skip·socket workaround를 추가하지 않았다.

Analytics 실제 Core dispatch·publication·ACK E2E harness는 현재 없으며 실행하지 않았다.
기존 AR-00에서 추적한 Integration 후속 작업과 AR-10 운영 완료 gate로 유지한다.
API/worker 모의 테스트·실 DB 통합·fixture 검증을 Core E2E 통과로 해석하지 않는다.

## 4. 의존성 이행과 남은 작업

AR-02A의 2개 import group·5개 심볼 예외를 제거했다.
남은 예외는 4개 파일, 10개 group, 15개 심볼이다.
[legacy registry](../tests/architecture_legacy_imports.json)와
[의존성 운영 문서](ARCHITECTURE_DEPENDENCIES.md)에서 관리한다.

API의 의미 검증 호출은 기존 위치에 남아 있으므로 use case 분리 완료로 보지 않는다.
입력의 artifact 검증·변환과 결과 projection·bounded serialization은 AR-05A의 책임이다.
Root package의 eager FastAPI app import는 AR-02N과 동일하게 AR-11에서 검토한다.

증적은 Integration의
[AR-02A manifest](../../../integration/.integration-data/architecture-refactoring/ar02a/20261029T032238Z/manifest.json)에 별도 보관한다.
Manifest·전후 snapshot·로그·JUnit은 로컬 자료이며 Compute에 커밋하지 않는다.
Determinism impact: none. Point-in-Time impact: none.
