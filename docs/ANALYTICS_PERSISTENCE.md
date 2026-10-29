# Analytics persistence 책임 분리 — AR-06A

상태: AR-06A 완료. 구조 전환·전후 비교·전체 회귀·실 PostgreSQL 검증을 마쳤다.
검증 run: `20261029T143741Z` (UTC).

## 1. 범위와 검토·커밋 경계

한 Analytics 기능 PR에서 다음 두 경계로 검토한다.

1. Accepted/status/descriptor와 저장 request·grant document 변환, AES-GCM codec을 분리하고
   고정 bytes·인증 실패·유효기간 경계 검증을 포함한다.
2. Repository consumer를 연결하고 기존 SQL·트랜잭션·lease 조건의 보존, 실제 DB 경합·
   rollback·API/worker/ACK 세 모드를 검증한다.

선행 변경과 PR 보조 파일을 보존했다. 완료 표시는 commit·push·배포 완료를 뜻하지 않는다.
Normalization의 분할 구조를 그대로 복제하거나 범용 repository·Unit of Work를 도입하지 않았다.
새 migration·timeout·retry 정책은 포함하지 않는다.

## 2. 실제 책임과 의존성

| 위치 | 책임 |
|---|---|
| persistence/postgres/trading_analytics_jobs.py | Connection·transaction, DB clock, admission·grant update·claim·fencing·retry·취소·발행·ACK·cleanup SQL |
| persistence/postgres/analytics_documents.py | 저장 request에서 locator 분리, grant 추출·명시된 DB 시각 기준 expiry, accepted/status/descriptor projection |
| persistence/postgres/analytics_payloads.py | 기존 AES-GCM 구성·nonce prefix·identity AAD 암호화·복호화 |

Repository는 786줄에서 697줄로 줄었다. 짧은 accepted 조회 SQL, `_db_time`, claim predicate와
parameter binding은 SQL 조율과 함께 남겼다. 새 SQL helper 파일을 형식적으로 만들지 않았다.
Document helper는 connection·암호화·네트워크를, payload helper는 connection과 repository
역참조를 차단하는 정적 규칙을 추가했다. 기존 legacy 예외는 assets/series의 6개만 남는다.

기존 public repository 경로·생성자·속성·application port·오류 class identity를 유지한다.
같은 connection/cursor에서 소유권을 잠그고 payload INSERT → job COMPLETED → attempt
COMPLETED를 실행한다. Generation·owner·attempt/token·lease expiry·cancellation 조건은 같다.
설정 오류의 우선순위·문구와 InvalidTag 전파도 유지하며 Normalization의 오류 방식으로 합치지 않는다.

## 3. 전후 보존 비교

정본은 Context ADR-069와 Analytics Job/Result Integrity V1 계약이다. SQL 문자열 48개와
DB clock·claim predicate·binding은 동일하다. Repository method는 검증한 helper 위임을
정규화한 AST가 같으며, document/cipher 본문과 accepted/status/descriptor 표현도 동일하다.

ID·시각·nonce를 고정한 12개 전후 산출물의 전체 bytes를 비교했다. Accepted, PENDING/
RUNNING/COMPLETED/FAILED/CANCELLED 상태, stable request, grants, expiry, ciphertext,
descriptor, terminal ciphertext가 모두 일치했다. 주요 값은 다음과 같다.

| 항목 | 바이트 | SHA-256 |
|---|---:|---|
| Accepted projection | 335 | `b79d6065e9b85ffb4675ccf6e398d5aa9a164070eab1f8084f7231a23c31c879` |
| Stable request | 3,732 | `e8612e718831e283e791d485899f7689459f3c083c9f52bf264668b3081e2da4` |
| Descriptor | 1,601 | `a2fb7201a6109797e6ccee78ba717731379c5feddd7caac6ffafda616b17960a` |
| Ciphertext | 45 | `96f648fe66728c3a3f6985811ef3e00ca93df2f12b558a0d319fc9d4ba558d25` |
| Terminal ciphertext | 1,167 | `894f125c6070f95e75ad09fcdba669ee178d774e36172617acdbbccbcc8cc803` |

Projection은 고정 JSON으로 직렬화한 비교 산출물이며 HTTP serializer bytes와 구분한다.
비교 helper의 nonce만 고정했다. Production은 기존 urandom(12)을 유지한다. 다른 production
source·계산·Normalization persistence·OpenAPI·fixture·migration·deploy는 변경하지 않았다.
Determinism impact: none. Point-in-Time / Execution semantics impact: none.

## 4. 검증 결과

- 직접 영향 suite: 131 passed.
- 전체 suite: 663 passed, 131 skipped, 실패 0개. 단일 실행 결과다.
- 실제 PostgreSQL: Normalization 27개·Analytics 10개, 총 37 passed, skip 없음.
- Formatter 252 files·Ruff·Pyright strict, 전후 계약 동기화 종료 코드 0.

새 순수 테스트 14개는 ID/key/tag 변조, 정확한 plaintext와 nonce/AAD 형식, locator 분리,
명시된 DB 시각 기준 5분 expiry 경계·더 이른 role 선택, 기존 설정 오류를 검증한다.
새 금지 import 사례 4개도 추가했다.

실 DB에는 0001~0017을 새 전용 schema에 적용했다. 기존 create/replay·공유 capacity·
claim/reclaim·stale write·retry·cleanup과 실제 provider를 사용하는 ASGI HTTP→worker→
descriptor→resource→ACK를 검증했다. ACK는 PERSISTED, DELETED_GENERATION, STALE_ATTEMPT
세 모드다. 이 smoke는 artifact 다운로드만 비민감 fixed input으로 대체한다.

두 발행의 경쟁에서는 성공 하나와 payload 하나만 남는다. 취소와 발행의 경쟁에서는 동일한
job/attempt terminal 상태와 payload 존재 여부를 확인했다. 최종 attempt UPDATE를 실행한 직후
예외를 주입하면 payload·job·attempt가 함께 rollback되고, 이어 정상 발행이 가능함도 확인했다.

전체·DB·직접 영향 suite는 AR-06N에서 검증한 opt-in IPv6 loopback bootstrap과 PGHOSTADDR=::1을
사용했다. 기본 Windows IPv4 오류가 해결됐다는 의미는 아니다. Bootstrap의 AnyIO 선행 import에
따른 assert-rewrite warning 1개를 기록했다. Production 코드·네트워크 설정에는 포함하지 않는다.

## 5. 검증 한계와 다음 단계

Analytics Core dispatch/publication E2E는 기존 harness가 없어 NOT_RUN이며 Integration/AR-10의
후속 gate다. 이 공백을 Compute ASGI+실 DB 검증이나 앞선 Normalization Core E2E로 대체하지 않는다.
Normalization은 source가 동일하여 AR-06N의 실 DB·Core 증적을 유지하고, 이번에는 공유 capacity와
DB 회귀를 다시 실행했다.

Adapter는 기존처럼 application이 전달한 payload/body와 semantic hash의 정합성을 전제로 한다.
저장 후 bytes 조회의 검증을 Normalization과 같게 강화하는 것은 별도 검토 범위다. 이번 분리는
key rotation·대형 payload 메모리·전체 crash recovery·운영 배포 검증을 추가하지 않는다.

다음은 AR-07 Snapshot 조회 경계와 요청 내 재사용이다. 변경 전에 같은 fixture의 `/assets`
loader 호출 횟수와 Snapshot 조회 비용을 먼저 측정한다.

로그·JUnit·12개 전후 bytes·AST/SQL 비교·source fingerprint·checksum은
[AR-06A manifest](../../../integration/.integration-data/architecture-refactoring/ar06a/20261029T143741Z/manifest.json)에
보관한다. Credential·로컬 fixture·compatibility evidence는 Compute에 커밋하지 않는다.
