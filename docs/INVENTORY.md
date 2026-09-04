# 이미지 아카이브 인벤토리

## 숫자 단위

- `18,792`는 웹 대시보드가 조합해 표시하는 **논리 레코드 수**다.
- `1,371`은 2026-08-30 1차 이동 시점의 **레거시 폴더 물리 파일 수**였다.
- 외부 레코드 `18,092`개는 파일 18,092개가 아니라 `external_prompt_records.json`의 `records` 배열에 들어 있는 행이다.

현재 수치는 `src/build_archive_inventory.py`가 파일과 레코드를 다시 읽어 `data/canonical/archive_inventory.json`으로 생성한다.

```powershell
python 08_AGENT_이미지_아카이브/src/build_archive_inventory.py
python 08_AGENT_이미지_아카이브/src/build_archive_inventory.py --apply
```

## 저장 원칙

1. 레코드마다 파일 하나를 만들지 않는다. 18,792개 파일로 분해하면 검색·갱신·배포 비용만 커진다.
2. 장기적으로는 하나의 버전된 canonical store만 정본으로 둔다.
3. JSONL, CSV, 브라우저용 JS, SQLite, 배포용 shard는 정본에서 다시 만드는 projection으로 취급한다.
4. `Reference/`의 원출처 증거는 이동하거나 수정하지 않는다.
5. 권리 확인 또는 내부 생성 검토를 통과한 미디어만 `media/public/` 및 R2 배포 후보로 승격한다.
6. 외부 URL이 있다는 사실은 파일 존재·상업 사용권·재배포 허가를 의미하지 않는다.

## 현재 리팩터링 단계

`Phase 1: safe move`와 `Phase 2A: canonical export`가 완료됐다. 플랫폼 루트, 레거시 호환 경로, 대표 5개 진입 화면, 전체 대시보드, 배포 경계와 QA가 구성됐고, 18,792개 전체가 `data/canonical/archive_records.jsonl`의 버전된 행으로 정규화됐다.

현재 canonical JSONL은 약 250 MB다. 공개 projection은 P1/P2만 포함하고 P3/P4는 관리자 전용으로 완전히 제외한다. 현재 18,815개는 모두 P3 기본값이므로 공개 shard는 0개이며, 권리 승격 전까지 private canonical만 유지한다.

아직 완료되지 않은 것은 기존 브라우저를 canonical shard reader로 전환하는 `Phase 2B`와 실제 Cloudflare/R2 배포 승인이다. 따라서 레거시 입력은 보존하며 배포 상태도 계속 `blocked`다.

## 배지 해석

카드 상단에는 상세페이지 적합도 휴리스틱인 `Candidate / A direct`를 표시하지 않는다. 대신 출처와 권리 신호를 표시한다.

- `Repo MIT · 항목권리 확인`: 저장소 라이선스는 MIT로 관측됐지만 개별 프롬프트·이미지·브랜드 권리는 별도 확인해야 한다.
- `Repo Apache-2.0 · 항목권리 확인`: 저장소는 Apache-2.0이지만 개별 항목 범위는 별도 확인해야 한다.
- `사용권 미확인`: 재사용 범위를 확인할 근거가 없다.
- `직접 생성`: 내부 생성 미리보기이며 공개 승인이나 상품 증거가 아니다.

`상용 사용 가능`은 항목 단위 라이선스 또는 소유권, 표시 의무, 브랜드·초상권, 인간 승인까지 모두 충족할 때만 표시할 수 있다.
