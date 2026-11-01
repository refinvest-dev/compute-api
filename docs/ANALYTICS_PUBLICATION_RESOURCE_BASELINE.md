# AR-08 Analytics DB 발행 경계 실측

2026-11-01 KST. **Windows 개발 실측 / 운영 수치 미확정 / production 미연결**.
ADR-071은 DRAFT다. 이번 자료는 AR-08~10 완료나 실행·메모리 한도 승인을 뜻하지 않는다.

## 1. 목적과 경계

[구조적 JSON 실측](ANALYTICS_STRUCTURED_RESOURCE_BASELINE.md) 뒤에 남은 DB 발행 비용을
분리한다. 기존 합성 생성기·Engine·projection·section hash로 결과를 먼저 준비한 뒤 별도
fresh client에서 실제 `PostgresAnalyticsJobRepository`를 사용한다. 준비 단계와 실행 중
상류 Engine graph를 함께 보유한 전체 worker는 이번 측정의 범위에 포함하지 않는다.

측정 경계는 `publish_completed` 전체(body SHA-256·descriptor 구성·AES-GCM·새 connection·
claim fence/row lock·payload/job/attempt write·transaction commit), descriptor 조회,
payload read/decrypt, ACK commit이다. 각 API가 여는 connection 비용도 포함하며 SQL만의 시간이
아니다. `create`·claim·payload 조립/직렬화·후속 검증 쿼리는 단계별 시간에서 제외한다.

## 2. 환경·재현 조건

- 기준 main `59c550a`, 도구·필수 테스트·사용법 커밋 `0c06da2`.
- Windows build 26200, AMD64, CPython 3.11.5, 논리 CPU 16개. 호스트 RAM 약 15.57GiB,
  측정 중 관측 available 약 3.17GiB인 공유 개발 환경이다.
- 같은 호스트의 PostgreSQL 16.4, loopback `::1`, 분리된 `refinvest_integration` DB.
  `synchronous_commit/fsync/full_page_writes=on`, `shared_buffers=128MB`,
  `max_connections=100`, `wal_level=replica`. 설정을 변경하거나 cache/disk 상태를 통제하지 않았다.
- FastAPI 0.141.1·cryptography 50.0.2·psycopg 3.3.4·pytest 9.1.1. Lock SHA-256
  `8b5735f18b372f4a64cc8c6c8ca1d87d8ba85e3af28ce2bade098e20a3fbc75c`.
- Runtime source 165개 LF 정규화 지문은
  `9502d0e98cf5ac453eb06cb1cfb2808cf6233ee25170d3b869f313cc3fd24f5d`로 앞선 실측과 같다.
  새 publication 도구 지문은 `ba2ecc65948e49afc4204e8ff18a9ebdf9161f368eb9f53b5fc1ddb203cea465`다.
  이 지문은 배포 implementationDigest가 아니다. Source·계약·migration·설정·fixed vector는 보존한다.
- Sample마다 Integration 정본 스크립트로 새 `ar08_pub_` schema에 전체 migration을 적용했다.
  두 client도 서로 다른 schema를 쓰므로 같은 queue·capacity advisory lock 경쟁을 시험하지 않는다.
- Generator/Engine 준비 프로세스는 종료하고, 각 client에 새 OS 프로세스·실제 Job·claim·
  임시 key를 사용한다. Grant는 현재 시각+4분으로 갱신하되 semantic input identity는 보존한다.
- 외부 process-tree watchdog 45초, connection 5초·lock 10초·statement 15초·payload cap 64MiB는
  실험 보호값이다. Production timeout을 설정하거나 lease predicate를 바꾸지 않았다.
  Windows RSS/commit은 관측값이며 메모리 hard limit은 적용하지 않았다.

## 3. 결과

MiB=1,048,576 bytes. 각 단계 시간은 초 단위 중앙값이다. 단일/lock은 3회, 동시는
3쌍의 **client 6개 표본** 중앙값이다. 메모리는 client 하나의 최대 누적 peak working set이며
동시 peak 합계나 PostgreSQL 서버·전체 worker의 RSS가 아니다. 준비 JSON parse와 직렬화도
process high-water mark에는 포함된다. 단계별 peak를 더하지 않는다.

| Profile / Episode | Client 표본 | 평문 MiB | 발행 commit 초 | Descriptor 초 | Read/decrypt 초 | ACK 초 | 최대 client RSS MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| 단일 발행 / 32 | 3 | 0.265 | 0.171 | 0.115 | 0.098 | 0.124 | 46.3 |
| 단일 발행 / 256 | 3 | 5.420 | 0.234 | 0.204 | 0.250 | 0.215 | 99.3 |
| 단일 발행 / 512 | 3 | 18.489 | 0.789 | 0.464 | 0.530 | 0.479 | 231.4 |
| 2초 row lock / 512 | 3 | 18.489 | 3.131 | 0.552 | 0.565 | 0.535 | 232.4 |
| 두 client 동시 발행 / 512 | 6 | 18.489 | 1.588 | 0.585 | 0.597 | 0.575 | 232.2 |

5개 profile·18개 client 표본이 모두 `PUBLICATION_VERIFIED`였다. Sample별로 새 connection에서
job/attempt COMPLETED와 ciphertext 길이=평문+28, descriptor 크기/SHA, 전체 payload의 byte/hash
roundtrip을 검증했다. ACK 후 payload bytes/descriptor 비노출과 committed receipt도 확인했다.
같은 episode의 모든 반복·lock·동시 실행에서 input/result hash와 준비 문서 지문이 같았다.
실제 Job ID·time·token과 nonce가 다르므로 반복 간 body/ciphertext hash 동일성은 요구하지 않는다.

512 episode의 단일 발행 시간 범위는 0.787~0.897초다.
Lock 실험 3회는 `pg_stat_activity`·`pg_blocking_pids`로 **해당 publisher의 실제 Lock wait**를
관측한 뒤 2초 이상 유지했다. Release 전 외부 connection에서 RUNNING/payload 0, release 후
완료 상태와 roundtrip/ACK를 확인했다. Lock을 무시하거나 terminal 상태를 먼저 공개하지 않았다.

동시 실험의 `publish_completed` 호출 구간 겹침은 각각 1.819, 1.434, 1.567초였다.
이는 암호화·connection도 포함한 호출 구간이다. SQL transaction의 모든 구간이 동시에
실행됐다는 뜻은 아니다. 동시 발행 중앙값 1.588초와 단일
0.789초는 이 개발 환경의 관측이며 worker 처리량·운영
동시성·p95/p99나 SLA를 추정하지 않는다. 표본 수와 측정 환경이 그런 결론에 충분하지 않다.

512 episode 단일 client의 발행 전 누적 peak RSS도 이미 최대 139.5MiB였다. 전체 누적
peak 231.4MiB나 peak commit 244.3MiB를 순수 DB 추가 메모리로 해석하지 않는다.

## 4. 확인한 비용과 남은 위험

1. `result_descriptor`는 descriptor만 반환하지만 `p.encrypted_bytes` 전체를 함께 조회한다.
   `acknowledge`도 `SELECT * FROM trading_analytics_payloads`로 ciphertext를 가져온다.
   존재 여부·메타데이터 확인을 위해 payload 크기만큼 DB→client 전송/사본을 만드는 구조다.
   Query에서 bytes 조회를 제거하는 개선은 별도 PR의 후보이며 이번에는 운영 코드를 수정하지 않았다.
   실제 절감 효과는 동일 profile의 전후 비교와 delivery/ACK·만료·동시성 회귀로 검증해야 한다.
2. 512 episode 결과는 평문 18.49MiB지만 단일 client peak는 최대
   231.4MiB였다. 준비 결과 객체와 직렬화 body, 암호화·DB 전송/조회 사본의
   비용을 고려해야 한다. 다른 OS의 앞선 Engine peak와 이 값을 더하거나 동일 조건으로 비교하지 않는다.
3. 암호화 후 DB row lock을 기다리므로 대기 중에도 큰 body/ciphertext가 살아 있을 수 있다.
   이번 2초 대기는 lease 안에서 끝났다. Lease expiry·heartbeat 경합·취소/삭제와 publication
   장기 대기·DB 장애·과부하·WAL/disk 포화·crash recovery 검증을 대신하지 않는다.
4. 최대 계약 payload·production Linux/systemd·메모리 hard limit·실제 상류 Engine와 함께
   살아 있는 worker, shared queue admission/worker 동시성은 남아 있다. 상한이나 정책 수치를
   조용히 낮추지 않았으며 이번 표본을 근거로 새 기본값을 넣지 않는다.
5. Repository에 합성 ACK `PERSISTED`를 직접 전달했다. 실제 Core durable commit·HTTP/TLS·
   Analytics dispatch부터 terminal delivery/ACK까지의 E2E는 실행하지 않았다.
   Integration 소유 harness와 실제 Core 경로의 release gate가 계속 필요하다.

## 5. 검증·증적·후속 순서

- 새 unit 검사 11개, 관련 unit/architecture **123 passed**. 실제 DB probe 1개와 기존
  Normalization 27·Analytics 10·Backtest 7개는 **45 passed**다. Skip을 통과로 세지 않았다.
- 전체 **943 passed / 132 skipped**, 기존 AnyIO assert rewrite 경고 1개. Ruff·formatter
  278 files와 src strict Pyright 0 errors/0 warnings. Pyright 버전 안내로 lock을 바꾸지 않았다.
- 초기 DB 도구 검증은 PostgreSQL inet의 `::1/128` 표기를 loopback으로 인식하지 못해
  1 failed/44 passed였다. `host(inet_server_addr())`로 비교하는 수정 후 새 schema에서 45개가
  통과했다. 이 authoring 실패를 실측 성공 표본에 포함하지 않는다.
- Windows 전체/DB 회귀는 기존 외부 IPv6 bootstrap을 사용했다. 새 unit 11개 단독·관련 123개와
  실제 발행 실측은 해당 bootstrap을 사용하지 않았다. Linux 실행 검증은 이번에 재실행하지 않았다.
- Runtime·OpenAPI·migration·lock·입력 생성기·이전 측정 도구와 `PR_BODY.md`의 보존을 확인했다.
  운영 동작·Core 호출/계약을 바꾸지 않아 새 Core E2E나 contract-sync 실행을 주장하지 않는다.
- 재현 CLI·새 schema·credential 설정은 [도구 사용법](../tools/README.md#analytics-db-publication-probe)과
  [Integration 정본](../../../integration/README.md)을 따른다. Raw reports·준비 합성 문서·migration/
  test logs는 저장소 밖 로컬 `refinvest-ar08-publication` 증적 폴더에 보관하고 커밋하지 않는다.

첫 커밋은 도구·필수 테스트·사용법, 두 번째 커밋은 실측 결과와 gate 연결이다. 모두 개발용이며
worker·API·persistence runtime을 활성화하지 않는다. 다음 독립 후보는 descriptor/ACK의 불필요한
BYTEA 조회 제거와 동일 profile의 전후 검증이다. 운영 profile·지원 OS·자원 수치와 ADR-071 승인,
Core/Integration E2E gate는 계속 남긴다.

Determinism impact: none. Point-in-Time impact: none.
