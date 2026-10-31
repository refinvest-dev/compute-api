# AR-11A — Core 전용 HTTP 인증 경계

2026-10-31. `app/security.py`의 HTTP 인증을 `api/security.py`로 옮기는 독립 구조 정리다.
ADR-071은 여전히 DRAFT이며 실행 제한·운영 수치·새 오류 정책을 승인하거나 구현하지 않는다.

## 1. 경계와 변경

`require_internal_api_key`는 FastAPI의 Depends/Header/HTTPException을 사용해 Core HTTP
요청을 검사한다. 이 책임을 composition 영역인 `app/`에서 inbound `api/`로 이동했다.
설정은 같은 `app.config.Settings/get_settings`를 사용하며 인증 함수·module AST는 기존과 같다.

- assets·series·backtests·Normalization·Analytics의 다섯 route 모듈이 새 dependency를 사용한다.
- 내부 API key 미설정은 503, 누락/불일치는 401이며 `hmac.compare_digest`를 유지한다.
  Header 이름·오류 detail·Trading Review code/message/traceId/retryable 투영을 보존한다.
- 공개 `/health` probe와 인증 후의 정상 요청·입력 검증·application 호출은 기존과 같다.
- Trading Review의 중복 JSON key·version pin 사전 검사는 해당 inbound route에 그대로 둔다.
  이 검사에서 인증 여부를 확인하는 순서와 HTTP 오류 처리도 변경하지 않는다.
- 이전 app 모듈과 re-export를 남기지 않는다. Repository 내부의 호출·테스트 import는 전환했다.

`api.security`는 표준 라이브러리·FastAPI·최소 설정 외 의존을 허용하지 않는다.
DB·application·다른 route·dependency provider를 조립할 수 없으며, route가 이전
`app.security`를 다시 사용하거나 인증이 다른 app 구현을 import하면 정적 검사에서 실패한다.
기존 legacy 예외 registry는 계속 빈 배열이다.

## 2. 커밋·검증 순서

1. 변경 전 모든 계약 operation의 인증 응답을 먼저 고정했다. 17개 operation에 대해 누락 key·
   불일치 key·설정 미구성 51개 wire 사례, 공개 health 2개와 endpoint coverage 1개를 검증했다.
   관련 suite 155개 통과 후 `dfef5db`로 커밋했다.
2. 인증 함수 이동, 다섯 consumer·단위 테스트 전환, 정적 경계와 금지 import 4개 회귀,
   architecture 문서·계획을 함께 반영한다. CLI·error handler 전체를 같은 PR에 옮기지 않는다.

Route coverage는 canonical OpenAPI operation과 실제 runtime operation을 비교한다.
`runId`/`run_id` 같은 자리표시자 이름만 정규화하며 실제 요청은 canonical 경로의 구체 ID로
전송한다. Trading Review trace ID는 테스트에서만 고정해 전체 오류 body bytes를 비교한다.

## 3. 보존 증거

- 인증 module AST가 변경 전후 동일하다. Production key·추가 인증 방식·기본값은 도입하지 않았다.
- Runtime OpenAPI는 전후 **44,951 bytes**이고 SHA-256은 모두
  `fa9da4fa83f1bc4b222165346d6dea807515a60355a15293d97cbd62429825af`다.
- OpenAPI YAML·Context golden fixture·계산·Snapshot·job protocol·SQL·migration은 바꾸지 않았다.
- `verify-contract-sync.ps1 -IncludeWeb` 전후 통과.
- Windows 관련 suite **159 passed**, 전체 **747 passed / 131 skipped**.
  Linux에서도 관련 suite **159 passed**이며 현재 source와 정규화된 파일 내용이 같은 checkout에서
  실행했다. 환경 의존/기존 PoC skip을 통과로 세지 않는다.
- Ruff·formatter 261 files·기존 src strict Pyright 0 errors/0 warnings.
  Windows에는 기존 저장소 밖 IPv6 loopback bootstrap과 AnyIO assert-rewrite warning 1개가
  있고 Linux 관련 suite는 해당 bootstrap 없이 통과했다.

## 4. 실제 Core 통합 검증

현재 Core `b9a170ff35280b4bf13e0d58479f05db1cb9e8a3`와 실제 HTTP/DB 경로를 사용해 통과했다.
`sameMarket`, `crossMarket`, `drawdown`, `drawdownReplay`는 COMPLETED,
결측 fixture의 `failed`는 예상한 FAILED였다. 정상 JWT/CSRF와 Core의 내부 API key를 통해
dispatch·Compute worker·Core terminal persistence까지 기존 `run-api-smoke.ps1`을 실행했다.

최초 시도는 Core Kotlin domain 컴파일의 중복 타입 후보 오류로 HTTP 기동 전에 실패했다.
로그를 보존한 뒤 `:tradingreview:domain:compileKotlin --rerun-tasks -Pkotlin.incremental=false`로
소스 변경 없이 비증분 재컴파일했고, 새 schema·registry의 재시도에서 통과했다.
Core source/Git 상태를 변경하거나 실패 시도를 PASS로 해석하지 않았다.

성공 실행의 Compute schema는 `ar11_http_20261030t152302z`, registry는
`ar11-http-20261030t152302z`다. 실행 전후 Compute source 160개의 지문이 일치하며
완료 후 registry의 소유 프로세스를 정리했다. 최초 실패와 성공 실행 증적은 Integration의
`.integration-data/architecture-refactoring/ar11-http/` 아래 각 run ID에 보존한다.

Core smoke는 별도 `refinvest_integration` DB와 전용 Compute schema·비민감 immutable fixture,
정상 JWT/CSRF·내부 API key를 사용하는 기존 Integration harness로 실행한다. 시작한 프로세스만
registry로 소유권을 확인해 정리하며 로컬 credential·fixture·증적은 Compute에 커밋하지 않는다.
실행 절차와 환경 값은 Integration의 README/AGENTS를 정본으로 사용한다.

이 검증은 공용 인증을 통과하는 Backtest 호출 범위다. Core Analytics dispatch/publication/ACK
runtime E2E의 기존 공백을 완료 처리하지 않는다.

## 5. 남은 AR-11 작업

전역 HTTP 오류 handler는 후속 [AR-11B](HTTP_ERROR_BOUNDARY.md)에서 API 경계로 이동했다.
HTTP 관측은 [AR-11C](HTTP_METRICS_BOUNDARY.md)에서 API 경계로 옮기고 app에는 조립을 남겼다.
Ingestion·cleanup·benchmark CLI의 업무 조율과
package root 진입점·re-export 검토는 별도 범위다. 책임 이동·필수 검증을 기능별로 나누고,
실제 프로세스·계약 ownership·운영 미검증 조합을 architecture/runbook에 반영한다.
이번 PR로 AR-11 전체나 AR-08~10을 완료 표시하지 않는다.

Determinism impact: none. Point-in-Time impact: none.
