# AR-11G — Reference-data 진단 경계

2026-10-31. 배포 진단의 snapshot 선택·catalog coverage·최근 cross-calendar series 검증을
`application/reference_data_diagnostics.py`로 이동한다. Context ADR-010의 immutable snapshot과
ADR-046의 snapshot-backed series·결측 fail-fast를 보존하며 API 계약을 변경하지 않는다.

## 1. 책임과 보존 범위

Application은 `DatasetSnapshotRepository`와 지연 `SnapshotExecutionDataLoader` factory를 받는다.
명시한 ID 또는 기존 truthy 규칙에 따른 latest를 한 번 선택한 후 loader를 생성한다. 알 수 없는
ID를 latest로 대체하지 않는다. 동일 snapshot으로 MVP 5개 자산의 availability를 검사한 뒤
QQQ/BTCUSDT 중 더 이른 마지막 세션을 end로, 그 7일 전을 start로 PRICE series를 검사한다.
이 운영 진단 창은 Backtest의 signal·lag·execution 규칙을 새로 정의하지 않는다.

App에는 DB 설정 검증과 concrete repository·storage 조립, argparse·compact JSON 출력·exit를
유지한다. 기존 public 함수 signature/default, console target, parser와 console 함수 AST가 같다.
선택 후 factory 실행 순서, 원래 예외 전파, 누락 coverage·빈 series·expected close 결측 오류를
유지한다. 5개 availability와 window·series를 위한 기존 loader 호출 7회도 보존하며 중복 로딩
최적화는 섞지 않는다. 이전 private helper를 app에서 재수출하지 않는다.

전체 application 의존성 규칙에 더해 진단 모듈은 assets·dataset_snapshots·series·
snapshot_execution_data와 표준 라이브러리만 허용한다. CLI parser·JSON·파일 직접 처리와
app/DB/SDK 의존을 차단한다. App의 asset·series 조율 및 날짜 창 계산 재유입도 검사한다.
금지 import 회귀 사례 7개와 허용 사례를 추가하며 legacy 예외는 추가하지 않는다.

## 2. 커밋과 검증

1. 이동 전 구현에 계약 20개를 추가해 관련 185개·실제 filesystem 진단을 통과한 뒤
   `6a30c87`로 먼저 커밋했다. Snapshot 선택·factory 순서·동일 객체 재사용, 결측 오류,
   parser 조기 종료·한 줄 JSON·설정/실행 실패의 상세 비노출과 exit 1을 고정했다.
2. Application 이동·app 연결·정적 경계·문서를 함께 묶는다.

- 분리 후 Windows 관련 **192 passed**, Ruff·formatter 272 files·src strict Pyright 통과.
- 전체 Windows 회귀 **909 passed / 131 skipped**, 기존 AnyIO assert rewrite 경고 1개.
- Native Linux 관련 **192 passed**. Skip은 통과에 포함하지 않는다.
- 실제 filesystem publisher·manifest 검증 loader로 고정된 비운영 5자산 snapshot을 검사했다.
  명시 ID/latest 진단 결과가 같고, 이동 전후 결과 전체와 artifact 3개의 bytes/hash가 동일하다.
  진단 전후 파일도 변경되지 않는다. 저장 경로는 출력에 포함되지 않으며 vendor·DB가 필요 없다.
- Runtime OpenAPI 전후 44,951 bytes, SHA-256
  `fa9da4fa83f1bc4b222165346d6dea807515a60355a15293d97cbd62429825af` 동일.
- 병합된 Core main `c51d9cd9cd48029283316bc2b513ed32a63e4cdf` 고정 E2E 통과:
  실제 진단 console은 ok, 정상 4개는 COMPLETED, 의도한 CALENDAR_RESOLUTION_FAILED는 FAILED다.
  실행 ID `20261031T133152Z`, 증적은 integration의
  `.integration-data/architecture-refactoring/ar11-reference/20261031T133152Z/summary.json`이다.
  Compute source는 실행 전후 동일하며 종료 후 전용 process registry는 비어 있다.
  성공 실행의 Core 기동은 Windows socket listen 오류 1회 후 정본 도구의 두 번째 시도에서 READY였다.

E2E는 병합된 Core main을 별도 checkout에 고정하고 격리 integration DB의 전용 Core·Compute
schema를 사용한다. 실제 진단 console이 published fixture를 읽는 경로도 실행한다. 임시 smoke
wrapper는 정본 API 검증 항목을 유지하며 Core schema·identity·seed 대상을 맞춘다. Integration
저장소 script·계약·fixture는 수정하지 않는다. Windows는 기존 외부 IPv6 bootstrap을 사용하며
Linux에서는 사용하지 않는다. Credential·fixture·실행 증적과 PR_BODY.md는 커밋하지 않는다.

첫 전체 회귀는 기존 `test_benchmark_watchdog_stops_descendants`와
`test_process_executor_times_out_when_child_does_not_exit_after_sending_result`가 실패해
907 passed / 2 failed / 131 skipped였다. 첫 E2E(`20261031T131638Z`)와 순차 재실행
(`20261031T132458Z`)은 실제 진단 console을 통과했으나 API 진행 중 curl 연결 실패로 종료됐다.
테스트 실패와 요청 중 연결 실패의 원인은 확정하지 않았으며, 이후 확인된 기동 시 socket 오류와
동일한 원인으로 단정하지 않는다. 실행 코드·테스트·시간 제한을 수정하지 않고 실패 테스트 2개를
단독 3회 통과한 뒤 전체 회귀와 위 E2E를 통과했다. 실패 로그·JUnit·E2E 증적은 별도로 보존한다.

## 3. Cleanup 검토와 남은 범위

Cleanup 진입점 3개는 설정·DB 조립·단일 repository 호출·완료 로그만 맡는다. Backtest의 retention
단위 전달, Normalization의 ACK·retention, Analytics의 payload/job 정리는 기존 구현에 둔다.
App에 별도 삭제 판단이나 반복 실행 정책이 없어 범용 cleanup service·port를 추가하지 않는다.
정책·쿼리·DB clock·배치 크기·console target·systemd 파일은 변경하지 않는다.

Fixture 도구 ownership·남은 re-export와 최종 architecture/runbook 전체 검토는 후속 범위다.
AR-11 전체를 완료 표시하지 않는다. ADR-071은 DRAFT이며 Analytics 운영 격리·실행 한도와
실제 Core Analytics runtime E2E gate는 계속 남아 있다.

Determinism impact: none. Point-in-Time impact: none. Temporal/Execution impact: none.
