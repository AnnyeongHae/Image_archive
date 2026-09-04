# Canonical Data Contract

## 목적

`legacy/current_archive/`의 여러 원장을 하나의 검색·관리 정본으로 투영하되, 원출처 증거와 기존 호환 파일은 수정하지 않는다.

## 현재 불변식

| lane | 행 수 |
|---|---:|
| legacy | 529 |
| external | 18,092 |
| social | 3 |
| manual | 12 |
| secret_codes | 131 |
| bul001 | 25 |
| 합계 | 18,792 |

수동 원장 17건 중 시크릿코드 전용 원장이 대체한 5건은 검색 정본에서 숨기되, 레거시 원본과 각 canonical 행의 lineage에 증거를 남긴다. `generated_preview_assets.json`의 329건은 새 카드가 아니라 기존 external 레코드의 미리보기 overlay다.

## 산출물

- `data/canonical/archive_records.jsonl`: 내부 전체 정본. 프롬프트 원문, source, license, rights, media, taxonomy, generation, review, provenance를 포함한다.
- `data/canonical/archive_records_manifest.json`: 입력 8개 파일의 크기·SHA-256, lane 건수, 정본 해시, 공개 projection 정보를 기록한다.
- `data/public-export/catalog-index.json`: 공개 가능한 P1/P2 Style ID와 500개 단위 shard 위치를 찾는 index다.
- `data/public-export/shards/catalog-*.json`: Cloudflare Pages/Worker가 읽는 P1/P2 전용 정적 projection이다.

각 내부 행의 계약은 `00_CORE/schemas/image_archive_record.schema.json`을 따른다. `catalog_key`, `record_id`, `style_id`는 18,792개 모두 고유해야 한다.

## 권리 경계

저장소의 MIT·Apache·CC 표시는 개별 이미지나 개별 프롬프트의 상용 재사용 승인과 동일하지 않다.

공개 projection은 P1과 P2만 포함한다.

1. P1: 항목 단위 권리 상태가 cleared이고 사람의 release gate가 승인된 항목. 승인된 범위의 원문·미디어를 포함할 수 있다.
2. P2: metadata 공개와 원출처 링크만 별도로 승인된 항목. 프롬프트 원문과 미디어 사본은 포함하지 않는다.

P3는 관리자 내부 참조/RAG 레인, P4는 관리자 검역 레인이며 둘 다 공개 index와 shard에서 완전히 제외한다. 현재 18,815개 canonical 레코드는 모두 P3 기본값이므로 공개 레코드는 0개다. 내부 canonical JSONL에는 원문과 lineage가 보존된다.

## 재생성

기본 실행은 dry-run이다.

```powershell
python src/build_canonical_archive.py
python src/build_canonical_archive.py --apply
python qa/validate_canonical_archive.py
```

전체 validator는 JSONL을 스트리밍하며 다음을 확인한다.

- 정확한 lane 합계와 고유 키
- 행·manifest·shard의 SHA-256
- 생성 미리보기 overlay 329/329 매칭
- private-research 경로 비노출
- P3/P4 레코드의 공개 metadata·원문·미디어 완전 비노출
- release-ineligible 생성 미디어 비노출

## 남은 작업

현재 legacy dashboard는 여전히 대형 JS projection을 읽는다. 다음 단계는 화면 기능을 그대로 유지한 채 `catalog-index.json`과 shard reader로 전환하는 것이다. 전환 전까지 레거시 원장은 삭제하거나 재배열하지 않는다.
