# AR-11C — HTTP 계측 경계

2026-10-31. `app/main.py`의 request middleware와 operation/status 분류를
`api/http_metrics.py`로 옮긴다. 기존 HTTP 응답과 관측 의미를 유지하는 구조 정리다.
Context의 ADR-053과 `docs/PERFORMANCE_OBSERVABILITY.md`의 bounded label·민감정보 금지 원칙을 따른다.

## 1. 책임과 연결

- API의 `install_http_metrics`가 기존 방식으로 HTTP middleware를 등록한다.
  `_operation`, `_record_http`, 요청 시간 측정·finally 기록 흐름을 함께 소유한다.
- App은 `ApiMetrics` registry 생성, `application.state.metrics`, `serve_metrics` lifespan과
  설정·route·예외 handler 조립을 유지한다. 이전 `_operation`·`_record_http`·`monotonic`의
  app 경로나 re-export를 남기지 않는다.
- App이 `lambda: get_settings().api_metrics.enabled`를 전달한다. 실행 시작에 enabled를 고정하지
  않고 기존처럼 요청 완료 시점에 조회한다. API 모듈은 app/config에 역참조하지 않는다.
- Collector·histogram bucket·안전한 기록·loopback listener 구현은 기존 observability 모듈에
  유지한다. 새 관측 port나 middleware class를 만들지 않는다.

## 2. 유지한 동작

| 관측 요소 | 기존 동작과 보존 범위 |
|---|---|
| Operation | submit, status, assets, series, trading_review, other의 기존 method/path 분류 |
| Health | `/health`는 제외하며 요청 계측의 enabled callback도 호출하지 않음 |
| Status | 응답의 2xx/3xx/4xx/5xx, 예상하지 않은 예외는 기존 500 기본값으로 finally 기록 |
| 시간 | monotonic 시작부터 call_next의 응답 생성/오류까지; streaming body 전체 전송 시간으로 확장하지 않음 |
| 비활성화 | HTTP counter·duration 표본을 추가하지 않음; 요청 응답은 그대로 반환 |
| 계측 실패 | 기존 safe_record의 안전한 warning만 남기고 응답 status/body/header를 유지 |
| Registry | 앱별 registry 분리, 기존 opt-in listener 시작·종료와 occupied-port 실패 유지 |
| 민감정보 | raw path·query·job/run ID·API key·예외 상세를 metric label로 사용하지 않음 |

기존 `/assets`·`/series` prefix 분류와 `/backtests/` slash 수 판정도 유지한다. 예를 들어
`/assets-shadow`는 assets 관측 그룹이다. 이 분류는 HTTP route 매칭이나 인증의 근거가 아니다.
`OBSERVABILITY.md`에 기존 trading_review label과 prefix 동작을 명시한 것은 runtime 정책 변경이 아니다.
설정 조회 자체가 실패하는 경우를 새로 삼키거나, partial metric 기록을 원자적으로 만드는 정책은 추가하지 않는다.

## 3. 커밋과 의존성 검사

1. 이동 전 코드에 28개 HTTP 관측 계약 사례를 추가하고 관련 suite 218개를 통과시킨 뒤
   `5cfc6c8`로 먼저 커밋했다. 실제 HTTP 응답과 collector 표본을 확인하며 listener는 해당
   테스트에서만 비활성화한다. 기존 scrape/opt-in/occupied-port 테스트는 실제 listener로 실행한다.
2. Middleware 이동·설정 콜백 연결, clock monkeypatch 사용 경로와 의존성 검사·문서를 묶는다.
   금지 import 사례 6개를 추가한다. API 모듈은 framework와 기존 `ApiMetrics`·`safe_record`만
   사용하며 app·application·persistence·route·provider·직접 네트워크·listener 조립을 금지한다.
   App의 기존 clock·safe_record 직접 import도 차단한다. Legacy 예외는 추가하지 않는다.

## 4. 검증 결과

- `_operation`·`_record_http` AST 동일. Middleware는 enabled 설정 표현식을 명시적 콜백으로
  바꾼 것 외 AST 동일. 해당 middleware 설치를 제외한 `create_app`·lifespan·CLI main AST도 동일하다.
- Runtime OpenAPI 전후 44,951 bytes와 SHA-256
  `fa9da4fa83f1bc4b222165346d6dea807515a60355a15293d97cbd62429825af` 동일.
- 이동 후 Windows 관련 suite **224 passed**, native Linux 관련 suite **224 passed**.
  Ruff·formatter 265 files, src strict Pyright 0 errors/0 warnings와 전후 contract sync `-IncludeWeb` 통과.
- Windows 전체 재실행 **804 passed / 131 skipped**, AnyIO assert rewrite 경고 1개.
  첫 전체 실행은 기존 `test_process_executor_times_out_when_child_does_not_exit_after_sending_result`의
  3초 제한 안에 sent marker가 생성되지 않아 1 failed / 803 passed / 131 skipped였다.
  실행기·해당 테스트는 main 대비 변경이 없고 단독 3회는 모두 통과했다. 최초 실패 원인은
  확정하지 않았으며 deadline·테스트를 완화하지 않았다. 원본 실패 `first-full.log/xml`과
  `process-recheck-1/2/3.log`, 전체 재실행 결과를 로컬 검증 디렉토리에 함께 보존한다.
- Core `9106c26bb1080ed8fd5d66aec365361de358e153`의 현재 source를 Gradle로 기동해
  JWT/CSRF → Core dispatch → Compute worker → Core terminal persistence smoke를 통과했다.
  Same-market·cross-market·drawdown·replay는 COMPLETED, 의도한 달력 오류는
  CALENDAR_RESOLUTION_FAILED를 유지한 FAILED다. Run ID는 `20261031T051118Z`이며
  로컬 integration `.integration-data/architecture-refactoring/ar11-metrics/20261031T051118Z/summary.json`에
  PASS 증적을 보존했다. 분리 DB `refinvest_integration`, 전용 Compute schema·immutable fixture를
  사용했고 종료 후 해당 registry는 비어 있다. Core source는 변경하지 않았고 실행 전후 revision과
  clean 상태를 확인했다.

Windows 검증은 기존 외부 IPv6 bootstrap과 AnyIO assert rewrite 경고 1개를 포함한다.
Native Linux는 bootstrap 없이 실행한다. Skip은 통과로 계산하지 않는다.
실행 당시 Compute source 지문과 최종 source를 대조하며 credential·fixture·로컬 증적과
기존 PR_BODY.md는 커밋하지 않는다.

## 5. 후속 범위

Ingestion·cleanup·benchmark CLI 업무 조율, package root 진입점/re-export 검토와 최종
architecture/runbook 정리는 별도 PR로 진행한다. AR-11 전체를 완료 처리하지 않는다.
ADR-071 DRAFT의 실행 제한 수치, production process 격리와 Core Analytics runtime E2E gate는
이번 HTTP 계측 경계 정리로 해소되지 않는다.

현재 package root `__init__.py`는 `app/main.py`를 즉시 import한다. 개별 계산·worker 모듈의
import도 HTTP 조립을 동반하는 기존 부수 효과는 남아 있다. 공개 package 진입점 호환성과
spawn 경로를 함께 검토할 별도 범위이며, 위의 최초 프로세스 테스트 실패 원인으로 단정하지 않는다.

Determinism impact: none. Point-in-Time impact: none. Temporal/Execution impact: none.
