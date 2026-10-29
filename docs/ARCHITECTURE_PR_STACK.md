# Architecture PR 검토 순서

작성일: 2026-10-30 KST. 대상: AR-00~AR-06A까지 구현한 구조 개선.

아래 PR은 모두 draft이며, 첫 PR은 main, 이후 PR은 바로 앞 단계 브랜치를 base로 한다.
선행 PR부터 순서대로 검토·머지한다. 선행 PR 머지 후 후속 PR의 base/diff와 검증을 다시 확인한다.
Squash/rebase merge를 사용하면 후속 branch에서 이미 머지된 선행 커밋을 정리한 뒤 base를 바꾼다.
최종 PR만 main 기준으로 단독 머지하지 않는다. 이번 작업에서는 PR 생성까지 수행했다.

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
