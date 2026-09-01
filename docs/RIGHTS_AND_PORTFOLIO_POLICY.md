# 이미지 아카이브 권리 및 포트폴리오 공개 정책

상태: 2026-09-01 로컬 운영정책 확정안
범위: `08_AGENT_이미지_아카이브`가 수집·보관·표시하는 프롬프트, 이미지, 메타데이터, 소스코드
주의: 이 문서는 보수적인 제품 운영 기준이며 법률 자문이 아니다.

## 1. 핵심 원칙

`MIT`, `Apache-2.0` 같은 저장소 라이선스와 “비상업 포트폴리오” 표시는 개별 프롬프트·이미지의 공개 권리를 자동으로 만들지 않는다.

항목마다 다음 권리를 독립적으로 판정한다.

1. 접근: 공개 URL을 열람할 수 있는가
2. 수집: 내부 연구 사본을 저장할 수 있는가
3. 공개 표시: 프롬프트 전문 또는 이미지 사본을 포트폴리오에서 보여 줄 수 있는가
4. 재배포·변형: 원본 파일이나 파생물을 제3자에게 제공할 수 있는가
5. 상업적 재사용: 광고·납품·유료 서비스에 사용할 수 있는가
6. AI 이용: 학습·파인튜닝·검색 인덱싱·생성 참조에 사용할 수 있는가

한 항목이 `3`을 통과했다고 해서 `4~6`도 통과한 것은 아니다. “출처 표기”, “비상업”, “연구용”이라는 문구는 실제 이용허락을 대체하지 않는다.

## 2. 라이선스 해석

- `MIT`, `Apache-2.0`, `BSD`: 라이선스가 **실제로 적용되는 파일**에는 폭넓은 사용을 허용할 수 있다. 고지 보존, 변경 표시 등 각 조건을 지킨다.
- 저장소의 `LICENSE`가 코드만 대상으로 하거나, 이미지·프롬프트가 외부 링크·사용자 기여·제3자 자산이면 별도 권리 증거가 필요하다.
- 공개 GitHub 저장소에 라이선스가 없으면 열람·포크 가능성과 재사용 허가는 다르다. 기본 저작권 상태로 취급한다.
- `CC BY`: 대상 저작물과 적용 범위가 명확하고 저작자 표시·변경 표시 등 조건을 충족할 때 공개 후보가 될 수 있다.
- `CC BY-NC`: 사용 목적에 따라 판단되는 회색지대가 있으므로 자기홍보 성격의 공개 포트폴리오를 자동 허용하지 않는다.
- 상표, 로고, 인물 초상·퍼블리시티·개인정보, 제3자 저작물은 저작권 라이선스와 별개로 검토한다.

## 3. 항목별 공개 등급

출처 사이트 전체가 아니라 **개별 항목**에 등급을 부여한다.

### P1 `public_item_cleared`

필수 조건:

- 프롬프트 또는 이미지에 적용되는 라이선스·직접 허락 증거가 있음
- 적용 범위와 필요한 고지 조건이 확인됨
- 공개 미리보기와 원문 표시 범위가 명시됨
- 상표·인물·제3자 권리 위험이 없거나 별도 해결됨

허용:

- 공개 아카이브에서 승인된 범위만 표시
- 조건을 충족한 압축 미리보기 제공

별도 판정:

- `commercial_reuse_status`
- `derivative_reuse_status`
- `ai_use_status`

### P2 `public_metadata_link_only`

적용:

- 출처는 공개지만 프롬프트 전문·이미지 사본의 공개 권리가 불명확함
- 소셜 게시물, 혼합 출처 갤러리, 외부 링크 이미지, 저장소 라이선스 범위가 불명확한 예시

허용:

- 제목, Style ID, 작성자·출처명, 원문 링크, 수집일, 내부 분류 태그
- 직접 링크를 통한 원출처 이동

금지:

- 프롬프트 전문 재게시
- 원격 이미지 미러링 또는 R2 공개 승격
- “상업 사용 가능” 표시

### P3 `private_reference_only`

적용:

- 내부 검색·비교에는 유용하지만 공개 표시 근거가 부족함

허용:

- 인증된 관리자 화면과 로컬 비공개 저장소에서 검토 (`portfolio_visibility=admin_only`)
- 내부 검색, 비교, RAG 참조, 생성 브리프 조립 같은 관리자 전용 연구 워크플로
- 출처·권리 증거를 보강하기 위한 내부 처리

금지:

- 비로그인 아카이브 노출
- 공개 검색 인덱스·미디어 번들 포함

### P4 `blocked`

적용:

- 인증·유료·접근 제한을 우회해야 함
- 명시적 재배포 금지 또는 삭제 요청이 있음
- 출처 위조·불명, 권리 충돌, 해결되지 않은 중대한 인물·상표 위험이 있음

처리:

- `portfolio_visibility=admin_only`의 관리자 검역 레인에서만 사유 확인
- 공개·재배포·다운로드 재활용·RAG 투입·생성 참조·AI 이용 금지
- 차단 사유와 판단 시점만 보존

## 4. 필수 메타데이터

```text
rights_evidence_url
rights_evidence_observed_at
license_status: verified | declared | missing | conflicting
license_id
license_scope: repo_code_only | repo_content | item_specific | unknown
portfolio_visibility: public | metadata_link_only | admin_only
rights_tier: P1 | P2 | P3 | P4
public_prompt_status: allowed | not_allowed | unknown
public_preview_status: allowed | not_allowed | unknown
commercial_reuse_status: allowed | not_allowed | unknown
derivative_reuse_status: allowed | not_allowed | unknown
ai_use_status: allowed | not_allowed | unknown
attribution_required
attribution_text
trademark_risk: none | possible | clear
personality_risk: none | possible | clear
reviewed_by
reviewed_at
```

`unknown`은 허용이 아니다. 공개 빌더는 필요한 필드가 없거나 오래되었거나 상충하면 fail closed한다.

## 5. 기본값과 승격 규칙

새로 수집한 제3자 항목의 기본값:

```text
license_status=missing 또는 declared
license_scope=unknown
portfolio_visibility=admin_only
public_prompt_status=unknown
public_preview_status=unknown
commercial_reuse_status=unknown
derivative_reuse_status=unknown
ai_use_status=unknown
```

사람의 콘텐츠 승인·중복 승인과 권리 승인은 별도다. 승인 큐에서 `approve`를 눌러도 `canonicalization_pending`까지만 이동하고 공개 등급은 바꾸지 않는다.

P1 승격에는 다음이 모두 필요하다.

1. 항목 수준 권리 증거
2. 필요한 고지문 자동 생성 가능
3. 공개 표시 범위 명시
4. 인물·상표 위험 판정
5. release gate의 독립적인 사람 승인

## 6. 포트폴리오 v1 운영 결론

- 비로그인 공개 화면은 P1 항목과 P2 메타데이터·원문 링크만 노출한다.
- 권리 미확정 이미지 대신 자체 생성·직접 소유·명시적 허락을 받은 미리보기를 우선한다.
- P2/P3/P4의 원본 이미지는 로컬 또는 private R2에만 보관하고 공개 CDN URL을 만들지 않는다.
- “상업화 가능” 배지는 `commercial_reuse_status=allowed`의 항목별 증거가 있을 때만 표시한다.
- 플랫폼 **소스코드**를 MIT로 공개할 수는 있지만, `CONTENT_RIGHTS.md`와 `THIRD_PARTY_NOTICES`로 아카이브 콘텐츠가 코드 라이선스에 포함되지 않음을 명시한다.

## 7. 공식 참고 근거

- GitHub, Licensing a repository: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository
- MIT License: https://choosealicense.com/licenses/mit/
- Apache License 2.0: https://choosealicense.com/licenses/apache-2.0/
- Creative Commons licenses: https://creativecommons.org/share-your-work/cclicenses/
- Creative Commons NonCommercial interpretation: https://wiki.creativecommons.org/wiki/NonCommercial_interpretation
