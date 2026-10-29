# 공용 terminal 전달 application — AR-04

상태: AR-04 완료. 구현·HTTP 연결·전후 비교와 직접 영향 범위의 DB·Core publication 검증을 마쳤다.
검증 run: `20261029T055631Z` (UTC), 작업일 2026-10-29 KST.

## 1. 범위와 커밋·PR 경계

`application/terminal_delivery.py`에 공용 resource 조회와 기능별 ACK 조율을 둔다.
두 기능의 Job 상태·저장 schema·retry·발행 정책을 공통 framework로 합치지 않는다.

하나의 기능 PR을 다음 두 커밋 경계로 검토한다.

1. Normalization 전달 전용 port·application 반환/command·use case·정책 테스트.
   기존 composition port는 Job port와 전달 port를 상속해 HTTP 연결 전에도 동작한다.
2. HTTP 두 경로의 application 연결, 관련 API/실 DB/Core publication 검증과 문서.
   미연결 production 경로를 활성화하지 않고 public URI·operation ID·wire 모델을 보존한다.

선행 작업의 미커밋 변경과 PR 보조 파일을 보존한다. 구현 완료는 commit·push·배포 완료가 아니다.

## 2. 실제 경계

| 요소 | 책임 |
|---|---|
| get_terminal_payload | Normalization 우선 조회, 부재 시 Analytics 조회, 명시적 bytes/media type 반환 |
| acknowledge_terminal_result | 명시적인 기능별 command로 port 선택, 기존 ACK 형상·오류 처리 연결 |
| NormalizationResultDeliveryRepository | 기존 get_terminal_payload·acknowledge 두 capability |
| AnalyticsResultDeliveryRepository | 기존 payload_bytes·acknowledge 두 capability 재사용 |
| TerminalPayload | 받은 bytes 참조와 기능별 vendor media type; 재직렬화·복사 없음 |
| NormalizationAcknowledgement | 기존 keyword 인자·parsed timestamp·generation 보존 |
| AnalyticsAcknowledgement | 기존 exclude_none JSON projection 하나를 그대로 전달 |
| TerminalDeliveryFailure | 이 두 use case에 한정한 실패 분류·safe code·message·retryable |

Application에는 HTTP·설정·concrete adapter import가 없다. HTTP는 인증·중복 JSON·wire schema·
union 해석·Idempotency-Key 형식 검증과 404/409/422/503 변환을 담당한다.
응답의 UTF-8 charset·identity encoding·204는 기존대로 HTTP에서 만든다.
기능별 command는 wire 검증을 통과한 입력이 전제이며, 임의 dict를 일반 공개 API로 지원하지 않는다.

Provider·설정·필수 Normalization store와 optional Analytics store의 구성은 바꾸지 않았다.
따라서 dependency 구성 실패와 잘못된 헤더 사이의 기존 우선순위도 유지된다.
기존 다섯 Normalization Job 경로와 Analytics Job application·worker는 변경하지 않았다.

## 3. 보존한 조회와 ACK 의미

조회는 opaque payload ID를 Normalization store에서 먼저 찾는다. None일 때만 Analytics를
조회한다. 빈 bytes는 정상 resource이며 fallback 사유가 아니다. Normalization 무결성 오류나
그 밖의 예외를 부재로 간주하지 않는다. Hash를 재계산해 고치거나 다른 payload로 대체하지 않는다.

| 조회 결과 | 기존 응답 |
|---|---|
| Normalization bytes | 200, Normalization vendor media type |
| Normalization None·Analytics bytes | 200, Analytics vendor media type |
| 모두 None 또는 optional Analytics 없음 | 404 PAYLOAD_NOT_FOUND |
| Normalization ResultIntegrityError | 409, 원래 무결성 safe code |
| Analytics payload 조회 unavailable | 404 RESULT_PAYLOAD_EXPIRED |

ACK의 기능 선택은 request union에서 나온 명시적 command를 따른다. 한 store가 실패하면
다른 store에 ACK를 시도하지 않는다. Normalization의 PERSISTED/DISCARDED_STALE 형상 검사,
timestamp 선택, key 해싱과 모든 기존 keyword 인자를 유지한다. Generation의 유효성·현재
attempt/token·hash·동일/상이 replay 검사는 기존 atomic adapter가 계속 담당한다.

Analytics ACK는 기존대로 헤더 key의 형식만 HTTP에서 확인하며 adapter에 새 key hash를
전달하지 않는다. Body projection·동일/상이 receipt 기준을 바꾸지 않는다. Analytics의 stale
reason·current attempt/deletion metadata는 기존 wire validator와 adapter를 따른다.

| ACK 결과 | 기존 응답 |
|---|---|
| 기능별 성공·동일 replay | 204 |
| Normalization 형상 오류 | 422 REQUEST_INVALID |
| Normalization False | 404 JOB_NOT_FOUND |
| Normalization idempotency/state conflict | 409 RESULT_HASH_MISMATCH |
| Analytics store 미구성 | 503 TEMPORARY_DEPENDENCY_FAILURE·retryable=true |
| Analytics unavailable | 원래 reason에 따라 404 RESULT_PAYLOAD_EXPIRED 또는 JOB_NOT_FOUND |
| Analytics conflict | 409 RESULT_HASH_MISMATCH |

기능별 기존 message를 유지한다. 그 밖의 예외를 새 retry·fallback·404로 숨기지 않는다.
두 PostgreSQL adapter와 migration bytes는 작업 전과 같다. ACK 후 bytes 제거·receipt 보존·
24시간 retention·DB clock·트랜잭션·CAS·암호화·발행 원자성은 그대로다.
API schema·OpenAPI·계약 hash·Metric·Temporal/Execution 의미론도 바꾸지 않았다.

정본은 Context ADR-067/069/070과
[Normalization 전달](../../../context/docs/TRADING_NORMALIZATION_RESULT_DELIVERY.md),
[Analytics 전달](../../../context/docs/TRADING_ANALYTICS_RESULT_DELIVERY.md),
[Analytics protocol](../../../context/docs/TRADING_ANALYTICS_JOB_PROTOCOL.md)이다.

## 4. 검증과 환경 제약

- 고정 입력의 payload·ACK 반환·호출 순서·인자 33개: 전후 동일.
  매 실행 달라지는 오류 traceId와 오류 JSON Content-Length는 비교에서 제외했다.
  성공 resource의 bytes·Content-Type·Content-Encoding·Content-Length는 비교에 포함한다.
- 두 ACK 요청 JSON schema: 전후 동일.
- 기존 의존성·HTTP·delivery 정책 회귀: 67 passed.
- 새 application 정책 테스트: 27 passed.
- 전체 로컬 suite 재검증: 573 passed / 120 skipped.
- Formatter 233개 파일·Ruff lint·Pyright strict, 구현 전·후 contract-sync: PASS.
- 직접 영향 범위의 별도 격리 DB 실행: 7 passed / 0 skipped.
- 고정 Core의 별도 publication harness: 정상 계산·결과 검증·발행·ACK, 모두 PASS.

120 skip은 DB 33개와 폐기된 test-only PoC 87개다. DB는 별도 격리 schema에서 실행한다.
전달·ACK·retention의 직접 영향 범위는 Normalization atomic terminal/ACK·24시간 만료·
token/stale generation 검사 3개, Analytics descriptor/ACK/cleanup 1개, 실제 provider를 쓰는
HTTP/worker smoke 3개다. 후자는 PERSISTED·DELETED_GENERATION·STALE_ATTEMPT를 각각 검증한다.
실제 artifact 다운로드만 합성 fixture로 고정하며, 같은 ACK replay 204·변경 ACK 409·bytes
제거와 후속 별도 Job 취소도 확인한다. Core publication과 이 ASGI/DB smoke는 구분한다.

최초 전체 suite와 두 번의 재실행은 Windows socketpair WinError 10014 또는 Python AST
recursion 오류로 실패했다. 코드 변경 없이 새 process에서 전체 통과했다.
확장 DB 27개 일괄 검증은 세 번 모두 DB 연결 Bad address 10014로 실패했다
(25/27, 24/27, 24/27 passed). 실패로 남은 pending Job이 후속 worker claim/capacity 검사에
영향을 준 경우도 함께 보존한다. 이번 작업에서 27개가 한 번에 모두 통과했다고 보고하지 않는다.
Migration 초기화 중 같은 연결 오류로 중단된 새 schema도 테스트 통과로 계산하지 않는다.
최종 직접 범위 검증은 새 schema에서 migration 0001~0017을 한 psql 연결에 순서대로
적용했다. 기존 harness와 같은 autocommit·파일별 statement 경계를 유지하며 `--single-transaction`을
사용하지 않는다. Migration 기록도 새 schema에만 작성했다. Harness source와 production은 바꾸지 않았다.

확장 version-pin Core E2E는 정상 계산·결과 검증·발행·ACK, V1 pin과 ORCH-024~026을
통과했지만 ORCH-028 canary proxy의 socketserver.server_activate에서 OSError로 중단됐다.
ORCH-027은 그 실행에서 NOT_RUN이다. 이 전체 실행을 PASS로 바꾸지 않는다.
직접 대상인 별도 publication harness는 새 schema·별도 포트에서 전체 통과했다.
확장 검증 실패와 직접 범위의 통과를 서로 덮어쓰지 않는다. 생성한 관리 프로세스는 종료했다.

Core 비교 대상은 기존 고정 commit 206473c다. 원본 Core HEAD 조합이나 V2 Core publication,
Analytics Core dispatch·publication·ACK E2E 검증으로 해석하지 않는다. Analytics Core harness
공백은 기존처럼 Integration 후속 작업·AR-10 운영 완료 gate로 남는다.
Production·테스트에 소켓 오류 자동 재시도·skip·전역 설정 workaround를 추가하지 않는다.

## 5. 후속 단계

AR-04의 두 공용 조율 경로를 application에 연결했다. 의존성 예외 수는 AR-03A 이후와 같은
Asset/Series의 6개이며, 새 예외를 추가하지 않는다. Worker 조율·실행 adapter는 다음 AR-05,
persistence 책임 분리는 AR-06, root eager app bootstrap은 AR-11에 남는다.
새 image 배포·지원 버전 등록·전달 정책 확장은 이 작업에 포함하지 않는다.

증적은 Integration의
[AR-04 manifest](../../../integration/.integration-data/architecture-refactoring/ar04/20261029T055631Z/manifest.json),
[통과 publication E2E](../../../integration/.integration-data/trading-normalization/5aece1f2c38b4e3083b95d3d5f08fe09/evidence.json),
[확장 version-pin/cutover 실패](../../../integration/.integration-data/trading-normalization/5a9b54f3d14c4393b92a0d1211ad4e50/evidence.json)에
별도 보관하고 Compute에 커밋하지 않는다.
Determinism impact: none. Point-in-Time impact: none.
