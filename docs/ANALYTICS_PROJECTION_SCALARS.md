# Analytics projection scalar 재사용

2026-10-30. 범위는 `application/analytics_result.py`의 결과 구성 비용 개선이다.
AR-08의 실행 제한 정책·운영 수치 승인, AR-09/10의 프로세스 격리·연결을 완료한 작업은 아니다.

## 1. 변경과 커밋 경계

기존 `build_sections`는 metric의 `scalarInputs`를 만들고, 각 observation마다 같은 scalar
전체를 다시 UTC 변환·dict 생성·정렬한 뒤 해당 unit을 필터링했다. 관계·pair·holding observation이
늘수록 같은 작업을 반복했다. 이전 자원 실측 [PR #118](https://github.com/refinvest-dev/compute-api/pull/118)의
projection 비용을 근거로 이 부분을 별도 검토했다.

1. 변경 전 코드에서 정렬·중복·UTC 변환·결과 독립성과 전체 section bytes를 검증하는
   characterization을 먼저 커밋했다(`6f76c6e`). 관련 테스트 119개가 기존 코드에서 통과했다.
2. metric마다 scalar 변환·정렬을 한 번 수행하고, 같은 호출 안에서 unit별 목록을 구성한다.
   Observation은 필요한 unit만 조회하고 row를 복사한다. 이 구현과 전후 검증·문서를 묶는다.

인덱스는 한 `build_sections` 호출에서만 존재한다. Module/global cache나 실행 간 공유 상태를
추가하지 않는다. 반환하는 metric과 observation, 서로 다른 observation의 dict 독립성을 보존한다.
Scalar의 Python 문자열 정렬과 ID set의 UTF-16 정렬은 다르므로, 여러 unit의 scalar를 모을 때
기존 Python 순서를 유지한다. 중복 row도 제거하지 않는다. Unicode ID의 이 차이와 offset timestamp의
UTC 변환, 호출자 mutation·후속 호출 독립성을 직접 검증한다.

정규화·Engine의 후보 선정, AFTER_WINDOW 감사 후보, Evidence·값·정렬/hash 규칙은 유지한다.
API·worker 조율·SQL·migration·설정·OpenAPI·Context golden fixture는 변경하지 않는다.

## 2. 전후 비교 방법

- 기준 main은 `b55f8886b8c14f66fb49268ff7d764eeb2034288`이다. PR #118은 측정 시점에 미머지다.
  그 PR의 [합성 workload와 측정 함수](https://github.com/refinvest-dev/compute-api/blob/656c83eee58b227d727264d6037d8f29d9bd7913/tools/analytics_resource_baseline.py)를
  외부 개발 검증에 사용했다. 이 PR의 서비스·저장소 테스트에는 그 도구 의존성을 추가하지 않았다.
- 변경 전 projector를 별도 파일로 보존했다. 새 프로세스에서 같은 production module의
  `build_sections`만 기존 함수로 교체해 before를 실행하고, after는 실제 수정된 함수를 실행했다.
  나머지 downloader·strict parser·adapt·Engine·integrity·직렬화·AES-GCM 코드는 같다.
- Windows build 26200 / CPython 3.11.5 64 bit / 논리 CPU 16개의 같은 공유 개발 호스트다.
  각 profile을 새 프로세스에서 3쌍 실행하되 before/after, after/before, before/after 순서로
  번갈아 측정했다. Generator는 측정 자식 밖에서 실행했다.
- 시간 표본에는 profiler를 켜지 않았다. 별도의 128 episode cProfile 실행에서
  `_scalar_inputs` 호출이 **259회→3회**로 감소함을 확인했다. 이 두 실행은 아래 시간 집계에서 제외했다.
- 모든 36개 완료 표본에서 profile별 inputHash·resultHash·전체 평문 body SHA-256이 같았다.
  고정 job/time과 **합성 VersionSet**을 사용한 같은 pin의 비교다. 실제 배포 artifact의 새
  implementationDigest를 과거 값으로 위장하거나 새 digest 간 hash 동일성을 주장하지 않는다.
- Read·content 검증·parse부터 AES-GCM까지를 pipeline으로 측정했다. 실제 network·DB·IPC와
  import/spawn 시간은 이 pipeline 시간에 포함하지 않는다. RSS는 OS process high-water mark다.

원본 projector 파일 SHA-256은 `4349575d86c3fe06d1610ac9d208cbf76b040b26348957be63454fdea765d8aa`,
수정 파일은 `9339fb713cf773adc46e1b29e0e053565a8909efb4a779dafcf51cb915a9f70e`다.
이는 로컬 파일 지문이며 production implementationDigest가 아니다.

## 3. 측정 결과

시간은 3회의 중앙값, 메모리는 3회 중 최대 working set이다. MiB=1,048,576 bytes다.

| Profile | 후보 수 | 결과 MiB | Projection 초: 전→후 | Pipeline 초: 전→후 | Peak RSS MiB: 전→후 |
|---|---:|---:|---:|---:|---:|
| dense32 | 256 | 0.265 | 0.028→0.008 | 0.136→0.127 | 66.7→66.7 |
| dense128 | 4,096 | 1.757 | 0.365→0.059 | 1.059→0.674 | 80.3→78.3 |
| dense256 | 16,384 | 5.420 | 1.349→0.224 | 3.127→2.013 | 106.9→106.3 |
| dense512 | 65,536 | 18.489 | 5.296→0.816 | 12.645→8.134 | 194.6→195.4 |
| spaced256 | 16,384 | 4.777 | 1.261→0.186 | 2.490→1.394 | 102.2→101.4 |
| evidence256x8 | 16,384 | 5.931 | 1.337→0.284 | 3.471→2.422 | 126.9→128.4 |

Dense는 2초 간격, spaced는 1시간 간격의 손실·이익 교대 episode다. Episode마다 OPEN/CLOSE가
있고, evidence profile만 record당 참조가 8개다. 모든 input/output bytes·후보 수가 유지됐다.
Dense512 projection 범위는 전 5.100~5.359초, 후 0.809~0.839초였다. 이 profile의 중앙값 기준
projection 비용은 약 84.6%, pipeline은 약 35.7% 감소했다.

메모리 감소를 보장하는 변경은 아니다. Dense512와 Evidence profile은 소폭 증가했다.
결과 tree와 직렬화·암호화 사본을 보유하는 현재 구조, 후보 수의 증폭도 남아 있다.
측정 환경·표본 수가 제한돼 p95/SLA나 production timeout·메모리 한도를 추정하지 않는다.
PR #118의 이전 실행 시간과 다른 시점의 값을 직접 나눠 개선율을 계산하지 않았다.

## 4. 검증과 통합 한계

- Ruff/formatter 255 files, 기존 src strict Pyright 0 errors/0 warnings.
- 변경 전후 관련 suite 119 passed. 전체 suite **684 passed / 131 skipped**.
  Skip은 실행하지 않은 환경 의존/기존 PoC 검사이며 통과로 세지 않는다.
- `verify-contract-sync.ps1 -IncludeWeb` 전후 통과. Context/복제 OpenAPI·fixed vector를 보존했다.
- 분리된 `refinvest_integration` DB의 새 전용 schema에 migration 0001~0017을 적용하고
  Analytics 실제 PostgreSQL suite **10 passed / skip 없음**을 확인했다.
- 별도 32 episode smoke는 실제 provider·ASGI HTTP·worker·PostgreSQL 암호화 저장·descriptor·
  payload 조회·PERSISTED ACK·payload 제거까지 통과했다. 세 metric AVAILABLE, observation 64개,
  membership 206개·relation 16개, wire schema·bytes SHA-256을 확인했다. Artifact 다운로드는
  합성 고정 입력으로 대체했다. 실제 TCP·Core dispatch를 실행한 검증은 아니다.
- 최초 DB smoke에서는 offline benchmark의 2099년 grant를 실제 API가 INPUT_REFERENCE_EXPIRED로
  거부했다. 실패 로그를 보존하고 실제 grant 계약에 맞는 4분 만료로 수정한 뒤 새 schema에서
  다시 통과했다. 이 과정에서 production admission·grant 정책은 수정하지 않았다.
- Windows 네트워크 회귀는 저장소 밖의 기존 IPv6 loopback bootstrap을 사용했다.
  pytest의 기존 AnyIO assert-rewrite warning 1개를 기록했다.

현재 확인한 Core checkout `421951f3f4f1556f7e5030b5fa10c48d03c14f7f`에는 Analytics 입력 생성·
artifact adapter가 있지만 Analytics dispatch/publication/ACK runtime과 Integration E2E harness가
없다. **실제 Core→Compute Analytics E2E는 NOT_RUN**이다. 위 Compute ASGI/DB smoke를 그 통과로
해석하지 않는다. 이 통합 gate는 PR 검토 시 열린 항목이며, 다른 서비스의 코드를 선제 구현해
이번 성능 변경에 섞지 않는다.

로컬 합성 입력·36개 원본 표본·실패/성공 로그·JUnit·DB smoke 증적은 저장소 밖
`refinvest-projection-optimization`에 보존한다. Credential·PR_BODY.md·검증 산출물은 커밋하지 않는다.

## 5. 다음 검증

지원 Linux 배포 환경·최대 허용 input/output·실제 전송·DB 경합·동시 worker의 자원 profile을
확인하고, Core Analytics runtime/harness를 연결해 실제 dispatch부터 publication/ACK를 검증한다.
그 자료로 AR-08 수치를 리뷰하고 AR-09/10의 저장 준비·격리·연결 순서를 진행한다.
이번 개선으로 T/G/S/P·OS hard limit·worker 동시성을 정하거나 AR-08을 완료로 체크하지 않는다.

Determinism impact: none. Point-in-Time impact: none.
