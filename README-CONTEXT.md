# Compute Agent Context

초기 Compute Agent 문서 세트.

- `AGENTS.md`: 진입점/절대 원칙
- `docs/ARCHITECTURE.md`: Compute 내부 구조/의존성
- `docs/DOMAIN.md`: 계산 도메인 의미론
- `docs/WORKFLOW.md`: Codex 작업 절차
- `openapi/compute-api.yaml`: Context의 Core↔Compute API 계약 snapshot

원본 system context의 `DECISIONS.md`, `GLOSSARY.md`, `ROADMAP.md`는 정책 변경이나 스펙 공백이 있을 때 참조한다.

`docs/TRADING_NORMALIZATION_IDENTITY.md`와
`tests/fixtures/trading_review/normalization_identity/v1/`은 Context의 identity 계약·고정
hash vector를 그대로 복제한 snapshot이다. `openapi/CONTEXT_REVISION`에 기준 Context
commit을 기록하고, Integration의 `verify-contract-sync.ps1`에서 OpenAPI와 fixture의
byte 일치를 검사한다. 계약과 fixture의 정본은 Context에서만 변경한다.
