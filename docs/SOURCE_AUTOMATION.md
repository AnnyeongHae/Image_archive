# Source Automation and Approval Operations

## Purpose

외부 프롬프트 출처를 지속적으로 관측하되, 자동화는 사람의 명시적 승인 결정을 넘지 않는다. `source-admin.html`은 출처와 수집 레인의 상태를 보여 주고, `approval-requests.html`은 후보 단위 결정과 서버 확정을 담당한다.

## OpenNana lane

```text
src/opennana/run_pipeline.py
  -> collect.py
  -> data/private-research/opennana/raw/
  -> normalize.py
  -> data/private-research/opennana/staging/
  -> dedupe.py (18,792 canonical comparison)
  -> build_review_queue.py
  -> data/private-research/opennana/review_queue/current.json
  -> legacy/current_archive/opennana-review-data.js
  -> approval-requests.html
  -> review_server.py: preview + explicit commit
  -> apply_decisions.py
  -> durable decision ledger + canonicalization_pending
  -> build_archive_lane.py
  -> private OpenNana archive lane + canonical/dashboard projections
```

Review API commit은 현재 큐 전체의 명시적 결정, 현재 queue revision, 각 content hash, same-origin session, CSRF, 1회용 commit token을 모두 재검증한다. 승인·그룹은 내부 참고 아카이브로 승계하고, 보류·제외는 활성 큐에서 제거하되 불변 이력과 결정 원장을 남긴다. 권리 검토, 상업 이용 승인, 공개 export에 원문·미디어를 포함하는 것, 배포는 여전히 이 자동화 범위 밖이다.

## Schedule

Codex heartbeat `OpenNana 기준선 이후 신규 수집`이 매일 오전 9시(Asia/Seoul)에 현재 작업에서 실행된다. 과거 상세 백필은 하지 않는다. 최초 1회 `run_daily_sync.py --fetch --apply --baseline-only`로 현재 공개·무료 목록의 ID와 목록 메타데이터 버전만 기준선에 기록한다. 이 단계는 상세 API, raw/staging, 승인 큐, 정본을 변경하지 않는다.

기준선이 준비된 뒤 `run_daily_sync.py --all-free --fetch --apply`는 공개·무료 목록을 신규 감지용으로 대조하고, 기준선에 없던 ID 또는 목록 메타데이터가 실제로 바뀐 항목을 모두 상세 조회 대상으로 잡는다. 여기서 `100`은 상세 처리 배치 크기일 뿐 실행당 총량 상한이 아니다. 변경분이 40건이면 40건, 140건이면 100건 + 40건 두 배치로 끝까지 처리한다. OpenNana 목록 응답에는 신뢰할 수 있는 게시일 필드가 없으므로 “어제 이후”는 추정 날짜가 아니라 어제까지 관측한 ID·버전 watermark로 판정한다. 기준선이 없으면 일일 실행은 네트워크 호출 전에 실패한다.

기존 `run_pipeline.py --fetch --apply --max-details 20`은 bounded canary로 남긴다. 일일 운영 모드는 상세를 작은 배치로 처리하고, `normalize → dedupe → review queue`까지 성공한 배치에 한해서만 checkpoint를 전진시킨다. 중간 실패 항목은 다음 실행에서 다시 시도한다. 기준선 생성은 과거 프롬프트 본문을 수집하는 작업이 아니며, 이미 중단 전에 완료된 항목을 자동 삭제하지도 않는다.

## Manual bounded backlog recovery

기준선에는 존재하지만 상세 처리 이력이 없는 과거 무료 항목은 일일 스케줄과 분리해 수동 복구한다.

```powershell
python src/opennana/run_backlog_sync.py --fetch --apply --max-details 300
```

`--max-details`는 실행마다 반드시 명시하는 총량 상한이며 100보다 클 수 있다. `100`은 API 목록 페이지 및 완료 체크포인트 배치의 최대 크기일 뿐 전체 실행 상한이 아니다. 예를 들어 250건은 `100 + 100 + 50`으로 처리한다. 각 배치가 `raw → normalize → dedupe → review queue`까지 끝난 뒤에만 상세 처리 watermark를 기록하므로 중단 후 완료 지점부터 재개한다. 이 레인은 자동 스케줄에 연결하지 않으며 private 승인 큐만 갱신한다. 정본 승격, 공개 반영, 권리 승인, 원격 이미지 다운로드는 포함하지 않는다.

## Fail-closed boundaries

- robots 또는 Content-Signal이 바뀌면 중단한다.
- 403/429와 API 필드 드리프트는 성공으로 간주하지 않는다.
- 유료 프롬프트 본문은 저장하지 않는다.
- 원출처 이미지는 URL만 보관하고 바이너리는 다운로드하지 않는다.
- exact duplicate만 자동 접을 수 있다. near duplicate와 remix family는 사람이 판단한다.
- 동일 출처의 미변경 버전과 과거에 이미 관측한 exact prompt hash는 다시 승인 큐에 넣지 않는다. occurrence와 출처 이력은 남긴다.
- 항목별 브라우저 버튼은 초안이다. 전체 큐 수량 요약을 확인하고 다시 명시적으로 commit해야만 원장과 내부 아카이브가 바뀐다.
- 승인도 권리·상업 이용·릴리스 승인이 아니다.

## Validation

```powershell
python qa/validate_opennana_workflow.py --write-report
python qa/validate_opennana_review_queue.py
node legacy/current_archive/tools/smoke_approval_requests.mjs
node legacy/current_archive/tools/smoke_source_admin.mjs
python qa/validate_platform.py --write-report
```
