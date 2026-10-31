# AR-11F — Snapshot Ingestion CLI 경계

2026-10-31. Snapshot CLI의 입력 해석·발행 lifecycle을 `ingestion/snapshot/cli.py`로 옮기고
`app/snapshot_ingestion.py`에는 설정·기술 구현 조립과 console을 남긴다. Context ADR-010의
immutable DatasetSnapshot과 ADR-051의 archive provenance 규칙을 바꾸지 않는다.

## 1. 책임과 연결

Feature CLI adapter는 argparse, UTC/date·coverage 검증, 안전한 ID·nonce, override JSON,
MvpSnapshotRequest metadata, 발행 시작·종료·실패 로그와 compact JSON 출력을 소유한다.
App은 Settings에서 명시적 설정값을 전달하고 clock/nonce와 storage path/coordinator callback을
연결한다. Coordinator factory는 validation·override 읽기·storage path 해석 후 시작 이벤트가
기록된 다음에 호출한다. Factory를 미리 실행해 잘못된 입력으로 SDK·DB 객체를 만들지 않는다.

기존 `run_snapshot_ingestion(arguments, settings=..., now=..., snapshot_nonce=...,
coordinator_factory=...)`의 signature·default와 console target은 유지한다. 새로운 feature 모듈은
Settings나 app을 import하지 않는다. Feature는 CLI adapter이므로 argv·파일 입력·JSON·로그를
사용하며 순수 domain이라고 주장하지 않는다. 새 job framework나 포괄 adapter 계층은 추가하지 않는다.

## 2. 보존한 동작

| 경계 | 보존 내용 |
|---|---|
| Clock·coverage | 정확한 UTC 확인, configured start·전일 end 기본값, start/end·미완료 일자 거부 |
| Snapshot ID | 수동 ID 우선·nonce 미소비, 자동 ID 형식과 안전한 identifier 검증 |
| Overrides | CLI 경로 우선, UTF-8 object/date/null/정확한 UTC timestamp 검사, 임의 보정 없음 |
| 발행 | source·adjustment·coverage·createdAt·integrity flags, 기존 Mvp coordinator 호출 |
| Artifact·metadata | 기존 artifact 발행 후 metadata 등록, manifest 검증·immutable 경로 재사용 거부 |
| 관측 | 기존 logger `refinvest.app.snapshot_ingestion`, JSON 필드·순서·level·monotonic duration |
| 실패 | 시작 후 factory/vendor 오류는 type만 기록하고 동일 예외 재전파, console은 traceback 없이 exit 1 |
| Parser·stdout | help/잘못된 옵션의 SystemExit, 한 줄 결과 JSON·alias·정렬/compact 형식 유지 |

`_parser`, `_automatic_snapshot_id`, `_date_argument`, `_official_overrides`, `_event`의 함수 AST와
MVP 상수·ID regex는 동일하다. Coordinator 조립·console 함수 AST와 public run signature도 같다.
Run 본문과 configured-start helper는 Settings 속성·storage/factory 호출을 명시적 값·callback으로
바인딩한 차이 외 AST가 같다. Logger는 이동 전 이름을 명시해 기존 journal 필터를 유지한다.
기존 로컬 publication 관측 메모는 운영 동작 보존 근거로만 참조하며 Context ADR 번호를 재정의하지 않는다.

## 3. 커밋과 의존성 검사

1. 이동 전 코드에 계약 16개를 추가하고 관련 suite 191개·오프라인 발행을 통과한 뒤
   `e69fb2f`로 먼저 커밋했다. UTC·override 실패 시 factory 미호출, parser exit·callback 순서,
   반환된 published identity, 실패 로그 비노출과 실제 filesystem artifact 읽기·불변성을 검사한다.
2. 코드 이동·clock monkeypatch 사용 모듈, 정적 경계·문서를 묶는다. 금지 import 사례 9개를
   추가하며 feature의 app·API·DB·Engine·vendor/calendar 직접 의존·네트워크를 차단한다.
   App의 parser·JSON·path·regex·publication timer와 MvpSnapshotRequest 직접 구성을 검사한다.
   현재 legacy 예외는 추가하지 않는다.

## 4. 검증 결과

- Windows 관련 **200 passed**, native Linux 관련 **200 passed**.
  전체 **882 passed / 131 skipped**, 기존 AnyIO assert rewrite 경고 1개.
  Ruff·formatter 270 files, src strict Pyright 0 errors/0 warnings. 전후 contract sync `-IncludeWeb` 통과.
- 실제 calendar 생성기·Mvp coordinator·filesystem publisher와 비운영 vendor fixture를 연결해
  metadata callback에서 loader 검증을 완료했다. 전후 `calendar.json`, `daily-bars.ndjson`,
  `manifest.json` bytes/hash와 논리 snapshot metadata가 같다. 서로 다른 격리 저장 위치의
  `storage_path`는 비교에서 제외했다. Source·coverage·provenance·integrity와 ID는 비교한다.
- Runtime OpenAPI 전후 44,951 bytes, SHA-256
  `fa9da4fa83f1bc4b222165346d6dea807515a60355a15293d97cbd62429825af` 동일. HTTP 구현도 변경하지 않는다.
- 병합된 Core main `c51d9cd9cd48029283316bc2b513ed32a63e4cdf`를 고정한 추가 E2E도 통과했다.
  정상 4개는 COMPLETED, 의도한 CALENDAR_RESOLUTION_FAILED는 FAILED다.
  실행 ID는 `20261031T103215Z`, 증적은 integration의
  `.integration-data/architecture-refactoring/ar11-ingestion-main/20261031T103215Z/summary.json`이다.
  Compute source hash는 검증 전후 동일하고 해당 실행의 전용 process registry는 비어 있다.

첫 Core HEAD 검증은 `1d64039e668f76e3bf964b3bac5227b37008dc1d` 조합에서 정상 4개·의도한 실패
1개를 통과했다(`20261031T094559Z`). 이후 이 HEAD가 원격 main에 없는 migration을 포함함을
확인했다. 중지 요청 시에는 해당 검증이 이미 종료돼 있었다. 이 결과를 merged-main 검증으로
표시하지 않으며 실제 commit·실행 증적을 별도로 보존한다.

추가 main 검증은 Core·Compute의 전용 schema를 사용한다. 정본 `run-api-smoke.ps1`의 API 검증
항목을 유지한 임시 wrapper에 Core schema 인자와 identity·seed 대상을 명시했다. Integration
저장소 script·OpenAPI·fixture를 수정하거나 기존 public schema migration을 삭제·되돌리지 않았다.
Windows는 기존 외부 IPv6 bootstrap을 사용하고 Linux는 사용하지 않는다. Skip을 통과로 계산하지
않으며 vendor 실계정·production DB/S3를 사용하지 않는다. 로컬 credential·fixture·evidence와
기존 PR_BODY.md는 커밋하지 않는다.

## 5. 남은 범위

Cleanup·diagnostic CLI의 실제 필요 경계, 나머지 re-export·최종 architecture/runbook 검토는
후속 개별 범위다. AR-11 전체를 완료 표시하지 않는다. ADR-071은 계속 DRAFT이며 Analytics
운영 격리·실행 한도·Core Analytics runtime E2E gate는 이번 이동으로 해소되지 않는다.

Determinism impact: none. Point-in-Time impact: none. Temporal/Execution impact: none.
