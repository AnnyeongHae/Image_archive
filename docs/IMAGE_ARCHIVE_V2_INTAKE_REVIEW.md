# v2 신규 입고 → 관리자 검토 연결

상태: 2026-09-04 로컬 구현·합성 통합 검증. 실제 인증된 Actions import가 아직 없으므로 실운영 전수 완료가 아니다. 기존 관리자 DB·원본·승인·공개 사이트를 변경하지 않았다.

## 기존 전용 배치 도구를 그대로 쓰지 않는 이유

기존 incremental 실행기는 CASE 전용 소스와 당시 건수 계약을 검증한다. 새 출처 데이터를 그 형식으로 가장하거나, 임베딩이 없는 상태에서 후보 목록을 비워 검토를 완료하면 원문 계보와 승인 경계가 깨진다. 새 변환기는 기존 사람 판정·후보 조립·관리자 화면을 재사용하되 별도 입고/manifest/build 계약으로 검증한다.

```text
인증된 GitHub Actions 인계 → receipt SHA 고정
    + 선택한 각 media_ref → 로컬 원본 SHA/Git blob 확인
    + 기존 관리자 DB의 마지막 확정 commit (저장 중인 초안 제외)
    ↓
파일 / 전체 RGBA 픽셀+비어 있지 않은 원문 일치 → 논리 별칭
    ↓
남은 기존/신규 이미지의 image-only 캐시 확인
    ├─ 누락 → blocked_missing_image_vectors, 호출 0, 검토 spec 생성 안 함
    └─ 모두 있음 → 코사인·프롬프트 후보 + 동결된 새 review run
                       ↓
기존 관리자 1 → 2 → 3 → 4단계, 승인·선택 메모 저장
                       ↓
기존 로컬 승인 handoff / Luna·텍스트 임베딩 대기 outbox
```

## 입력·실행

`actions_import.py`로 실제 로그인된 repo/workflow/run/artifact를 검증한 뒤 생성된 `receipt.json`과 그 SHA-256을 사용한다. `origin_verified=true`만 임의로 적은 JSON이나 로컬 원본 갤러리는 이 영수증을 대체하지 않는다. 변환기는 이전 인증 결과와 저장 바이트 결속을 재검증하며 매번 GitHub에 재접속하지 않는다.

미디어 바인딩은 ignored private JSON 배열이다. 0부터 시작하는 `media_index`로 한 원문의 여러 결과 이미지를 각각 선택한다. 필드는 아래 여섯 개만 허용한다. 예시는 입력 형식이며 실제 승인/실행 데이터가 아니다.

```json
[
  {
    "source_id": "source-id-from-import",
    "source_item_id": "docs/gallery-part-1.md#case-1",
    "media_index": 0,
    "local_path": "data/private-research/local-media/source-image.png",
    "sha256": "replace-with-the-full-verified-file-sha256"
  }
]
```

```powershell
python -B platform/v2/local/review_bridge.py `
  --import-receipt <archive-relative-private-receipt.json> `
  --import-receipt-sha256 <full-sha256> `
  --media-bindings <archive-relative-private-media-bindings.json> `
  --baseline-run-id <current-confirmed-run> `
  --review-run-id <new-unique-run>
```

기본은 읽기 전용이며, `--apply`가 새 동결 파일을 쓴다. 기존 DB의 결정이나 원본을 덮어쓰지 않는다. 누락 벡터가 있으면 `--apply`여도 검토 run을 만들지 않는다. 반환된 요청 키는 재사용/별도 예산 검토를 위한 목록이며 추론 승인이 아니다.

준비가 성공하면 기존 실행기로 접속한다. 운영 중인 포트를 종료하지 말고 비어 있는 포트를 사용한다. `--db` 생략 시 기존 전용 DB에 새 run만 추가하며, 격리 검증은 별도 `.sqlite3`를 명시한다.

```powershell
python -B src/serve_image_admin.py --run-id <new-unique-run> --port 8965 --serve
```

새 run에 `--seed-decisions`를 주지 않는다. 이전 승인/제외·메모·그룹은 읽기 전용 기준선으로 전달된다. 새 이미지 승인은 2·3단계 완료 후 4단계에서 기본 체크되며, 사람이 해제하거나 선택 메모를 저장할 수 있다. 저장 초안과 최종 승인 commit은 구분된다.

## 중복·비용·증거 규칙

- 입고 한 건에 이미지가 여러 장이면 각 이미지 ID를 분리한다. 같은 원문만으로 삭제하지 않는다. 명시적으로 선택하지 않은 이미지/빈 원문 레코드의 개수를 결과에 남긴다.
- 기계 판정은 `exact_file OR (full_pixels AND nonblank_prompt_exact)`다. 신규 내부 대표 우선순위는 JSON 구조 우선이고, 이전에 사람이 확정한 대표는 자동 교체하지 않는다. 더 구조화된 신규 원문도 별칭 manifest에 그대로 보존한다.
- 기존 대표뿐 아니라 **과거 제외된 별칭의 원본 fingerprint**도 조회한다. 별칭 재수집은 기존 대표까지 따라가며, 증거에는 실제 일치한 별칭과 최종 대표를 구분해 남긴다. 서로 다른 이전 대표에 연결되면 자동 병합하지 않는다.
- 기존 pixel hash도 전체 EXIF 반영 RGBA지만 해시 prefix가 다르고 `rgba-exif-v2` 정책 표기가 없다. 서로 직접 비교하지 않는다. 새 정책의 full RGBA 증거가 없는 기준선은 파일 hash만 확정 근거로 쓰고 시각 후보/사람 확인을 거친다. 이 coverage 한계는 manifest에 기록한다.
- 원본 full-pixel 해시와 임베딩 입력의 768px RGB PNG 해시는 다르다. 모델·차원·image-only/text/task/protocol/정확한 prepared 바이트가 같은 캐시만 재사용한다.
- 후보 조립은 기존 bounded 로컬 코사인 구현을 재사용한다. 새 이미지 최대 300장, 기존 기준선 최대 4,000장이다. 이 작업을 Workers 요청 중에 돌리지 않는다. 대규모 ANN 수집을 완료했다고 주장하지 않는다.
- 원문 개행·공백은 유지한다. 전체 source_record/권리/버전은 동결 manifest에 남고 기존 prompt API에서 정확한 원문을 복사한다. handoff에는 그 manifest hash가 연결된다.
- 새 파일·출처·캐시·원문 결속이 바뀌면 HTTP뿐 아니라 AdminStore의 승인 변경도 차단한다. 읽기 전용 원본 증거 경로와 실제 서빙 가능한 private PNG 경로는 별도다.
- `.env`, private key, 외부 절대 경로, 링크/junction 경로를 미디어에 사용할 수 없다. 이미지 최대 15MiB·80MP·정적 프레임만 허용한다.
- 동결 manifest가 32MiB, 개별 동결 산출물이 96MiB를 넘으면 더 작은 선택 배치로 나눠야 한다. 텍스트를 조용히 잘라서 통과시키지 않는다.

## 검증 범위와 남은 작업

합성 테스트는 실제 AdminStore 트랜잭션·로컬 HTTP·기존 prompt/rights/handoff 코드를 사용한다. GitHub origin과 이미지 벡터는 명시적인 합성 fixture이다. 따라서 이것은 실제 사용자 승인·실제 신규 임베딩·배포된 API 검증이 아니다.

실제 로컬 잔여 155건의 원본/미리보기는 정상이고 검증된 캐시 적중은 0건이다. 별도 대상·예산 승인 없이는 새 모델 요청을 하지 않는다. 실제 Actions 실행/로그인, 새 소스의 bounded media 다운로드 연결, 신규 벡터 생성 승인과 해당 캐시 적재는 여전히 필요하다. Luna outbox 생성은 추론 실행이 아니며 기존 자료를 무조건 재분석하는 승인도 아니다.

오프라인 검증:

```powershell
python -B -m unittest qa.test_v2_intake_media qa.test_v2_intake_review qa.test_v2_review_consumers -v
```

추가 설치 없이 기존 Pillow 환경에서 실행한다. GitHub 오프라인 workflow의 AST 검사와 Windows의 통합 테스트 통과를 혼동하지 않는다.
