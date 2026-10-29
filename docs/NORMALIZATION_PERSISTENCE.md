# Normalization persistence 책임 분리 — AR-06N

상태: AR-06N 완료. 구현·고정 비교·실 DB 검증에 이어, 별도 loopback 검증 환경에서
단일 전체 회귀 실행과 고정 Core publication·ACK 재시도·version pin E2E를 통과했다.
검증 run: `20261029T131544Z` (UTC), 작업일 2026-10-29 KST.

## 1. 범위와 검토·커밋 경계

Normalization 한 기능 PR에서 다음 경계로 검토한다.

1. Row/document 변환과 payload 암호화·무결성 검증을 분리하고 고정 입력 비교·변조 검증을 포함한다.
2. 기존 cursor를 사용하는 grant SQL helper를 분리하고 repository를 연결한다. 실제 DB의
   경합·rollback·공유 capacity·ACK와 Core dispatch/publication smoke를 함께 검증한다.

기존 public repository 경로·생성자·application port·오류 타입은 유지한다. 범용 load/save,
Unit of Work, 공통 Job 기반 클래스, schema migration은 도입하지 않는다. Analytics 내부
분리는 AR-06A의 별도 기능 PR이다. 선행 변경과 PR 보조 파일을 보존하며 이 문서의 완료
표시는 commit·push·배포 완료를 뜻하지 않는다.

## 2. 실제 책임과 트랜잭션 소유권

| 위치 | 책임 |
|---|---|
| persistence/postgres/trading_review_jobs.py | Connection·transaction, admission·reconciliation·claim·lease·retry·취소·발행·ACK·cleanup SQL 조율 |
| persistence/postgres/normalization_rows.py | Status·descriptor·receipt projection, claim identity·failure JSON, 기존 persisted document/fingerprint 직렬화 |
| persistence/postgres/normalization_payloads.py | Key 검증·AES-GCM 구성, grant 암호화/locator 복원, terminal 암호화·인증·저장 integrity 검증 |
| persistence/postgres/normalization_grants.py | 전달받은 cursor에서 grant freshness·revision INSERT·artifact lease 종료 SQL 실행 |

주 repository는 1,711줄에서 1,429줄로 줄었다. 남은 SQL 조율을 파일 크기만으로 더 나누지
않는다. 공개 command 하나의 transaction, 잠금과 조건부 갱신 순서가 같은 위치에서 보이게 한다.
Grant helper는 connection을 열거나 commit하지 않으며 caller의 cursor를 그대로 사용한다.

Row 모듈은 DB client·암호화·네트워크를, payload 모듈은 DB client·repository 역참조를,
grant 모듈은 DB client·repository 역참조를 금지하는 정적 의존성 규칙을 추가했다.
기존 legacy 예외는 assets/series의 6개만 유지한다. 이 규칙만으로 런타임 원자성을
보장하지 않으므로 실제 DB 경합과 fault injection을 함께 검증한다.

Publication의 소유권·취소·input/version/result policy 재검사, terminal bytes INSERT,
COMPLETED 상태·descriptor metadata·attempt 기록은 기존 한 transaction에서 실행된다.
Admission·claim·retry·ACK·cleanup의 DB clock·advisory/row lock·CAS 조건을 유지한다.
Application 예외 매핑과 DB trigger·immutable integrity metadata도 유지한다.

## 3. 전후 보존 비교

Context ADR-063/064 및 Normalization 전달·result version pin 계약을 기준으로 삼았다.
SQL 문자열 61개는 공백까지 동일하다. 추출한 row·grant helper AST는 소유 이름 변경 외
동일하며, repository method는 검증한 helper 위임과 constructor의 cipher 생성 추출을
정규화한 AST가 같다. Constructor의 key 오류 우선순위와 누락 key의 조회 전 실패도 검증한다.

기존 AES-GCM의 12-byte random nonce·ID 기반 AAD·nonce+ciphertext 형식, JSON 직렬화,
InvalidTag의 safe error 매핑과 저장 count/section/manifest/result hash 검증을 유지한다.
Legacy grant ciphertext가 없으면 원래 payload 객체를 돌려주는 동작도 같다. Analytics와
서로 다른 cipher·fingerprint 구현을 합치지 않는다.

고정 ID·입력·key·nonce를 사용하는 전후 비교:

| 항목 | 바이트 | SHA-256 |
|---|---:|---|
| Status projection | 1,054 | `d1498ae1a4dbda00b447abaecc4d3fdd2b1eb908ec34e0aba0f7044284a69eda` |
| Terminal descriptor | 794 | `723a345ff68b793abc496a0db721df8c0b9b428d8f897ebf81ac0234366e1088` |
| Grant ciphertext | 134 | `e50daaf9ed4c658058f9270a058ed50771ea492c4eaa12cb23e15af655498710` |
| Locator를 복원한 grant JSON | 131 | `faa27d8bb575e8e136356fae3c4c8d33a17e724e19892409789380f9ce2af511` |
| 복호화·검증한 terminal payload | 11,792 | `3e116694be306ca346dabca6347c6f2ec8d5f08192a6bfabf86a84380866a8e4` |
| Terminal ciphertext 비교 vector | 11,820 | `80ac8485c10d2a4cb6ca9767f474fb98987b9909e48b3d74873e9f18f9cf97e9` |

Random nonce를 고정한 것은 로컬 비교 helper뿐이다. Production과 일반 테스트의 nonce는
계속 무작위다. Status/descriptor는 dataclass를 고정 JSON으로 직렬화한 비교 산출물이며
HTTP serializer의 바이트라고 해석하지 않는다.

다른 production source·계산·OpenAPI·Context fixture·migration·deploy는 이 단계에서
변경하지 않았다. Determinism impact: none. Point-in-Time / Execution semantics impact: none.

## 4. 검증 범위

실 PostgreSQL에서 Normalization 27개와 Analytics 7개, 총 34개가 skip 없이 통과했다.
새 전용 schema에 0001~0017 migration을 적용했다. 기존 admission/replay·공유 capacity·
grant 갱신·lease 소유권·취소·V1/V2 pin·payload 상한·trigger·ACK·cleanup 검증을 포함한다.

새 검증은 두 발행의 동시 실행에서 terminal payload가 하나만 남는지, 취소와 발행이
같은 row lock 아래 직렬화되는지 확인한다. Payload INSERT 직후 예외를 주입하면
transaction이 rollback되어 RUNNING 상태와 payload 부재가 유지되고, 이후 정상 발행이
가능한지도 확인한다. 별도 순수 테스트 14개는 다른 key/ID·ciphertext 변조·저장 metadata
불일치·random nonce·legacy grant·설정 오류 우선순위를 검증한다.

Formatter 249 files·Ruff·Pyright strict, 변경 후 계약 동기화 검사는 통과했다.
전체 수집은 773개다. 한 전체 실행에서 642 passed·3 failed·128 skipped였으며, Windows
socketpair/AST 런타임 오류가 난 세 노드를 독립 Python 프로세스로 실행해 모두 통과했다.
재실행 포함 coverage는 645 passed·128 skipped·미해결 실패 0개다. 단일 실행 전체 PASS로
기록하지 않는다. 최초 중단 실행과 각 재실행의 JUnit·로그, node별 coverage를 보존한다.
Skip은 격리 DB URL 없는 41개와 폐기된 PoC 87개다. DB 검증 34개는 별도로 실제 실행했다.

Core publication은 고정 Core `206473c6e7c5214d23c731935e9ead703507a2b6`로 네 차례 시도했다.
첫 실행은 Compute 기동 시 Windows socketpair 오류로 중단됐다. 두 번째는 PENDING job,
세 번째는 RUNNING job의 status 조회가 500이 됐고 API/worker DB 연결에 Bad address 10014가
기록됐다. 네 번째는 별도 환경 파일에서 허용 loopback host를 localhost로 변경하고 새 포트와
schema를 사용했지만 Compute listen 오류로 중단됐다. 기존 환경 파일은 변경하지 않았다.
기동 실패 두 건은 structured evidence 생성 전 중단됐으며 console에 표시된 경로를 성공
증적으로 해석하지 않는다. 상태 조회 실패 두 건의 evidence.json과 네 실행 로그를 보존한다.

원래 preflight 로그는 모든 검증 항목의 완료 메시지가 있으나 바깥 shell 종료 상태가 1이었다.
변경 후 명시적으로 확인한 계약 동기화 종료 코드는 0이다. 이 차이도 manifest에 기록한다.
원본 계약·fixture가 전후 동일함은 source hash 비교로 별도 확인했다.

위 내용은 최초 실행의 실패 기록이다. 이후 `20261029T134736Z` 재검증에서 아래 결과를 확정했다.

### 후속 재검증과 완료 판정

Application을 import하지 않는 socketpair probe에서도 Python 3.11.5는 20회 중 3회,
설치된 Python 3.12.0은 20회 중 2회 실패했다. IPv6 socketpair·DB loopback probe는 모두
통과해, 원래 소스를 유지한 채 opt-in 임시 네트워크 bootstrap으로 검증했다.

- `REFINVEST_VERIFY_IPV6_LOOPBACK=1`일 때 asyncio 내부 socketpair 기본 family를 IPv6로 설정한다.
- API·fixture HTTP는 IPv6-mapped loopback listener로 기존 127.0.0.1 URL을 받는다.
- API listener backlog는 1이며 단일 worker·reload 없음이다. DB는 `PGHOSTADDR=::1`을 사용한다.
- Bootstrap은 로컬 evidence에만 보관한다. Compute·Core·Integration source와 E2E 판정은 변경하지 않았다.

같은 source fingerprint `e7dda918da852c059a0678d1288d751d1f22a4163a70ba2d0f67fab836eeded9`로
최종 bootstrap의 전체 suite가 한 번에 645 passed·128 skipped, 종료 코드 0이다. 앞선 bootstrap
실행도 같은 수치로 통과했다. AnyIO 선행 import에 따른 assert-rewrite warning 1개는 별도 기록했다.

일반 publication harness는 정상 발행·ACK와 hash/count를 확인했으나 ACK 실패 주입 전에 ACK가
끝나 `ACK_FAULT_WINDOW_MISSED`로 실패했다. 해당 실행을 PASS로 집계하지 않았다. 기존 proxy
기반 `test-trading-normalization-result-version-pin.ps1`로 주입 시점을 제어하여 최종 종료 코드 0을
확인했다. Primary·result integrity·ledger publication이 PASS이고 ACK는 ACKNOWLEDGED_AFTER_RETRY다.
LedgerRevision 1개, staging 정리, commit 이후 ACK, 재시도 후 중복 발행 없음과 ORCH-024~028을 확인했다.

ORCH-027의 단일 transaction migration runner 진단은 SQLSTATE 55006으로 rollback됐다. 정본의
기존 psql -f autocommit 경계에서는 migration·재시작·replay 검사가 통과했다. 이 배포 방식의
차이를 숨기거나 migration runner 전체가 안전하다고 확대하지 않는다. V2 Core publication은
기존 harness 범위 밖이다. 검증이 소유한 main·second·canary process registry는 모두 비었다.

이 결과로 AR-06N의 남은 Core gate를 닫는다. 기본 Windows IPv4 환경의 오류 자체는 남아 있고,
이번 성공은 명시한 검증용 네트워크 구성에 한정한다. Throughput·backlog 성능이나 production
네트워크 구성의 검증을 뜻하지 않는다. 임시 bootstrap을 제품 코드에 포함하지 않는다.

[후속 재검증 manifest](../../../integration/.integration-data/architecture-refactoring/ar06n-revalidation/20261029T134736Z/manifest.json)에
probe·단일 suite·실패/성공 E2E·bootstrap 버전·fingerprint·checksum을 연결했다. 최초 부분 완료
manifest는 당시 실행 기록으로 보존한다.

## 5. 한계와 후속 단계

이번 변경은 저장 형식·key rotation·새 retention·SQL 성능 개선을 포함하지 않는다.
정적 AST 비교는 실제 DB 경합이나 crash recovery 전체 조합의 증명이 아니다. E2E는 이전
baseline과 같은 고정 Core revision을 사용하며 현재 원래 Core HEAD와의 호환성 검증과 구분한다.
부하·대형 payload 메모리·전체 crash recovery 조합은 별도 운영 검증 범위다.

다음 AR-06A에서는 Analytics persistence의 실제 책임을 별도로 검토한다. Normalization의
분할 구조를 템플릿처럼 적용하거나 두 기능의 저장·상태 모델을 합치지 않는다.

로그·JUnit·전후 bytes·AST/SQL 비교·source fingerprint·checksum은
[AR-06N manifest](../../../integration/.integration-data/architecture-refactoring/ar06n/20261029T131544Z/manifest.json)에
보관한다. Credential·로컬 fixture·compatibility evidence는 Compute에 커밋하지 않는다.
