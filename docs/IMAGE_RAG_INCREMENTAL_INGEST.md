# 이미지 증분 수집·유사도 검토 설계

기준일: 2026-09-03. 기준 실행: `2026-09-03-voyage-similarity-200-v1`.

이 문서는 읽기 전용 재고 조사 결과와 **증분 처리에 필요한 계약**을 구분한다. 사용자는 다음 **최대 300건을 아직 표본에 없는 CASE/legacy 공개 MVP 레코드로 제한**하고 새 임베딩을 진행하는 범위를 승인했다. 현재 브라우저 판정의 authoritative import는 여전히 선행 조건이며, 기존 승인 그룹에 대한 신규 합류 적용은 그 판정 복구·검증까지 보류한다. 이 문서 작성 작업에서는 신규 대상 선택, API 호출, 임베딩, 사람 승인 적용, 원본 삭제 또는 공개 배포를 실행하지 않았다.

## 1. 확인한 대상 범위

### 공개 MVP와 현재 표본은 같은 모집단이 아니다

`deploy/cloudflare-public/public/catalog-data.js`의 실제 CASE 목록과 현재 duplicate index를 비교했다.

| 항목 | 관측 수량 | 의미 |
|---|---:|---|
| 공개 MVP CASE 레코드 | 529 | duplicate index의 `legacy` 레인 Style ID 집합과 일치 |
| CASE가 참조하는 기본 이미지 경로 | 529 | 레코드별 기본 이미지 |
| 공개 번들의 실제 이미지 파일 | 532 | 아래 미참조 파일 3개 포함 |
| 현재 200건 중 CASE/legacy | 74 | 공개 MVP에 포함된 표본 |
| 현재 200건 중 나머지 레인 | 126 | external 118, manual 6, social 2 |
| 아직 표본에 없는 CASE 이미지 레코드 | 455 | `529 - 74`; `529 - 200`이 아님 |
| 위 455건의 고유 원본 파일 SHA-256 | 449 | 파일 해시 기준 고유 개수 |
| 위 455건 중 현재 200건과 파일 해시가 같은 레코드 | 40 | 레인 간 중복 포함 |
| 위 일치 항목을 제외한 CASE 레코드 / 고유 파일 해시 | 415 / 412 | 전체 픽셀 중복 검사는 아직 적용 전 |

532개 파일 중 CASE 목록에서 참조하지 않는 파일은 `assets/images/case12.jpg`, `assets/images/case169.jpg`, `assets/images/case170.jpg`다. 파일이 있다는 이유만으로 새 레코드로 수집하지 않는다.

원래 200건 준비 코드는 모든 indexed lane의 후보를 사용했다. 공개 MVP 529건에서 200건을 뽑은 실험이 아니다. 다음 배치는 사용자가 승인한 **아직 표본에 없는 CASE 중 최대 300건**으로 고정하고, 선택한 정확한 ID 목록을 별도 manifest에 기록해야 한다. 아래 전체 내부 재고를 다음 실행 범위로 사용해서는 안 된다.

### 내부 재고는 배경 정보일 뿐 자동 확장 대상이 아니다

| 항목 | 관측 수량 |
|---|---:|
| canonical 레코드 | 19,005 |
| duplicate index asset 행 / 고유 원본 파일 SHA-256 | 9,001 / 5,974 |
| 현재 데이터셋의 안전한 로컬 경로·캐시 resolver로 연결 가능하며 파일이 존재하는 행 | 8,870 |
| 위 resolver 조건에서 제외된 행 | 131 |
| 연결 가능하지만 현재 200건 표본 밖인 행 | 8,670 |
| 위 표본 밖 행의 고유 파일 SHA-256 | 5,815 |
| 위 표본 밖 행 중 현재 200건 파일 해시와 일치하는 행 | 406 |
| 현재 200건에 없는 고유 파일 SHA-256 | 5,707 |

재고 수량은 현재 index와 경로 연결을 읽어 계산한 결과다. 모든 원본의 전체 해시를 이번 조사에서 다시 계산한 것은 아니다. 실행 전 선택한 대상의 실제 바이트와 기록된 SHA-256을 재검증해야 한다. 연결 가능 여부는 외부 AI 전송·권리·공개 승인 여부가 아니다.

## 2. 현재 구현과 미구현 영역

확인된 재사용 가능 자산:

- 기존 20/50/200 실행의 Voyage 이미지 벡터를 합쳐도 고유 asset ID는 200개다. 모두 1,024차원이다.
- 현재 200건은 원본 파일 해시 136개, prepared image payload 134개를 공유한다. Voyage 캐시 키는 이미지 134개와 검색어 5개를 합쳐 139개다.
- 현재 200건에는 원본 파일 SHA-256, full decoded pixel SHA-256, pHash/dHash가 있다.
- 전체 duplicate index에는 9,001행 모두의 파일 SHA-256이 있지만 pHash/dHash는 128행에만 있다. **full pixel SHA-256 컬럼은 없다.** 새 대상의 픽셀 해시는 로컬 원본에서 계산해야 한다.
- 재고 조사 시점에 이번 그룹 검토의 실제 `group-workflow-v1/decision-imports/*/receipt.json`은 0개였다. 브라우저에서 선택했거나 내보내기를 시도한 상태를 서버가 수신한 사람 승인으로 간주하지 않는다.

재고 조사 시점의 기존 구현은 고정된 20 → 50 → 200 실험과 해당 실행 안의 사람 검토 흐름이다. **조사 당시 200 → 500 또는 임의 신규 배치의 증분 승인 그룹 엔진은 없었다.** 현재 별도 작업에서 신규 전용 준비를 구현하더라도, 실제 신규 임베딩 완료·사람 그룹 판정 적용과 구분해 해당 구현의 receipt와 검증 결과로 판단해야 한다.

- `src/run_image_embedding_comparison.py`: `--sample-limit`은 20, 50, 200만 허용한다. `--max-new-requests`는 기존 실행에서 미캐시 요청 수를 제한하는 장치이며, 새 증분 모집단을 선택하는 기능이 아니다.
- `src/image_rag_eval/scaling.py`: 50개 parent에서 200개로 늘리는 전용 준비다.
- `src/image_rag_eval/comparison.py`와 `carryover.py`: 표본·부모 크기와 승인·예산 조건이 고정되어 있다. 숫자 하나를 바꾸는 것으로 증분 승인 계약이 완성되지 않는다.

새 증분 엔진은 별도의 dry-run 기본 진입점과 검증된 manifest/receipt를 갖추는 방향으로 구현한다. 이 문서는 새 명령의 존재나 실행 완료를 보증하지 않는다. 실제 코드와 receipt를 확인하기 전에는 명령을 실행 가능 CLI로 안내하지 않는다.

### 이후 구현 상태: 준비 계약과 제한된 실행기

위 고정 크기 실험과 별도로 다음 진입점이 추가되었다. 이는 기존 사람 판정 자동 적용이나 전체 증분 승인 엔진이 완성되었다는 뜻이 아니다.

- `src/image_rag_eval/incremental.py`: CASE 범위를 고정한 로컬 준비와 `validate_incremental_prepared` 검증 계약.
- `src/image_rag_eval/incremental_embedding.py`: `plan_incremental_embedding` / `execute_incremental_embedding`. 새 `embedding_item_ids`만 Voyage에 전송하고, 검증된 기존 요청 캐시를 재사용한다.
- `src/run_image_incremental_embedding.py`: 기본 dry-run. 실제 전송에는 `--execute --apply --consent`가 모두 필요하며, 정확한 manifest/source-binding/request-key 승인과 최대 US$0.10 예약 상한을 확인한다.
- 첫 실제 요청은 이미지 1개, 이후 요청은 최대 8개의 독립 이미지 입력이다. 비동기 유료 Batch API가 아닌 표준 요청이다. 호출 사이 간격은 최소 3.1초, 불명확한 실패·429는 자동 재시도하지 않는다.
- 전체 batch 영수증과 각 content-key 예약을 저장한다. 모든 키의 예약을 전송 전에 기록하며, 성공한 batch 영수증만 남고 캐시 기록이 중단된 경우에는 로컬 복구로 완료한다.
- 출력은 새 실행의 `embedding-v1/vectors.json`에 `voyage_image`의 **신규 ID만** 저장한다. 기존 200개 벡터는 이전 실행에 유지되며 후속 로컬 비교에서 함께 읽는다.

실행기 구현 검증: 네트워크 없는 가짜 provider 테스트 **19개 통과**. 이 중 1개는 실제 준비 모듈의 fixture → 실제 준비 validator → 가짜 provider 실행으로 계약 연결을 검증했다. 검증은 실서비스 API 성공·검색 품질·사람 승인 증거가 아니다. 실제 신규 임베딩 상태는 해당 실행의 `embedding-v1/execution-receipt.json`으로 확인해야 한다.

수동 네트워크 실패 복구에는 별도 `--retry-consent` 계약이 있다. 기본 중단 동작은 유지하며, 조사자가 승인 범위 안에서 원인을 확인한 **이미지 1개만 해당 run 전체에서 한 번** 재시도할 수 있다. 이 옵션은 `--max-new-images 1`을 요구한다. 승인 JSON은 실패 ledger의 원본 바이트 SHA-256, 원본 manifest/source-binding SHA-256, 정확한 실패 request key, 로컬 진단 증거 경로와 SHA-256을 고정한다. 원래 실패 attempt와 오류·예약액을 그대로 보존하고 `:manual-retry-1` attempt를 추가한다. 실패 ledger 원본도 별도 archive에 남기며, 실패와 재시도 **양쪽 예약액을 모두** US$0.10 상한에 포함한다. 성공 뒤 일반 재개는 검증된 캐시를 재사용하고, 재시도도 실패하면 자동 또는 두 번째 수동 재시도를 허용하지 않는다. 조사 증거나 보존된 실패 기록이 바뀌어도 중단한다.

## 3. 제안하는 신규 전용 처리 순서

### A. 기존 사람 판정을 먼저 고정

1. 현재 브라우저 선택을 복구·내보내고 authoritative importer가 검증한 판정만 입력으로 받는다.
2. source run, spec hash, decision hash, retained ID, duplicate alias → keeper, 승인 그룹별 member ID와 대표 ID, known negative 판정을 하나의 읽기 전용 기준 revision으로 고정한다.
3. 브라우저 임시 상태, 미완성 초안, 미검토 후보를 승인된 기존 그룹으로 읽지 않는다. 새 배치 도중 기준 revision이 바뀌면 기존 기준으로 끝낼지 다시 검토할지 명시하고, 조용히 혼합하지 않는다.

### B. 선택한 신규 대상의 machine exact 중복을 먼저 제외

1. 사용자가 고른 출처·수량 상한에 맞는 ID 목록을 만든다. 이전 실행에서 검토한 ID와 소스 버전은 checkpoint로 제외한다.
2. 원본 바이트 SHA-256을 검증하고 전체 decoded pixel SHA-256을 계산한다. decoded pixel normalization 규칙과 버전을 저장한다.
3. 비교 대상은 현재 화면의 대표만이 아니라 **기존 모든 alias의 원본 파일·픽셀 해시**다. archived alias도 원본 해시 → 현재 keeper 연결을 유지하여 재유입을 막는다.
4. 신규끼리도 파일·픽셀 중복을 검사한다. `exact_file OR exact_pixels`만 machine exact 근거로 사용한다. 같은 프롬프트, 높은 cosine, pHash만으로 제외하지 않는다.
5. 기존 사람이 승인한 keeper가 있으면 유지한다. 새로운 항목의 프롬프트가 더 좋은 JSON 구조여도 출처·프롬프트 alias로 연결하고, 기존 이미지 대표를 조용히 교체하지 않는다. 대표 변경은 별도 사람 검토다.
6. 신규끼리만 동일한 경우 명시된 프롬프트 품질 우선순위로 keeper를 제안하고 모든 출처·프롬프트·해시 lineage를 남긴다. 제외는 복구 가능한 논리 제외이며 원본 파일 삭제가 아니다.

### C. 남은 고유 입력만 임베딩

- 기존 `request_key`의 provider, model, dimensions, prepared image SHA-256, text, task, protocol을 검증해 동일한 요청만 재사용한다. preprocessing 버전도 manifest/receipt에 고정한다.
- 입력 이미지·모델·차원·전처리·태스크가 바뀌면 다른 캐시 identity다. 다른 임베딩 공간의 벡터를 섞지 않는다.
- 해시 alias가 새 레코드라도 prepared payload와 요청 identity가 같으면 이미 검증된 캐시를 재사용할 수 있다.
- **중복 검사, 소스/외부 AI 전송 승인, 정확한 요청 상한과 비용 상한 확인 전에는 API를 호출하지 않는다.** 무료 잔액을 확인하지 못한 상태에서 비용을 0원으로 단정하지 않는다.
- 이미지 유사도 배치에는 신규 이미지 임베딩만 필요하다. 검색어 임베딩 재생성과 LLM 메타데이터 생성은 이 단계 범위가 아니다.
- 요청별 완료 영수증과 checkpoint를 남긴다. 응답·청구 상태가 불명확한 요청은 자동 재시도하지 않는다. dry-run은 네트워크와 파일 쓰기 모두 0건이어야 한다.

### D. 대표를 중심으로 보여주되 멤버까지 비교

화면 대표와 검색 대표의 목적은 다르다. JSON 프롬프트 품질로 고른 이미지가 임베딩 공간에서 그룹 중심이라는 보장은 없다.

- 확장 규모에서는 대표·검색 exemplar로 기존 그룹 후보를 먼저 찾고, 후보 그룹의 **모든 유지 멤버로 비교를 확장**한다. 실제 최근접 멤버 ID·점수·기존 대표를 함께 기록한다.
- 현재 규모에서는 모든 유지 멤버에 대한 로컬 전수 비교를 기본 검증 기준으로 삼을 수 있다. 신규 300개와 기존 134개를 가정하면 new-old 40,200쌍, new-new 44,850쌍, 합계 85,050쌍이다. 이는 수량 계산이며 실행 시간·검색 품질 실측 주장이 아니다. 실제 비교 수는 중복 제외 후 더 작아질 수 있다.
- 대표만 검색했을 때의 누락을 전수 결과와 비교해 평가하기 전에는 대표 검색을 유일한 라우팅 조건으로 사용하지 않는다.
- 신규→기존 그룹, 신규→기존 비그룹 항목, 신규→신규를 모두 처리한다. 신규끼리 새 그룹을 만들 수 있어야 한다.
- A↔B가 유사하고 B↔C가 유사하다는 이유만으로 A/B/C를 자동 합치는 single-link union을 하지 않는다.
- 사람이 남긴 `unrelated`/`same_theme_only` 등 known negative 경계와 충돌하면 합류를 막거나 별도 재검토 대상으로 표시한다. 여러 기존 그룹을 연결하는 신규 이미지는 자동 그룹 병합 근거가 아니다.
- 점수는 후보 순서와 설명 근거다. 임계값만으로 동일 삭제, 그룹 합류, 프론트 승인을 하지 않는다. 기존 표본의 band는 `IMAGE_RAG_PERSONAL_SIMILARITY.md`의 한계를 그대로 갖는다.

### E. 신규 항목만 사람 검토 → 선택적 태그 → 승인 프론트

운영 화면은 다음 순서를 따른다.

1. computer exact 제외 결과 확인
2. human identical 판정으로 동일 중복 확정
3. human similarity 판정으로 기존 그룹 합류 또는 신규 그룹 구성 승인
4. 앞 단계가 완료되어 남을 이미지가 명확해진 뒤, 선택적으로 자유입력 태그를 작성하고 항목을 승인

태그가 없는 상태는 오류나 미승인을 뜻하지 않는다. 모든 이미지에 태그를 요구하지 않는다. 그룹으로 합류했다는 이유로 대표의 태그를 새 이미지에 자동 복사하지 않는다.

4단계 승인 결과에서 유지·승인된 항목을 곧바로 승인 전용 **private front**에 전달한다. 별도의 5단계 중복 승인 화면은 두지 않는다. 구현 시 미완료 2·3단계, 제외된 ID, 미승인 신규 ID가 프론트에 들어가지 않는지 백엔드에서 재검증해야 한다. 기존 변경 없는 승인 항목은 새 배치 때문에 다시 미승인 처리하지 않는다.

그룹에는 안정적인 group ID와 revision, 부모 revision, 변경한 member ID, 근거 decision hash, 변경 시점을 남긴다. 신규 합류에 대한 사람 승인 없이 기존 승인 그룹 membership을 덮어쓰지 않는다. 이 private front 전달은 실제 공개 Worker 배포·R2 승격·권리 승인과 다르며, 그런 외부 변경은 별도 명시된 승인 경계를 따른다.

## 4. 재사용할 코드와 원장

아래 경로는 아카이브 루트 기준이다.

| 경로 / 진입점 | 재사용 범위 |
|---|---|
| `deploy/cloudflare-public/public/catalog-data.js` | CASE 529개 대상 집합 검증 |
| `src/build_cloudflare_public_frontend.py` | 공개 MVP가 CASE 스냅샷만 포함하는 범위 계약 |
| `data/private-research/duplicate-analysis/current/duplicate_index.sqlite3` | asset ID, original file hash, source/record 연결; read-only 사용 |
| `src/image_rag_eval/dataset.py` / `_all_asset_candidates`, `_manifest_item` | 기존 전체 후보 모집단과 안전한 로컬·캐시 연결 |
| `src/image_rag_eval/expansion.py` / `_select_additional_candidates`, `_prepare_item` | 신규 선택 방식 참고, 원본 검증·전처리·image signals |
| `src/image_rag_eval/similarity.py` / `image_signals`, `_pixel_sha256`, `cosine` | full decoded pixel 기준, 로컬 지표와 벡터 점수 |
| `src/image_rag_eval/machine_dedupe.py` / `build_machine_retention` | file/pixel exact 제외 및 alias lineage |
| `src/image_rag_eval/comparison.py` / `request_key`, `execute_comparison` | 요청 identity, 미캐시 요청·예산·checkpoint; 고정 크기 제약은 별도 확장 필요 |
| `src/image_rag_eval/carryover.py` / `import_parent_cache_and_ledger` | 검증된 부모 캐시 보존 방식; 현재 고정 parent 제약 주의 |
| `src/image_rag_eval/group_workflow.py` / `import_group_workflow_decisions` | 실제 사람 판정 importer, retention overlay, `approved-groups.json`, private front export |
| `src/image_rag_eval/incremental.py` / `validate_incremental_prepared` | CASE-only 신규 준비 원본·전처리·source binding·exact alias 재검증 |
| `src/image_rag_eval/incremental_embedding.py` / `plan_incremental_embedding`, `execute_incremental_embedding` | 동의·비용 상한, 새 ID만 전송, 표준 1→8 이미지 요청, 불명확한 재시도 차단 |
| `qa/test_image_rag_incremental_embedding.py` | 가짜 provider 기반 단위 테스트와 실제 준비 계약 연결 테스트 |
| `docs/IMAGE_RAG_PERSONAL_SIMILARITY.md` | 개인 판정의 표본·점수 겹침·임계값 해석 한계 |

## 5. 구현 수용 테스트

다음 테스트는 **전체 증분 승인 엔진에 요구하는 계약**이다. 위 실행기에서 검증한 부분이 있더라도 이 목록 전체가 현재 통과했다고 주장하지 않는다.

1. **범위·재개:** CASE-only max300 계획은 현재 CASE 74개를 제외하고 최대 300개의 정확한 CASE ID만 기록한다. 다른 레인과 미참조 이미지 3개를 포함하지 않는다. 같은 source version 재실행은 신규 선택·요청을 중복 생성하지 않는다.
2. **기준 승인:** 브라우저 초안이나 receipt 없는 group decision은 승인 기준으로 사용할 수 없다. 실행 중 기준 spec/decision/retention hash가 바뀌면 fail closed하고 기존 결과를 덮어쓰지 않는다.
3. **archived 재유입:** 새 ID가 기존 archived alias의 파일 또는 full-pixel hash와 같으면 현재 keeper로 연결된다. archived ID를 active로 되살리지 않고 lineage와 새 프롬프트를 보존한다.
4. **신규끼리 중복:** 파일 hash가 다르지만 full decoded pixels가 같은 신규 둘은 한 유지 입력이 된다. 같은 prompt에 픽셀이 다른 신규 둘은 삭제되지 않는다. pHash/cosine이 높기만 한 항목은 human identical 후보다.
5. **승인 keeper 보존:** 기존 keeper보다 높은 JSON 프롬프트 tier의 신규 exact duplicate가 와도 keeper와 기존 front ID를 유지한다. 더 좋은 프롬프트는 출처와 함께 alias로 남는다.
6. **캐시·비용:** 기존 요청 identity와 같으면 외부 요청 0건이다. 모델/차원/전처리/입력이 달라지면 잘못 재사용하지 않는다. exact 중복을 제외한 미캐시 입력 수가 request cap과 cost cap 안일 때만 승인된 실행을 시작한다. dry-run은 네트워크/쓰기 0건이다.
7. **대표 누락:** 신규 이미지가 그룹 대표와는 낮은 점수지만 다른 승인 멤버와 높은 점수인 fixture를 만들고, member-expanded 또는 전수 비교가 해당 그룹을 제시하는지 확인한다. 대표-only 결과와 전수 결과의 차이를 기록한다.
8. **chaining·negative:** A↔B, B↔C positive와 A↔C negative fixture에서 자동 3자 union이 발생하지 않는다. 신규 bridge 하나가 두 기존 승인 그룹을 자동 병합하지 않는다.
9. **new-new·membership:** 기존 항목에는 유사하지 않으나 신규끼리 유사한 셋을 하나의 검토 후보로 보여준다. 체크된 부분집합만 사람 승인 후 membership revision에 반영하며 미선택 항목은 삭제·negative로 간주하지 않는다.
10. **4단계 순서·태그 선택성:** 2·3단계 미완료 상태에서 4단계 승인이 프론트를 변경할 수 없다. 완료 후 태그 없이 항목 승인할 수 있고, 필요한 항목에만 태그를 붙일 수 있다. 그룹 태그를 신규 멤버에게 조용히 전파하지 않는다.
11. **private front 정합성:** 4단계 승인 ID 집합과 backend-approved retained ID 집합이 일치한다. 미승인 신규·삭제 alias·과거 검토 control이 노출되지 않는다. 새 batch 미완료가 이전의 무관한 승인 항목을 제거하지 않는다.
12. **보존·외부 경계:** parent run, canonical 원장, 원본 파일, 기존 승인 revision의 해시가 실행 전후 같다. 별도 승인 없는 R2/Qdrant/공개 Worker 쓰기와 물리 삭제가 0건이다.

## 승인된 범위와 다음 실행 조건

**다음 최대 300건의 미표본 CASE-only 범위와 신규 임베딩 진행은 사용자 승인됨**이다. 전체 내부 8,670건으로 확장하는 승인은 아니다. 로컬 exact 검사·정확한 요청 목록·비용 상한 검증을 먼저 완료하고, 승인된 외부 전송·비용 범위 안에서만 신규 임베딩을 실행한다. 현재 브라우저 판정을 authoritative import하기 전에는 기존 그룹을 확정 기준으로 삼거나 신규 membership을 자동 반영하지 않는다. 준비 완료, 임베딩 완료, 사람 그룹 판정 완료, 승인 프론트 반영을 각각 별도 상태와 receipt로 기록한다.
