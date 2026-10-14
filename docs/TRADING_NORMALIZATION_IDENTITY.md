# Trading Normalization Input Identity V1

ADR-065의 `inputSetHash` 바이트 계약이다. Core와 Compute는 이 문서와 두 OpenAPI의
`NormalizationInputIdentity`를 같은 의미로 구현한다. 비민감 고정 oracle은
`fixtures/trading-normalization/input-identity-v1.json`이다.

## 세 hash의 경계

| 이름 | 대상 | 확인 시점 |
|---|---|---|
| `inputSetHash` | 아래 `inputIdentity`만 | Core admission 및 Compute job 접수 시 독립 재계산 |
| source content SHA-256 | 각 grant에서 실제로 받은 원본 bytes | normalize 이전; identity의 role별 `sha256`과 비교 |
| `resultHash` | 결정적 normalization 결과/manifest | terminal 생성과 Core staging/publication |

`payloadSha256`은 terminal JSON 전송 bytes의 별도 무결성 hash다. dialect 탐지 결과,
timezone database version, instrument terms의 실제 계산 입력/출력과 manifest hash는
result provenance 또는 별도 execution/result hash에 기록한다. 이를 `inputSetHash`에
사후 혼합하지 않는다. `inputSetHash`는 결과가 올바르다는 증명이 아니다.

## `NormalizationInputIdentity`

V1 root에는 아래 필드 **정확히 아홉 개**가 있다. 모두 required, non-null이다.
알 수 없는 property, 중복 JSON key, 잘못된 enum/role, 빈 ID는 거절한다.

| 필드 | V1 의미 |
|---|---|
| `hashSpecificationVersion` | 고정 문자열 `TRADING_INPUT_SET_HASH_V1`; hash 입력에 포함 |
| `tradingBookId` | Core 소유의 안정적인 opaque Book ID; 표시명·UID 금지 |
| `sourceImportSessionIds` | 중복 없는 opaque ID 배열; MVP는 정확히 1개. 장래 복수 source를 위한 plural 구조이며 확장은 새 계약 필요 |
| `artifactsByRole` | 정확히 `TRADE_HISTORY`, `POSITION_HISTORY` key. 각 값은 `{artifactId,sha256}`만 포함 |
| `sourceTimezone` | Core가 pin한 IANA timezone ID 원문 |
| `reviewTimezone` | Core가 pin한 IANA timezone ID 원문 |
| `declaredProduct` | `{venue,productFamily,settlementAsset}`; 기존 wire enum 값 그대로 |
| `declaredPositionMode` | 기존 wire enum 값 `ONE_WAY` |
| `targetVersionSet` | Core `TargetTradingVersionSet`의 normalization 적용 부분 전체: 정확히 `normalizer`, `reconstruction`, top-level `versionSetHash` |

`normalizer`에는 role별 source dialect version, adapter, normalizer, canonical schema,
implementation digest와 component `versionSetHash`가 있다. `reconstruction`에는
instrument별 terms version, reconstruction policy, reconciliation, ReviewUnit/evidence
schema, implementation digest와 component `versionSetHash`가 있다. 이 두 component와
합성 `versionSetHash`는 Core 정본의 값을 그대로 보존한다. `analytics`는 normalization
입력이 아니므로 V1 object에 **생략**한다. 누락 component, null, flat Compute
`InternalNormalizerVersionSet` 대체, hash만 전달하는 표현을 허용하지 않는다. VersionSet
내 map key도 중복 불가다. Component hash와 합성 hash는 별도의 VersionSet 규칙대로
검증하며 `inputSetHash`만 맞는다는 이유로 우회하지 않는다.

Artifact `sha256`은 원본 bytes의 64자리 lowercase hex다. 같은 role은 한 번만 나타나며
알 수 없는 role은 거절한다. role은 object key이므로 grant 배열 순서는 hash와 무관하다.
`byteSize`와 `contentType`은 검증 descriptor로 pin하고 다운로드 시 확인하지만 V1
logical identity에는 넣지 않는다. 같은 artifact ID에 다른 SHA-256은
`ARTIFACT_IDENTITY_CONFLICT`다. source session IDs는 UTF-16 code unit의 unsigned
사전순으로 정렬한다. V1은 한 개지만 Core와 Compute가 같은 정렬·중복 검사를 해야 한다.
ID/enum/timezone은 trim, case folding, Unicode normalization 또는 offset 변환 없이
계약상 원문을 쓴다. 서버·브라우저 timezone을 추론하지 않는다.

## Canonical bytes와 계산 순서

JSON Canonicalization Scheme **RFC 8785 (JCS)** 를 사용한다. object key는 UTF-16
code unit 순, 중첩 object에도 동일하게 정렬한다. 배열 순서는 보존하되 위 source ID
정렬을 먼저 수행한다. RFC 8785의 ECMAScript JSON string serialization을 적용한다:
필요한 quotation mark, reverse solidus와 제어문자만 escape하며 solidus(`/`)는
escape하지 않는다. Unicode scalar는 가능한 그대로 UTF-8로 쓰고 lone surrogate는
거절한다. 숫자, boolean, null은 V1 identity schema에 존재하지 않는다. null이나
optional property 생략을 허용하지 않으며 `analytics` 같은 비적용 필드는 애초에
V1 schema에 존재하지 않는다. BOM, 공백, 마지막 newline이 없는 UTF-8 bytes다.
문자열 안의 공백·Unicode는 원문 그대로다. UUID가 쓰이면 Core opaque ID의 wire
문자열 그대로, enum은 OpenAPI wire 값 그대로 쓴다.

1. Core는 정본 schema의 exact identity를 만들고 중복 key/ID, role, 값과 VersionSet을 검증한다.
2. `sourceImportSessionIds`를 위 순서로 정렬한다.
3. 전체 root object를 RFC 8785로 serialize해 canonical UTF-8 bytes를 만든다.
4. bytes에 SHA-256을 적용하고 64자리 lowercase hex로 표현한다.
5. Compute는 받은 `inputIdentity`만으로 같은 절차를 **독립적으로** 실행한다.
6. 재계산값과 요청 `expectedInputSetHash`가 다르면 job을 성공시키지 않고
   `INPUT_SET_HASH_MISMATCH`로 끝낸다.

같은 identity는 같은 bytes와 hash를 낸다. grant locator/expiry만 바꾸면 hash는
같고, artifact SHA-256이나 exact VersionSet을 바꾸면 hash가 달라진다. 고정 vector의
`canonicalJson`, `canonicalUtf8Hex`, `expectedSha256`은 동적으로 생성하는 기대값이
아닌 독립 oracle이다.

## 전송, 실행, idempotency

Compute create 요청의 `inputIdentity`만 logical hash 대상이다. `coreNormalizationRunId`,
`coreLogicalJobId`, `coreImportSessionId`, `artifactAccessGrants`, `pinnedInstrumentTerms`,
`deletionGeneration`, `retryPolicyVersion`, `attemptToken`, `Idempotency-Key`, worker/job ID,
attempt/retry count, 시각, callback/result URL, 인증 정보는 제외한다. Grant의 ID/role/hash는
identity와 정확히 일치해야 하며 byte size/content type은 pin한 descriptor와
검증한다. `coreImportSessionId`는 V1 `sourceImportSessionIds[0]`와 일치해야 한다.
`deletionGeneration`은 hash 입력이 아니고 Core publication의 stale-result guard다.
`coreNormalizationRunId`는 Core correlation ID이며 hash 입력이 아니다.

Compute create `Idempotency-Key`의 fingerprint는 ADR-064의 별도 transport/create
fingerprint다. 같은 key와 같은 logical identity, 같은 Core Run ID, 같은 최초
`attemptToken`·generation 등 immutable create field는 한 job으로 수렴한다. grant의
locator/expiry만 새로 받은 replay는 conflict가 아니며 저장된 grant/token을 바꾸지
않고 기존 job을 반환한다. 다른 logical identity 또는 다른 Core Run ID/최초 token/
generation을 같은 key에 붙이면 `409`다. grant의 변경은 별도 CAS update를 쓴다.
logical normalization key는 owner + `inputSetHash` + deletion generation이다.

`coreNormalizationRunId`는 create에서 받은 값을 job persistence, worker restart,
status, completed descriptor/payload 및 idempotent 재조회까지 그대로 반환한다.
Compute가 새 Core Run ID를 생성하지 않는다. `FAILED`에는 별도 payload가 없으므로
status의 correlation identity, input hash specification version, 요청
`expectedInputSetHash`, exact VersionSet 및 safe failure를 유지한다. 지원 규격에서
재계산이 가능했던 실패에는 재계산 `inputSetHash`도 포함하고, 미지원 규격에는 생략한다.
`COMPLETED` payload에는
이 값과 job/attempt/generation, manifest identity, 별도 result hash를 모두 담는다.
Core는 current Run·attempt·generation·identity·VersionSet과 source/result/payload
hash를 각각 확인한 뒤에만 발행한다. descriptor의 `reconciliationManifestHash`는
payload `reconciliationManifest.manifestHash`와 일치해야 한다. `payloadSha256`은 받은
terminal JSON bytes, `resultHash`는 선언된 결정적 결과, role별 `sha256`은 다운로드한
원본 bytes에 각각 적용한다.
