# Synthetic V1 normalization identity oracle

`result-integrity-v2.json`은 ADR-070의 별도 synthetic 결과 oracle이다. 기존 V1 파일은 수정하지 않는다. 하나의 ReviewUnit에 두 Allocation, 각각의 execution/fee/PnL canonical 근거, 실제 `0` fee/PnL과 1:1 보충 행을 담는다. `sectionVectors`의 10개 JCS 입력/UTF-8 hex/SHA-256, 기존 투영을 유지한 `manifestVector`, 새 `resultDigestManifestVector`의 전체 result hash 및 `invalidCases`의 참조 실패를 `python scripts/verify-trading-normalization-result-v2.py`로 검증한다. 재생성 명령은 `python scripts/generate-trading-normalization-result-v2-vector.py`다. 생성기는 기대값을 만들고 검증기는 저장된 기대값을 독립 계산한다. 모든 ID/금액은 비민감 synthetic 예시이며 실제 CSV가 아니다.

`result-integrity-v1.json`은 ADR-066의 synthetic 결과 oracle이다. `result-integrity-v1.schema.json`과
`scripts/verify-trading-normalization-result-integrity.py`가 terminal 공개 DTO, declared count,
9개 section canonical JSON/UTF-8 hex/SHA-256, 공개 manifest hash와 전체 resultHash를 검증한다.
고정 결과 hash는 `cfb13b971bfa0952d5c5a251daef94d7e76271960905ea5a6d516adc32ad4107`다.
작성 시 독립 Python `hashlib`로 값을 고정했고 Node.js `crypto`로 저장된 hex bytes를 별도 검증한다.
모든 ID와 기록은 synthetic이며 실제 Binance symbol/UID/Trade ID/CSV/credential/locator가 없다.
이 vector는 공개 DTO와 hash byte 계약 oracle이며 금융 복원 계산의 golden fixture는 아니다.
V1 vector 문자열은 ASCII이므로 Python의 key-sorted compact JSON이 JCS와 byte-identical하다;
비ASCII JCS 적합성은 input identity 별도 vector/구현 검증 범위다.

`input-identity-v1.json`은 ADR-065의 `TRADING_INPUT_SET_HASH_V1` 고정 oracle이다.
`input-identity-v1.schema.json`은 fixture 자체의 machine-readable shape를 검증한다.
`wire-v1.json`은 같은 identity의 Compute create request, 완료 descriptor 및
입력 불일치 `FAILED` status를 OpenAPI schema로 검증하는 예시다. grant URI는
`example.invalid`의 비작동 synthetic locator이며 서명·credential이 없다.
`resultDeliveryPolicy`와 descriptor의 policy version/content encoding도 이 wire fixture에서
검증한다. 예시 `maxAcceptedPayloadBytes=1000`은 synthetic schema 검증값이며 운영 한도가 아니다.
모든 ID, component hash와 SHA-256 원본 값은 예시이며 실제 Binance CSV/UID/거래값/URL/
credential이 아니다. Component `versionSetHash` 값은 이 vector에서 **identity hash 바이트
검증용 synthetic 값**이다. 배포 registry의 VersionSet compatibility/개별 component hash
검증을 통과한다고 주장하지 않는다.

검증 절차:

1. fixture JSON과 schema를 검증하고 `specificationVersion` 및 identity 안의
   `hashSpecificationVersion`이 모두 `TRADING_INPUT_SET_HASH_V1`인지 확인한다.
2. `inputIdentity`만 RFC 8785 JCS로 직렬화한 문자열을 **저장된** `canonicalJson`과 비교한다.
3. 저장된 `canonicalJson`을 BOM/newline 없이 UTF-8로 인코딩해 **저장된**
   `canonicalUtf8Hex`와 비교한다.
4. 저장된 bytes의 SHA-256을 별도 도구로 계산해 **저장된** `expectedSha256`과 비교한다.
   production Core/Compute hasher가 시험 도중 기대값을 생성해서는 안 된다.

작성 시 canonical bytes와 SHA-256은 Node.js `crypto`로 고정했고, 별도로 Python
`hashlib.sha256(bytes.fromhex(canonicalUtf8Hex))` 및 PowerShell/.NET SHA256으로
저장된 bytes를 재검증한다. 실제 검증 결과와 명령은 PR Validation에 남긴다.

`Asia/Seoul`의 slash는 `\/`로 바뀌지 않으며 role은 JSON object key로 정렬된다.
V1 Session 배열은 정확히 한 요소다. 추가 Unicode/복수 Session 규칙을 적용하는
경우에는 V2 규격과 별도 vector를 먼저 정의한다.
