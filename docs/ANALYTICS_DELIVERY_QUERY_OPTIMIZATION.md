# Analytics 전달 메타데이터 조회 최적화

2026-11-01 KST. [발행 경계 실측](ANALYTICS_PUBLICATION_RESOURCE_BASELINE.md)에서 확인한
descriptor·ACK의 불필요한 encrypted payload 조회를 제거한다. 기존 Result Delivery V1과
ACK receipt·만료·잠금·payload endpoint의 동작은 보존한다.

## 1. 변경과 보존 경계

`result_descriptor`는 기존 descriptor·expires_at과 함께 payload의 존재 여부를 SQL boolean으로
받는다. `octet_length(encrypted_bytes)>0`은 NULL뿐 아니라 zero-length BYTEA도 falsey로 다루던
기존 동작을 보존한다. 먼저 만료를 검사한 뒤 존재 여부를 판단하는 순서를 유지한다.

`acknowledge`의 payload row 조회는 실제 사용하는 descriptor·expires_at으로 한정한다.
Job→receipt→payload의 조회·잠금 순서와 `FOR UPDATE`, DB clock, fingerprint·identity 검증,
receipt insert와 encrypted bytes NULL update의 transaction 경계는 유지한다. 같은 ACK는
만료 후에도 기존 receipt로 재전송을 판정하고, 변경된 ACK는 충돌한다.

실제 payload 다운로드의 encrypted BYTEA 조회·복호화는 유지한다. 이 변경은 새 endpoint,
migration·공개 schema·설정·timeout·worker isolation·계산 정책을 도입하지 않는다.

## 2. 검증 방법

`tests/integration/test_postgres_analytics_delivery.py`는 실제 cursor의 결과 type OID로
descriptor/ACK에 BYTEA가 반환되지 않고 실제 payload 다운로드에는 반환되는지 검증한다.
SQL 문구 자체의 일치 여부를 테스트하지 않는다. NULL/empty/저장 상태별 만료 우선순위와
ACK replay, 동시 동일 ACK·서로 다른 유효 ACK의 단일 receipt/충돌을 검증한다.

변경 전 main `4c245dc`에서는 새 검사 10개 중 BYTEA projection 검사 2개가 실패하고
기존 의미론 검사 8개가 통과했다. 이 실패는 최적화 대상의 재현이며 성공 실측 표본으로 세지 않는다.
같은 테스트와 기존 durable/API/worker 회귀를 변경 후 실제 DB에서 다시 실행한다.

## 3. 실측과 후속 gate

기준 코드의 별도 사본과 변경 코드를 같은 Python·호스트·DB·합성 준비 문서·기존 측정 도구로
비교한다. 순서는 반복마다 번갈아 배치하고 각 sample/client는 새로운 schema·프로세스를 쓴다.
앞선 PR #131의 수치를 다른 main과 곧바로 비교해 개선율로 해석하지 않는다. 전후 input/result
hash와 payload 길이, commit/roundtrip/ACK를 확인했다. 결과·환경·한계는 아래에 기록한다.
전후 비교에 쓰는 VersionSet은 고정 합성 pin이다. 실제 배포 artifact의 implementationDigest를
다른 코드에 재사용하는 것을 허용하지 않으며, 배포 시 실제 artifact 기준 pin을 적용해야 한다.

Core Analytics dispatch/publication/ACK runtime과 Integration 전용 E2E harness는 아직 없으며,
Compute ASGI·실 DB 검사를 그 완료 증거로 사용하지 않는다. 기존 Core→Compute smoke는
영향 범위와 구분해 기록한다. ADR-071은 DRAFT이며 운영 자원 수치·실행 제한·Core E2E gate는
이 SQL 최적화로 승인하거나 완료 처리하지 않는다.

첫 커밋은 SQL projection·필수 DB 회귀·동작 경계, 두 번째 커밋은 동일 조건의 실측 결과와
운영 gate 연결이다. Rollback은 두 조회의 projection을 복원하는 것으로 한정된다.

Determinism impact: none. Point-in-Time impact: none.

## 4. 동일 조건 전후 실측

기준 main `4c245dc`와 SQL·회귀 커밋 `e81a7b6`을 비교했다.
같은 호스트·CPython 3.11.5·Windows build 26200·논리 CPU 16개·PostgreSQL 16.4를 사용했다.
호스트 RAM 약 15.57GiB, 측정 시작 관측 available 약 3.51GiB인
공유 개발 환경이며 cache/disk·background load를 통제한 운영 benchmark가 아니다.
잠금파일은 `8b5735f18b372f4a64cc8c6c8ca1d87d8ba85e3af28ce2bade098e20a3fbc75c`이며,
FastAPI 0.141.1·cryptography 50.0.2·psycopg 3.3.4·pytest 9.1.1 설치와 일치한다.

LF 정규화 source 지문은 전 `c6614522a43bc1ebc5fde4bf5b990e3637c37128a326f73899d4e6b84587160e`,
후 `6dcf8678c2c017304144c2c52c9d15a717836e636127b7b9b3b04ea36c58d32c`다. 코드 사본의 차이는 repository 파일 하나이며
도구 지문은 전후 모두 `ba2ecc65948e49afc4204e8ff18a9ebdf9161f368eb9f53b5fc1ddb203cea465`다.
별도 사본의 import 경로까지 확인했고 실제 배포 implementationDigest로 사용하지 않는다.

기존 `analytics_publication_baseline` 도구·입력 생성기·합성 준비 문서는 변경하지 않았다.
각 반복을 전→후, 후→전, 전→후 순서로 가까이 배치했으며, case별 sample/client는 각각 새 schema와
fresh process를 사용했다. 준비 Engine·Core smoke는 측정 전에 종료했다. 각 버전의 5개 profile·
18개 client, 총 **36개 client가 모두 PUBLICATION_VERIFIED**였다. 동일 episode의 모든 전후
표본은 input/result hash·준비 문서 지문·payload 길이가 같았다. Job/time/token/nonce가 달라
body/ciphertext hash는 sample 안에서 검증했다. 전후 모두 commit·payload roundtrip·ACK를 확인했다.

MiB=1,048,576 bytes. 평문은 32/256/512 episode가 각각
0.265/5.420/18.489MiB다.
시간은 초 단위 중앙값이며 connection 생성도 포함한다. 단일/lock은 버전별 3회,
동시는 버전별 3쌍·client 6개의 중앙값이다. RSS는 준비 결과 parse/직렬화와 DB 작업을 포함한
각 client의 누적 peak working set이며 단계 합계·두 client 합계·전체 worker/DB 서버 메모리가 아니다.

| Profile / Version | Client 표본 | 발행 commit 초 | Descriptor 초 | Read/decrypt 초 | ACK 초 | 최대 client RSS MiB |
|---|---:|---:|---:|---:|---:|---:|
| 단일 / 32 / 변경 전 | 3 | 0.140 | 0.104 | 0.102 | 0.110 | 46.4 |
| 단일 / 32 / 변경 후 | 3 | 0.165 | 0.105 | 0.109 | 0.127 | 46.9 |
| 단일 / 256 / 변경 전 | 3 | 0.301 | 0.227 | 0.275 | 0.236 | 99.2 |
| 단일 / 256 / 변경 후 | 3 | 0.252 | 0.105 | 0.224 | 0.115 | 99.3 |
| 단일 / 512 / 변경 전 | 3 | 0.912 | 0.512 | 0.497 | 0.537 | 231.6 |
| 단일 / 512 / 변경 후 | 3 | 0.890 | 0.115 | 0.451 | 0.199 | 232.4 |
| 2초 lock / 512 / 변경 전 | 3 | 2.975 | 0.549 | 0.533 | 0.499 | 231.8 |
| 2초 lock / 512 / 변경 후 | 3 | 3.306 | 0.141 | 0.616 | 0.164 | 232.2 |
| 2 client / 512 / 변경 전 | 6 | 1.504 | 0.629 | 0.575 | 0.613 | 232.5 |
| 2 client / 512 / 변경 후 | 6 | 1.603 | 0.124 | 0.639 | 0.143 | 232.5 |

512 episode 단일 client의 descriptor 중앙값은 0.512→
0.115초, ACK는 0.537→
0.199초였다. Descriptor 단계까지의 최대 누적 RSS는
231.5→176.9MiB였다.
이 단계의 불필요한 BYTEA 반환이 사라졌다는 DB 회귀와 함께 보는 국소 관측이다.

실제 다운로드는 계속 payload 전체를 조회·복호화하므로 전체 client peak는
231.6→232.4MiB였다. 발행·다운로드 경로를 최적화하거나
전체 worker 메모리 상한을 확보한 것으로 해석하지 않는다. 단계별 최대/중앙값을 합하거나
표본 3회로 운영 SLA·p95/p99·worker 동시성 권장값을 계산하지 않는다.

작은 0.265MiB 결과에서는 descriptor 0.104→0.105초, ACK 0.110→0.127초로 개선이 관측되지
않았다. 변경하지 않은 발행·다운로드 시간도 표본마다 달랐다. 모든 크기에서 latency가 줄어든다는
결론을 내리지 않으며, 큰 BYTEA를 반환하지 않는다는 DB 결과 type 검증과 국소 비용을 구분한다.

전후 lock 실험 모두 실제 publisher의 lock wait를 관측한 뒤 2초 이상 유지하고,
release 전 RUNNING/payload 0과 이후 완료·조회·ACK를 확인했다. 두 client 실험도 전후 각 3쌍의
발행 호출 구간 겹침을 검증했다. 서로 다른 schema의 publication client이며 shared queue admission,
lease 만료·삭제 경합·최대 계약 결과·production Linux/systemd·메모리 hard limit 검증은 남아 있다.

## 5. 검증·한계·증적

- 새 DB 회귀 10개·기존 repository/API/worker/발행 probe 45개는 **55 passed**, 삭제 전용
  schema의 기존 회귀는 **15 passed**로 총 **70 passed**다. 전체 suite는
  **958 passed / 157 skipped**, 기존 AnyIO assert rewrite 경고 1개다. Skip은 통과로 세지 않는다.
- Ruff·formatter 289 files, src strict Pyright 오류·경고 0개. Native Linux 검사는 이번에
  재실행하지 않았다. Windows 전체/DB 검사와 Core smoke에는 기존 외부 IPv6 bootstrap을
  사용했고, 전후 실측에는 사용하지 않았다. Production source나 의존성을 그 목적으로 수정하지 않았다.
- 초기 확장 DB 실행은 55 passed 뒤 삭제 검사의 전용 schema 안전 검사 15개에서 setup error였다.
  안전 검사를 완화하지 않고 필요한 `deletion_test_` schema로 분리해 55+15개를 다시 통과했다.
- 첫 smoke는 잘못 선택한 schema prefix 때문에 fixture seed에서 멈췄다. 같은 실행의 Core
  기동은 Windows socket listen 실패 1회 후 harness 재기동으로 READY였다. 최초 로그를 보존하고
  허용되는 격리 prefix로 수정했다. 두 번째 실행은 로컬 wrapper의 잘못된 호출 경로로 중단됐으며,
  경로·구문을 확인한 뒤 세 번째 실행에서 실제 Core→Compute smoke를 통과했다.
- Smoke Core는 사용자 checkout과 분리한 clean main `70cd7d9`이다. 새 Core/Compute
  schema와 immutable fixture로 same/cross market·drawdown·replay·실패 응답을 확인했다.
  계약/Context 문서·fixture 동기화도 전후 검증했다. 이는 **Backtest Core dispatch→terminal
  persistence smoke**이며 Analytics Core runtime E2E의 완료 증거가 아니다. Analytics는 실제
  repository/API/worker·stale ACK 검사 범위이며 Core의 아직 없는 runtime/harness gate는 남긴다.
- 임시 Core worktree 외의 사용자 작업은 수정하지 않았다. 원래 Core checkout의 동시 작업·
  커밋 진행을 되돌리지 않는다. OpenAPI·migration·설정·pin·lock·fixed vector·측정 도구와
  `PR_BODY.md`는 이번 변경에서 보존했다. 계산·Temporal/Execution 의미론은 바꾸지 않았다.
- Raw report·코드 사본·합성 준비 문서·DB/failed-run 로그는 저장소 밖 로컬
  `refinvest-analytics-query` 증적 폴더에 보관한다. Core smoke evidence는 Integration의
  `.integration-data/architecture-refactoring/analytics-query/20261101T043831Z`에 남긴다.
  Credential·fixture 산출물·raw 증적은 커밋하지 않는다. DB 설정·실행·정리는
  [Integration 정본](../../../integration/README.md)과 [측정 도구 사용법](../tools/README.md#analytics-db-publication-probe)을 따른다.

이 개선은 AR-08 정책/수치 승인이나 AR-09/10 구현을 뜻하지 않는다. 다음 실행 격리 단계는
ADR-071 승인·운영 profile 결정·실제 Core Analytics E2E gate를 유지한 채 별도 범위로 진행한다.
