# AR-08 실제 HTTP 입력 경계 실측

2026-11-01 KST. **WSL 개발 실측 / 운영 수치 미확정 / production 미연결**.
ADR-071은 계속 DRAFT이며 이 자료로 AR-08~10을 완료하거나 timeout·RSS 한도를 승인하지 않는다.

## 1. 범위와 재현 조건

기존 [Linux 실측](ANALYTICS_RESOURCE_LINUX.md)은 HTTP opener를 파일 stream으로 대체했다.
이번 도구는 별도 부모 프로세스에서 loopback HTTP 서버를 띄우고, fresh client 프로세스에서
변경하지 않은 Analytics downloader·크기/hash 검사·strict JSON parser·입력 adapter를 호출한다.
서버·generator의 객체와 메모리 peak는 client 측정에 포함하지 않는다.

고정 16 episode·32 record의 유효한 합성 Ledger/Manifest 뒤에 whitespace를 stream으로 붙인다.
각 artifact의 byteSize·SHA-256·input identity를 다시 계산하고 전송한다. Golden fixture·실제
Normalization 결과·production VersionSet을 바꾸지 않는다. 다른 크기의 inputHash는 다르며,
같은 profile의 반복에서 inputHash와 논리 document hash를 비교한다.

- 기준 main `6b228f6`, 도구·테스트·사용법 커밋 `8f1a8d9`.
- Python 3.11.16, WSL2 Linux 5.15.146.1, x86_64, glibc 2.35, 논리 CPU 16개.
  실행 전 MemAvailable 약 6.84GiB인 공유 개발 환경이다. 전용 production 호스트가 아니다.
- Lockfile SHA-256 `8b5735f18b372f4a64cc8c6c8ca1d87d8ba85e3af28ce2bade098e20a3fbc75c`.
  FastAPI 0.141.1·cryptography 50.0.2·psycopg 3.3.4·pytest 9.1.1의 설치 버전과 lock을 대조했다.
- Linux 전용 새 checkout의 source 165개를 Windows source와 LF/CRLF 정규화 후 대조했다.
  Source 지문은 `9502d0e98cf5ac453eb06cb1cfb2808cf6233ee25170d3b869f313cc3fd24f5d`,
  새 도구 지문은 `bdc4e5b1d48d1d8971f40d54c5225445d70319b8c3e5fedee5be86bad74d663d`다.
  실제 배포 implementationDigest가 아니다.
- 실험 CLI·prepare·client는 각각 상속한 **RLIMIT_AS 2GiB**와 watchdog 하에서 순차 실행했다.
  이는 virtual address space 제한이며 RSS/cgroup hard limit·운영 권장값이 아니다.
- Read·검증·strict parse 시간과 adapter 시간을 각각 기록한다. 표의 client 시간은 둘을 합친
  검증 완료/거부까지다. Fresh process 생성~종료 시간도 raw report에 별도로 남긴다. RSS는 import를 포함한 client의
  누적 `ru_maxrss`이며 반복의 최대치를 보고한다. OS page cache·서버·kernel buffer는 포함하지 않는다.
- 실제 Core·DB·TLS·Engine·projection·terminal payload 구성/암호화·IPC/spool·heartbeat는
  실행하지 않았다. Synthetic request/grant만 사용하고 credential을 읽거나 저장하지 않았다.

## 2. 결과

크기는 **각 artifact** 기준이며 Ledger와 Manifest가 각각 해당 크기다. MiB=1,048,576 bytes다.
시간은 3회 중앙값으로 p95·SLA 추정이 아니다.

| Profile | 각 artifact MiB | 표본·결과 | Client 중앙값 초 | 최대 RSS MiB |
|---|---:|---|---:|---:|
| fast1m | 1 | 3회 입력 검증 | 0.028 | 32.1 |
| fast16m | 16 | 3회 입력 검증 | 0.182 | 63.7 |
| fast64m | 64 | 3회 입력 검증 | 0.648 | 158.4 |
| max512m | 512 | 3회 입력 검증 | 5.879 | 1055.3 |
| trickle512k | 0.5 | 3회 입력 검증 | 26.232 | 32.1 |
| expiry128k | 0.125 | 3회 INPUT_REFERENCE_EXPIRED | 0.315 | 30.1 |
| cap1m | 1 | 3회 TEMPORARY_DEPENDENCY_FAILURE | 0.0003 | 30.1 |
| watchdog128k | 0.125 | 1회 BENCHMARK_WATCHDOG | — | — |

총 22개 표본 중 입력 검증 15개, 의도한 거부/만료/watchdog 7개다. 완료 profile은 3회 모두
inputHash·논리 document hash가 같다. 512MiB profile의 read/parse만의 중앙값은 5.867초였다.
만료·상한 거부 행은 전체 입력을 받았다는 뜻이 아니다. Watchdog 행은 client가 보고한 시간이
없으며 5초의 실험 supervisor가 종료했다. 이후 반복은 중단했다. MemoryError·OOM·process crash
표본은 없으므로 해당 오류 분류나 production OS 감독이 검증됐다고 주장하지 않는다.

`INPUTS_VERIFIED`는 도구의 입력 검증 완료 상태이며 Analytics Job의 COMPLETED가 아니다.
성공 표본의 논리 document와 input hash 반복 일치를 검사했다. Engine/result/body hash의 결정론을
새로 측정한 것이 아니다. 거부·만료·watchdog 표본은 성공·결정론 통과에 섞지 않는다.

## 3. 확인한 의미와 한계

1. 각 512MiB 두 입력의 read/parse만으로 client RSS **약 1.03GiB**를 관측했다. 작은 JSON
   tree라도 bytearray→bytes·UTF-8 decode 등 입력 바이트 사본의 비용이 크다. Artifact별 512MiB
   기본 설정은 전체 RSS guard가 아니다. 이 관측을 모든 입력의 고정 배수나 안전 상한으로 일반화하지 않는다.
2. 1KiB마다 0.025초 지연한 0.5MiB 두 입력은 **26.232초** 뒤에도 정상 검증됐다.
   현재 20초 socket timeout은 전체 fetch deadline이 아니다. 0.15초 grant는 4KiB/0.02초 전송에서
   약 0.315초 뒤 기존 INPUT_REFERENCE_EXPIRED로 거부됐다. 만료 검사는 read 사이에 수행되며
   blocking read 반환 전 즉시 중단을 보장하지 않는다. 도구의 5초 watchdog 종료는 production의
   timeout·cancel·lease 감독이나 공개 오류 code를 구현한 것이 아니다.
3. Whitespace는 큰 전송 바이트의 비용을 분리하는 입력이다. 큰 문자열·많은 record/Evidence·
   중첩 JSON tree·관계 후보·전체 Engine/output의 비용은 측정하지 않았다. 이전 dense1024와
   이번 최대 입력의 RSS를 더해 production peak를 계산하지 않는다.
4. Loopback·공유 WSL 환경의 결과로 외부 TLS·실제 Core artifact storage·느린 DB·동시 worker·
   production systemd/OOM·강제 종료·취소/lease 경합을 승인할 수 없다. 기본 512MiB보다 큰
   별도 운영 configuration까지 검증했다고 해석하지 않는다.

## 4. 사용법과 검증

도구와 기본 명령은 [tools 안내](../tools/README.md)를 따른다. 예를 들어 작은 입력을 먼저 검증한
뒤 동일한 checkout/lock의 별도 프로세스에서 다음 조건을 적용한다.

```sh
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m tools.analytics_http_baseline run \
  /var/tmp/analytics-http-trickle --artifact-bytes 524288 \
  --send-chunk-bytes 1024 --chunk-delay-seconds 0.025 --repetitions 3
```

- 새 실제 HTTP·CLI 검사 15개 통과. Padding 뒤 exact identity·논리 내용, 변조 거부, 외부 target
  거부, 만료·크기 거부의 실패 집계, 새 프로세스 반복·덮어쓰기 거부를 검증한다.
- Windows 관련 **137 passed**, 전체 **924 passed / 131 skipped**, 기존 AnyIO rewrite 경고 1개.
  Ruff·formatter 274 files·src strict Pyright도 통과했다. Native Linux 관련 **137 passed**.
- 최초 제한된 Windows 관련 검사는 기존 watchdog의 `benchmark child-tree cleanup failed`로
  136 passed / 1 failed였다. Taskkill stderr 세부 원인은 수집되지 않았다. 실패 증적을 보존하고
  프로세스 정리 권한이 있는 실행에서 137개·전체 924개를 통과했다. 테스트·시간 제한을 수정하지 않았다.
- Windows 관련/전체 회귀에는 기존 외부 IPv6 bootstrap을 사용했다. 새 15개 단독 검사와 Linux
  검증·실측은 해당 bootstrap 없이 실행했다. Skip을 통과로 계산하지 않는다.
- Runtime source·계약·설정·migration·lock·golden fixture를 변경하지 않는다. 개발 도구만 추가하므로
  실제 DB·Core E2E와 계약 동기화를 이번 검증으로 새로 수행했다고 주장하지 않는다.

Raw 입력·report·JUnit·로그는 새 Linux 실험 폴더와 저장소 밖 `refinvest-ar08-http`에 보존하며
Git에 커밋하지 않는다. 실험 ID는 `20261031T153410Z`, raw 입력은
`/var/tmp/refinvest-ar08-http-20261031T153410Z/evidence`에 보존했다. CLI가 종료된 뒤 서버·실험
자식을 정리했고 source 동일성도 다시 확인했다.

## 5. 다음 gate

정책 초안의 T/G/S/P와 child memory·parent reserve·동시성은 계속 미확정이다. 다음은 운영 Linux
사양·대표 입력 분포·최대 구조적 JSON/output을 고정하고, 실제 전송·DB 발행·동시성을 포함해
실측하는 단계다. 이번 HTTP 관측을 [정책 리뷰](ANALYTICS_EXECUTION_POLICY_REVIEW.md)에 연결하고,
수치·실패 code·profile pin 승인 및 정본 계약 반영 후 미연결 executor 준비와 production 연결을
별도 PR로 검증한다. Backtest의 600초나 오류 code를 Analytics에 임의 적용하지 않는다.

Determinism impact: none. Point-in-Time impact: none. Temporal/Execution impact: none.
