# AR-11B — HTTP 오류 응답 경계

2026-10-31. `app/main.py`의 두 framework exception handler를 `api/exception_handlers.py`로
옮긴다. 기존 HTTP 응답·예외 등록·OpenAPI를 보존하는 구조 정리이며 새 오류 정책은 도입하지 않는다.

## 1. 변경과 소유권

`request_validation_error_handler`와 `internal_http_error_handler`의 signature·본문 AST가
기존과 같다. RequestValidationError의 400 응답과 HTTPException의 status/body를 API 모듈이
소유하며, app은 module import 후 framework 예외와 handler를 등록한다.
이전 app handler 이름이나 uuid4의 re-export는 남기지 않는다.

- Non-Trading-Review validation은 기존 DSL_INVALID message/errorCode/details 투영을 쓴다.
  중첩 위치·정수 index를 포함한 details 순서와 alias 이름도 유지한다.
- Trading Review validation은 REQUEST_INVALID/message/traceId/retryable 형태를 유지한다.
  Input/body의 private 값을 응답에 복사하지 않는 기존 동작도 검증한다.
- Trading Review HTTP 401·404·503의 code와 그 밖의 REQUEST_INVALID fallback, status,
  retryable 판정·일반 message를 그대로 둔다. Non-Trading-Review는 기존 detail 응답을 유지한다.
- Exception에 전달된 header의 전파 여부도 기존 동작을 유지한다. 보존 검증에 Retry-After를
  포함하지만 이 PR에서 해당 정책을 바꾸지 않는다.
- 예상하지 않은 타입의 예외는 같은 객체를 다시 raise한다. 새 catch-all·fallback은 추가하지 않는다.

기존 schema mapper는 `api/schemas/errors.py`, route 고유의 오류·JSON key/pin 사전 검사는
각 route에 유지한다. HTTP metrics middleware·operation 분류, lifespan, config·router 조립은
이번 이동 범위 밖이다. API handler는 app/config·application·persistence·route·provider·네트워크에
의존하지 못하고 framework와 기존 API schema mapper만 사용하도록 검사한다.

## 2. 커밋과 테스트 경계

1. 기존 코드에 18개 오류 계약 사례를 추가해 관련 suite 177개를 통과시킨 뒤 `c656ddd`로
   먼저 커밋했다. Validation 4개, HTTP status/family 12개, 예외 재전파 2개다.
2. Handler 이동·등록 연결, trace ID를 사용하는 테스트 두 곳의 실제 사용 모듈 패치,
   의존성 금지 사례 5개와 문서를 함께 반영한다.

테스트용 probe route는 실제 동적 job route와 겹치지 않게 별도 suffix를 사용하고 OpenAPI에서
제외했다. Handler의 실제 framework 등록을 통해 응답 bytes·status·content-type·header를
검증하며, trace ID만 테스트에서 고정한다. Production에서는 기존 uuid4를 그대로 사용한다.

## 3. 검증 결과

- 전후 두 handler 함수의 AST 동일. Runtime OpenAPI는 모두 44,951 bytes이며 SHA-256은
  `fa9da4fa83f1bc4b222165346d6dea807515a60355a15293d97cbd62429825af`다.
- 변경 후 관련 Windows suite **182 passed**. Ruff·formatter 263 files와 기존 src strict Pyright
  0 errors/0 warnings. 전후 `verify-contract-sync.ps1 -IncludeWeb` 통과.
- Windows 전체 **770 passed / 131 skipped**, 기존 AnyIO assert rewrite 경고 1개.
  Linux native 관련 suite **182 passed**, 별도 bootstrap 없음.
- 실제 Core `b9a170ff35280b4bf13e0d58479f05db1cb9e8a3`의 현재 source를 Gradle로 기동해
  JWT/CSRF → Core dispatch → Compute worker → Core terminal persistence smoke를 통과했다.
  Same-market·cross-market·drawdown·replay는 COMPLETED, 달력 해석 실패 사례는
  CALENDAR_RESOLUTION_FAILED를 유지한 FAILED다.
  Run ID는 `20261031T031207Z`이며 로컬 integration의
  `.integration-data/architecture-refactoring/ar11-errors/20261031T031207Z/summary.json`에
  PASS 증적을 보존했다. 분리 DB `refinvest_integration`과 전용 Compute schema·immutable fixture를
  사용했고, 종료 후 해당 registry의 process 항목이 비어 있음을 확인했다. Core source 변경은 없다.

검사 때의 source 지문을 보존하고 최종 커밋 내용과 대조한다. 로컬 PR_BODY.md·credential·
fixture·실행 증적은 커밋하지 않는다. Windows의 기존 외부 IPv6 bootstrap/AnyIO 경고와
Linux의 native 검증은 구분하며, 환경 미구성·기존 PoC skip을 통과로 계산하지 않는다.

## 4. 남은 작업

HTTP 관측 middleware는 후속 [AR-11C](HTTP_METRICS_BOUNDARY.md)에서 API 경계로 이동했다.
Package root의 즉시 HTTP import는 [AR-11D](PACKAGE_ENTRYPOINT_BOUNDARY.md)에서 제거했다.
AR-11의 CLI 업무 조율과 최종 architecture/runbook 정리는
후속 개별 범위다. AR-11B의 완료 범위는 전역 HTTP 오류 응답이다.
ADR-071은 계속 DRAFT이며 Analytics 실행 제한 수치·production Linux/systemd 격리·Core Analytics
dispatch/publication/ACK E2E의 기존 gate를 완료한 것으로 해석하지 않는다.

Determinism impact: none. Point-in-Time impact: none.
