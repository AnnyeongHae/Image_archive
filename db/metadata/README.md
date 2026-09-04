# 비공개 이미지 자료 DB

`0002_luna_library.sqlite.sql`은 수집 원자료와 Luna 메타데이터 후보를 함께 보존하는 SQLite snapshot 계약이다. 운영 관리자 DB인 `state.sqlite3`와 기존 v1 후보 DB를 변경하지 않는다.

## 생성과 완료 조건

Archive root에서 실행한다. 기본은 dry-run이며 파일 저장은 `--apply`로 명시한다.

```powershell
python -X utf8 src/finalize_luna_full_run.py
python -X utf8 src/finalize_luna_full_run.py --apply
```

현재 고정 승인본은 379개다. 신규 compact 359개 및 재사용 v1/v2 20개의 결과와 신규 실행 token 계측이 모두 갖춰져야 최종 완료 조건을 통과한다. `--allow-partial --apply`는 명시적으로 미완료인 중간 snapshot만 만든다. 실제 최신 경로·완료 수·SHA는 반환값과 해당 `receipt.json`, `execution-summary.json`으로 확인한다. 디렉터리 이름의 정렬 순서로 최신본을 추정하지 않는다.

저장 위치: `data/private-research/image-rag-admin/metadata-candidates/full-library-v3/snapshots/<snapshot-key>/library.sqlite3`

Snapshot은 수정하지 않는다. 보정이나 새로운 승인은 새로운 입력 증거 및 snapshot으로 기록한다. 이전 snapshot도 당시 상태로 보존한다.

## 테이블 역할

| 자료 | 테이블 | 사용 원칙 |
|---|---|---|
| 원자료·이미지 참조·권리 | `source_items`, `assets`, `asset_locations` | 이미지 바이너리는 삽입하지 않고 로컬 위치와 SHA를 유지한다. |
| 원문·명시적 교체 항목 | `prompts`, `source_prompt_arguments`, `source_prompt_argument_parses` | 정확한 원문과 리터럴 표기·문자 위치를 보존한다. 실행 코드로 취급하지 않는다. |
| 사람 결정·메모·유사 그룹 | `human_notes`, `approval_groups`, `group_memberships`, `archived_aliases` | 확정 대표와 중복 보관 별칭을 구분한다. 메모는 원문 그대로 보존한다. |
| 분석 작업·후보 | `analysis_runs`, `analysis_tasks`, `analysis_results` | 이미지 승인과 메타데이터 검수를 구분한다. 스키마 버전은 행별로 확인한다. |
| 활용 사전·연결 | `taxonomy_versions`, `taxonomy_terms`, `taxonomy_aliases`, `usage_assignments` | ID 관계는 정규화하고 한국어/영어 별칭은 사전에 한 번 둔다. |
| 자유형 설명 | `analysis_results`의 `visual_json`, `prompt_json`, `freeform_json` | 스타일·배경·슬롯·제약 등의 비정규화 내용은 JSON으로 둔다. |
| 검토·보정 증거 | `candidate_qa`, `quality_reviews`, `candidate_normalizations`, `analysis_result_history`, `literal_format_repairs`, `draft_format_backups` | 원결과와 파생본·이전 결과를 구분한다. 이전 결과는 현재 검색 문서에 합치지 않는다. |
| 사용량 증거 | `token_receipts`, `token_turns`, `receipt_turns`, `turn_items` | 실제 완료 turn의 관측값이다. 청구액이나 이미지별 비용으로 해석하지 않는다. |
| 로컬 확인용 검색 | `diagnostic_documents`, `diagnostic_fts` | 공개 검색·벡터 RAG가 아닌 비공개 진단용이다. |

`source_items.approval_state`는 `image_approved`, `retained_unchecked`, `archived_alias`, `unreviewed`를 구별한다. 655개 관리 자료 모두를 넣더라도 승인되지 않은 항목에 분석을 수행하거나 공개를 허용하는 것은 아니다.

## 읽기 예시

가능하면 SQLite를 read-only URI로 연다. 아래 SQL은 비공개 확인용이며 API 공개 권한을 부여하지 않는다.

```sql
-- 이미지 한 건의 원문과 현재 분석 후보. 분석이 없는 원자료도 조회된다.
SELECT i.style_id, i.approval_state, i.rights_json, p.original_text,
       r.result_schema, r.effective_json, r.review_status
FROM source_items AS i
LEFT JOIN prompts AS p ON p.sha256 = i.prompt_sha256
LEFT JOIN analysis_results AS r ON r.item_id = i.item_id
WHERE i.style_id = 'BST-001';

-- 확정 대표와 펼쳐볼 구성원. 보관 중복 별칭은 이 목록에 섞지 않는다.
SELECT g.group_id, rep.style_id AS representative_style_id,
       member.style_id AS member_style_id, m.is_representative
FROM approval_groups AS g
JOIN source_items AS rep ON rep.item_id = g.representative_item_id
JOIN group_memberships AS m ON m.group_id = g.group_id
JOIN source_items AS member ON member.item_id = m.item_id
WHERE rep.style_id = 'BST-001'
ORDER BY m.is_representative DESC, member.style_id;

-- 상태와 검증 가능한 사용량. NULL은 0으로 치환하지 않는다.
SELECT state, usage_state, count(*) FROM analysis_tasks GROUP BY state, usage_state;
SELECT kind, count(*), sum(total_tokens), count(*) - count(total_tokens) AS unknown_count
FROM token_receipts GROUP BY kind;
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

그룹 인지 텍스트 진단은 `image_rag_eval.luna_library_store.diagnostic_search(connection, query, limit)`를 사용한다. 일치한 하위 이미지의 ID는 `matched_style_ids`에 남기고 한 그룹의 기본 결과는 대표 하나로 접는다. 이 함수는 단순 텍스트 확인용으로, 의미 유사도 점수나 최종 ranking 품질을 제공하지 않는다. 운영 RAG에서는 후보 검색 → 그룹 단위 점수 집계 → 서로 다른 그룹 top-k → 선택 시 하위 결과의 경계를 별도로 구현·검증해야 한다.

## 보존과 배포 경계

- 모든 결과는 `needs_review`이고 메타데이터 사람 승인 및 공개 가능 값은 false다. `public_search_items`는 비어 있어야 한다.
- 원자료에 쓰인 라이선스 문자열은 권리 검증 결과가 아니다. 권리·공개 release gate는 별도다.
- `raw_json`/`raw_sha256`과 `effective_json`/`effective_sha256`은 다를 수 있다. 알려진 무손실 형식 정규화 이력을 함께 확인한다.
- 현재 raw schema 통과 수는 최초 시도 성공률이나 의미 정확도가 아니다. 실패·재시도·보정 비용도 token 기록에 남는다.
- 캐시 입력은 입력의 부분집합, reasoning 출력은 출력의 부분집합이다. 두 값을 합계에 다시 더하지 않는다. 고정 prefix의 효과와 긴 세션의 캐시 효과를 이 계측만으로 분리할 수 없다.
- 이 전체 보존본에는 긴 프롬프트·실행 증거가 포함된다. 배포할 때에는 작은 서비스용 projection과 비공개 증거 저장소를 분리하고, 이 snapshot 자체를 공개하지 않는다.
