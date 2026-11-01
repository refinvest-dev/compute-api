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
hash와 payload 길이, commit/roundtrip/ACK를 확인하고 결과·환경·한계를 후속 커밋에 기록한다.
전후 비교에 쓰는 VersionSet은 고정 합성 pin이다. 실제 배포 artifact의 implementationDigest를
다른 코드에 재사용하는 것을 허용하지 않으며, 배포 시 실제 artifact 기준 pin을 적용해야 한다.

Core Analytics dispatch/publication/ACK runtime과 Integration 전용 E2E harness는 아직 없으며,
Compute ASGI·실 DB 검사를 그 완료 증거로 사용하지 않는다. 기존 Core→Compute smoke는
영향 범위와 구분해 기록한다. ADR-071은 DRAFT이며 운영 자원 수치·실행 제한·Core E2E gate는
이 SQL 최적화로 승인하거나 완료 처리하지 않는다.

첫 커밋은 SQL projection·필수 DB 회귀·동작 경계, 두 번째 커밋은 동일 조건의 실측 결과와
운영 gate 연결이다. Rollback은 두 조회의 projection을 복원하는 것으로 한정된다.

Determinism impact: none. Point-in-Time impact: none.
