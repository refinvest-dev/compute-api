# Architecture PR 검토 순서

작성일: 2026-10-30 KST. 대상: AR-00~AR-06A까지 구현한 구조 개선.

이하 본문·수치는 PR #115 준비 당시의 머지 복구 기록이다. #115 이후 AR-07과 AR-11A~G가
별도 PR로 main에 반영됐다. 2026-11-01 KST 현재 상태와 완료 범위는
[최종 점검](ARCHITECTURE_FINAL_AUDIT.md)을 따른다. 아래의 당시 후속 목록을 현재 미머지 목록으로 읽지 않는다.

아래 PR은 단계별 변경의 검토·머지 기록이다. #103~#105는 main으로, #106~#114는
중간 브랜치 `refactor/ar02n-normalization-contracts`로 머지됐다. 중간 브랜치는 삭제됐고,
main에는 AR-02N까지만 반영되어 [통합 PR #115](https://github.com/refinvest-dev/compute-api/pull/115)로 남은 최종 구현을 반영한다.

복구 기준은 마지막 머지 커밋 `598870f`이며, 전체 파일이 검증 완료 커밋 `d415bed`와 같다.
현재 main 통합도 같은 파일을 유지한다. 이후 수정은 이 문서의 머지 경로 설명뿐이다.
기존 단계별 구현 커밋과 PR 머지 이력을 보존하며 새로운 계산 정책·기능을 추가하지 않는다.

앞으로 단계별 PR을 머지한 뒤에는 후속 PR의 base를 확인하고, 전체 완료 여부는 PR의 Merged
표시뿐 아니라 원격 main의 커밋 이력과 최종 파일 반영 여부로 확인한다.

| 단계 | PR | 범위 | 독립 단계 전체 테스트 (통과 / skip) |
|---|---|---|---|
| AR00 | [#103](https://github.com/refinvest-dev/compute-api/pull/103) | [architecture-baseline](ARCHITECTURE_BASELINE_AR00.md) | 463 / 117 |
| AR01 | [#104](https://github.com/refinvest-dev/compute-api/pull/104) | [dependency-guards](ARCHITECTURE_DEPENDENCIES.md) | 529 / 117 |
| AR02N | [#105](https://github.com/refinvest-dev/compute-api/pull/105) | [normalization-contracts](NORMALIZATION_CONTRACTS.md) | 529 / 117 |
| AR02A | [#106](https://github.com/refinvest-dev/compute-api/pull/106) | [analytics-contracts](ANALYTICS_CONTRACTS.md) | 529 / 117 |
| AR03N | [#107](https://github.com/refinvest-dev/compute-api/pull/107) | [normalization-application](NORMALIZATION_APPLICATION.md) | 548 / 117 |
| AR03A | [#108](https://github.com/refinvest-dev/compute-api/pull/108) | [analytics-application](ANALYTICS_APPLICATION.md) | 571 / 120 |
| AR04 | [#109](https://github.com/refinvest-dev/compute-api/pull/109) | [terminal-delivery](TERMINAL_DELIVERY_APPLICATION.md) | 598 / 120 |
| AR05N | [#110](https://github.com/refinvest-dev/compute-api/pull/110) | [normalization-worker](NORMALIZATION_WORKER_APPLICATION.md) | 609 / 124 |
| AR05A | [#111](https://github.com/refinvest-dev/compute-api/pull/111) | [analytics-worker](ANALYTICS_WORKER_APPLICATION.md) | 628 / 124 |
| AR05B | [#112](https://github.com/refinvest-dev/compute-api/pull/112) | [backtest-process](BACKTEST_PROCESS_ADAPTER.md) | 635 / 125 |
| AR06N | [#113](https://github.com/refinvest-dev/compute-api/pull/113) | [normalization-persistence](NORMALIZATION_PERSISTENCE.md) | 649 / 128 |
| AR06A | [#114](https://github.com/refinvest-dev/compute-api/pull/114) | [analytics-persistence](ANALYTICS_PERSISTENCE.md) | 663 / 131 |

각 단계에서 Ruff lint·format, Pyright strict, 전체 pytest, diff 검사를 통과했다.
AR-00의 포맷 변경은 AST와 주석 동일성 검증을 포함해 별도 커밋으로 분리했다.
AR-02N 이후 production source는 기존 단계별 검증 manifest의 SHA-256과 일치하도록 복원했다.
최종 소스·테스트·기존 문서는 분할 시작 시 보존한 구현과 줄바꿈 정규화 후 동일하다.
초기 PR의 진행 문서는 해당 단계에서 구현한 내용만 완료로 표시한다.

최종 전체 테스트는 663 passed / 131 skipped다. Skip은 DB URL 없이 수집된 DB 테스트 44개와
폐기된 test-only PoC 87개다. 별도 integration DB의 새 schema에서 migration 0001~0017을
autocommit 방식으로 적용하고 Normalization 27개·Analytics 10개·Backtest 7개, 총 44개 DB 테스트를 통과했다.
계약 동기화(`verify-contract-sync.ps1 -IncludeWeb`)도 통과했다.

Windows socketpair WinError 10014를 피하기 위해 저장소 외부의 opt-in IPv6 loopback bootstrap과
PGHOSTADDR=::1을 로컬 테스트에만 적용했다. HTTP 계약과 production 네트워크 설정에는 포함하지 않는다.
AnyIO assertion-rewrite warning 1건을 기록했다. 새로운 기본 환경에서 동일한 우회가 필요하다는 뜻은 아니다.

기존 단계별 보고서의 DB/Core E2E 기록은 당시의 실행 증적이다. 이번 PR 분할 검증과 구분한다.
Normalization Core publication·ACK·version pin E2E의 최신 완료 기록은
[Normalization persistence](NORMALIZATION_PERSISTENCE.md)의 재검증 절에 있다.
ORCH-027의 단일 transaction migration runner는 기존 위험이며,
실제 `psql -f` autocommit 배포 경로의 통과와 구분한다.

AR-07 Snapshot 조회, AR-08~10 Analytics 실행 제한·격리·실제 Core E2E, AR-11 최종 정리는 후속 범위다.
Analytics Core E2E 검증 공백을 Compute ASGI/DB 또는 Normalization E2E로 대체하지 않는다.

Determinism impact: none. Point-in-Time impact: none.
PR_BODY.md의 로컬 변경, credential, 실행 fixture·로그·compatibility evidence는 새 커밋에 포함하지 않는다.
