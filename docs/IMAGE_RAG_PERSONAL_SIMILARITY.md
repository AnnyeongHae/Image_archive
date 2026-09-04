# 개인 유사도 보정 메모

기준 데이터는 2026-09-03 실행 `2026-09-03-voyage-similarity-200-v1`과 실제 검토 파일 `C:/Users/user/Downloads/human-similarity-review-v2.labels.json`이다. 이 문서는 사람 검토 결과를 운영 임계값으로 “확정”하지 않는다. 목적은 다음 검토 우선순위를 더 정직하게 잡는 것이다.

## 먼저 확인된 원인: API-049 / ERK-1365 반복

- `API-049`, `DAV490-276`, `ERK-1365`는 **동일 normalized prompt**를 공유한다.
- `API-049`와 `ERK-1365`는 **같은 파일이 아니고** `sha256`도 다르다.
- decoded `pixel_sha256`도 다르다.
- 둘 다 retention active이며 이미 제외된 archived control이 아니다.
- 두 항목의 Voyage image cosine은 약 `0.9934`로 매우 높다.

즉 이 사례의 직접 원인은 “완전 파일 중복” 하나가 아니라 다음 둘이 섞여 있다는 점이다.

- `API-049`와 `DAV490-276`: 같은 원본/같은 prepared 입력을 공유하는 exact duplicate
- `ERK-1365`: 같은 프롬프트를 공유하고 pHash/dHash도 같지만, **해시가 다른 이미지 파일**이다. 압축, 리사이즈, 재인코딩, 또는 매우 유사한 결과일 수 있으므로 시각 확인 전에는 “별도 렌더링”으로 단정하지 않는다.

이 패턴은 exact delete 근거라기보다, 같은 prompt family가 검색 결과를 과도하게 점유할 수 있다는 신호다.

## 실제 표본 분해

archived-touching control 6쌍은 calibration에서 제외했다. 활성-활성 74쌍만 보면:

- visual positive: 37
  - active `identical`: 0
  - active `near_duplicate`: 0
  - active `same_visual_family`: 37
- negative: 36
  - `same_theme_only`: 5
  - `unrelated`: 31
- unlabeled: 1

중요한 점:

- 이번 표본에는 **활성 쌍 identical 사례가 없다**.
- 따라서 이번 calibration은 “visual family 우선 검토”에는 도움을 주지만, active identical 삭제 cutoff를 정당화하지 않는다.
- archived-touching pair는 전부 calibration-only control로 남겨야 한다.

## 관측된 점수 겹침

활성-활성 쌍 기준 관측치:

- visual positive 최저 cosine: `0.429199`
- negative 최고 cosine: `0.725358`

이 값은 겹침이 매우 크다는 뜻이다. 즉 단일 cutoff 하나로 정확하게 양분할 수 없다.

실무용 후보 band는 다음처럼 해석하는 편이 안전하다.

| band | cosine | 관측 표본 | 해석 |
|---|---:|---:|---|
| 낮은 유사도 음성 편향 | `< 0.42` | 11쌍, positive 0 / negative 11 | 이번 표본에서는 전부 음성이었지만, 전역 rejection 규칙으로 쓰면 안 됨 |
| 경계 혼합 구간 | `0.42 ~ <0.73` | 57쌍, positive 32 / negative 25 | 가장 중요한 수동 검토 구간 |
| 높은 우선 검토 구간 | `>= 0.73` | 5쌍, positive 5 / negative 0 | visual family 후보 우선 검토용. 자동 그룹/삭제 금지 |

이 수치는 **표본 내 관측 밴드**일 뿐이다. “97% 정확도”나 운영 cutoff 승인으로 읽으면 안 된다.

## 운영 제안

1. exact delete는 계속 별도 규칙 유지
   - machine exact: `exact_file OR exact_pixels`
   - human identical: active-active pair에서만 별도 계획

2. visual family는 점수 cutoff만으로 묶지 않는다
   - 현재 구현은 calibration과 사람 검토뿐이다.
   - complete-link / no-chaining은 **향후 그룹 정책 후보**로만 읽는다.
   - known negative edge가 있으면 같은 family 자동 승격 금지

3. 다음 검토 우선순위
   - `>= 0.73`: visual family 고우선 검토
   - `0.42 ~ <0.73`: 경계 혼합 검토
   - `< 0.42`: 낮은 우선순위 음성 편향, 다만 전역 제외 규칙은 아님

4. prompt repetition 대응
   - 같은 normalized prompt 반복은 exact delete가 아니라 검색 다양성 문제로 다룬다.
   - 결과 정렬 단계에서 MMR/다양성 또는 prompt-family cap을 검토할 수 있다.

## 코드 계약

`src/image_rag_eval/calibration.py`의 `calibrate(source, spec, labels)`는 다음을 반환한다.

- archived-touching control 제외 count
- active-only positive/negative/unlabeled count
- 관측 overlap
- 후보 band
- threshold table
- `complete_link_required_for_grouping="recommendation_only"`, `single_link_chaining_allowed=false`, `automatic_grouping=false`, `automatic_deletion=false`

이 출력은 리포트와 우선순위 조정용이다. canonical write, comparison mutation, 자동 승인, 자동 삭제는 포함하지 않는다.
