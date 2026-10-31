# AR-11E — Benchmark CLI 조율 경계

2026-10-31. `app/benchmark.py`의 입력 로딩→실행→report 저장 조율을 `benchmarking/runner.py`로
옮긴다. ADR-053과 Context `PERFORMANCE_OBSERVABILITY.md`의 고정 입력·계산 hash·correctness
gate를 유지하며 공식 성능 baseline이나 성능 개선을 주장하는 작업은 아니다.

## 1. 책임과 선택 범위

App은 argparse 옵션·검증, Engine version 조회, filesystem/verified loader·executor 조립,
stderr/stdout와 종료 코드를 소유한다. Runner는 fixture·scenario, version supplier·executor
factory 호출, optional expected 로딩, 시작·종료 시각, 측정·report 구성과 atomic write를 조율한다.
Version은 입력 로딩 후 기존 순서로 조회하며 factory의 기존 생성 순서도 유지한다.
Runner의 결과는 scenario ID·기존 MeasurementResult·passed로 구성한다.

`run_warm_benchmark_report`는 warm 실행만 제공한다. CLI는 기존처럼 process-cold를 실행 전에
거부한다. 새 cache mode나 기본값, fallback은 추가하지 않는다. 입력 schema·strategy validation,
measurement·canonicalization·report writer는 변경하지 않는다. Benchmark의 기존 API DTO 재사용도
이번 범위 밖이다. 새 generic runner·job framework·port 계층을 만들지 않는다.

Cleanup CLI는 현재 설정 검증·repository 조립과 기존 cleanup 호출이 중심이므로 이번에는
단순 전달 계층을 추가하지 않았다. Retention·payload/job purge·ACK·lease와 SQL 원자성은 그대로
repository에 남긴다. Ingestion의 coverage/override·발행 조율은 별도 PR로 검토한다.

## 2. 커밋과 동작 보존

1. 기존 코드에 CLI 계약 13개를 추가하고 관련 suite 155개를 통과한 뒤 `bbd735c`로 먼저 커밋했다.
   기본/명시 인자, expected 유무, 조립·호출 순서, 정확한 stdout/stderr와 gate 실패·pipeline 오류,
   invalid option·help·필수 인자 누락의 실행 차단을 확인한다.
2. 실행 조율 이동과 실제 monkeypatch 사용 모듈 전환, 의존성 검사·문서를 묶는다.
   Runner의 app·API·persistence·ingestion·argparse·직접 네트워크·subprocess와 app의 직접
   입력 로딩·측정·report 의존을 차단한다. 금지 import 사례 7개와 허용 사례를 추가한다.

| 경계 | 유지한 동작 |
|---|---|
| 정상 종료 | report를 먼저 저장하고 기존 scenario/hash/실측 iteration 수를 stdout에 출력 |
| Correctness 불일치 | report 저장 후 기존 stderr message와 exit 1, 성공 출력 없음 |
| 입력·실행·report 오류 | 기존 오류 종류·문구와 exit 2, 성공 출력 없음 |
| Parser 오류 | process-cold·음수 warmups·0 이하 iterations·누락 인자를 workflow 전에 거부 |
| 측정 | 기존 perf_counter_ns·warmup 제외·stage·hash·메모리 측정 구현과 시작/종료 경계 |
| 저장 | 기존 atomic report write, 출력 경로·report format·expected 정책 유지 |
| Runtime | API·worker·Engine·OpenAPI·migration·배포 target 변경 없음 |

## 3. 검증 결과

- Windows 관련 suite **162 passed**, native Linux 관련 suite **162 passed**.
  Windows 전체 **857 passed / 131 skipped**, 기존 AnyIO assert rewrite 경고 1개.
  Ruff·formatter 268 files, src strict Pyright 0 errors/0 warnings. 전후 contract sync
  `-IncludeWeb` 통과. Windows는 기존 외부 IPv6 bootstrap을 사용하며 Linux는 사용하지 않는다.
- Parser·version helper AST와 HTTP 구현 bytes를 보존했다. Runtime OpenAPI 전후 44,951 bytes와
  SHA-256 `fa9da4fa83f1bc4b222165346d6dea807515a60355a15293d97cbd62429825af` 동일.
- Core의 커밋된 HEAD `c51d9cd9cd48029283316bc2b513ed32a63e4cdf` 격리 worktree에서
  JWT/CSRF → Core dispatch → Compute worker → Core terminal persistence smoke를 통과했다.
  정상 4개는 COMPLETED, 의도한 달력 오류는 CALENDAR_RESOLUTION_FAILED를 유지한 FAILED다.
  Run ID `20261031T084526Z`의 PASS 증적은 로컬 integration
  `.integration-data/architecture-refactoring/ar11-benchmark/20261031T084526Z/summary.json`에 보존한다.
  분리 DB `refinvest_integration`과 전용 Compute schema·immutable fixture를 사용했고 종료 후
  해당 registry는 비어 있다. 최초 Core 준비는 기존 작업트리의 미커밋 변경을 감지해 기동 전에
  중단했으며 해당 변경은 수정·stash하지 않았다. 검증한 것은 격리된 committed HEAD이며
  기존 작업트리의 미커밋 코드를 검증한 것으로 해석하지 않는다.
- 고정 fixture 여섯 scenario가 변경 전후 각각 Smoke gate를 통과했다. Scenario·fixture identity,
  canonicalization version, 계산 result hash·terminal status·trade count·sample warning과
  expected 검사·mismatch 유무가 모두 일치한다. Zero Trades도 COMPLETED/0/ZERO를 유지한다.
  증적은 로컬 integration의 다음 경로에 보존했다.
  - 전: `.integration-data/architecture-refactoring/ar11-benchmark/before/20261031T081638Z-compute-5b51d9e2/baseline.json`
  - 후: `.integration-data/architecture-refactoring/ar11-benchmark/after/20261031T084144Z-compute-bbd735cb/baseline.json`

고정 integration fixture의 여섯 scenario를 전후 Smoke 모드(0 warmups·1 measured iteration)로
검증하며 correctness hash·terminal/trade count·warning을 비교한다. 시각·revision·측정 시간 등
operational metadata까지 report 전체 bytes가 같다고 주장하지 않는다. 이 실행은 공식 성능
baseline이 아니며 elapsed time 비교로 최적화 효과를 주장하지 않는다.

첫 변경 전 harness 기동은 Windows PowerShell의 기본 경로 인자 평가에서 실패했다. 실패 로그를
보존하고 환경 파일·sibling checkout 경로를 지원 인자로 명시해 재실행했다. Integration script나
fixture·expected 값을 고쳐 통과시키지 않았다. Credential·로컬 fixture/report/evidence와 기존
PR_BODY.md는 커밋하지 않는다.

## 4. 남은 범위

AR-11 전체는 미완료다. Ingestion 조율, cleanup의 실제 필요 경계와 남은 re-export 검토,
architecture/runbook 최종 정리는 후속 범위다. ADR-071은 DRAFT이며 Analytics의 운영 격리·실행
한도·실제 Core Analytics runtime E2E gate를 이번 CLI 정리로 완료 처리하지 않는다.

Determinism impact: none. Point-in-Time impact: none. Temporal/Execution impact: none.
