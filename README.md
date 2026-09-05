# Image Archive Platform v2

이미지와 원문 프롬프트를 보존하고, 중복을 제거한 대표 이미지 중심으로 탐색·검색하는 이미지 RAG 플랫폼입니다.

## 현재 배포 상태

- 공개 프론트: [photoposting.shop](https://photoposting.shop)
- 공개 릴리스: v2, 이미지 379개 · 그룹 326개 · 그룹 변형 53개
- 개인 RAG API: `https://api.photoposting.shop`
- 검색 임베딩: Voyage-4-lite 텍스트 임베딩(개인 API 전용)
- 저장소: Cloudflare Workers + R2, 메타데이터/검색 인덱스는 Neon·Qdrant
- 공개 페이지에는 private DB, 벡터, API 키를 포함하지 않습니다.

## 전체 파이프라인

```text
허용된 source 자동 수집 (GitHub Actions)
  → 출처·권리 기록, 파일/픽셀/프롬프트 SHA-256 계산
  → 확정 완전 중복 제외 (원문·출처 관계는 보존)
  → 이미지 임베딩 (Voyage multimodal)으로 유사 후보 탐색
  → 사람 검증: 동일 이미지 제외 / 형식별 그룹 / 개별 유지
  → 유지 이미지 + 원문 프롬프트를 Luna로 분석·태깅
  → 활용 중심 텍스트 생성 (메타데이터 + 프롬프트 + 선택 메모)
  → 텍스트 임베딩 (Voyage-4-lite) 및 Qdrant 적재
  → 공개 프론트에는 승인된 대표 이미지와 원문 프롬프트만 노출
  → 소유자 API는 토큰 인증 후 대표 그룹 단위 RAG 검색 제공
```

새 이미지도 먼저 완전 중복을 차단하고 이미지 임베딩 유사 후보를 사람에게 보여준 뒤, 승인된 항목만 LLM·텍스트 임베딩 비용을 사용합니다. 이미지 임베딩과 텍스트 임베딩은 서로 다른 역할이며 하나의 joint vector로 섞지 않습니다.

## 프론트 기능

- 대표 이미지 카드만 노출하고 그룹 라벨을 누르면 변형을 상세에서 확인
- 원문 프롬프트 복사, 검색, 활용·스타일·배경·인물 필터
- 기본 정렬은 최신순(datetime desc), 모바일 대응
- 공개 이미지에 `참고용 · 권리 미확인` 고지와 출처 링크 표시

## 저장·보안 경계

- 원본·분석 원문·사람 메모·벡터는 private 저장소에 보관합니다.
- 공개 Worker는 정적 v2 산출물만 읽으며 private DB/vector binding을 갖지 않습니다.
- 개인 검색 API는 Access와 API token으로 보호하며 일일 호출 한도는 200회입니다.
- 관측된 저장소 라이선스와 개별 이미지 이용 허가는 별개입니다.
- 비밀값, 원본 이미지, private DB와 벡터는 GitHub에 커밋하지 않습니다.

## 개발·검증

```powershell
node --test platform/v2/tests/*.test.mjs
python -B -m unittest discover -s qa -p 'test_*v2*.py' -q
python platform/v2/local/query_activation.py --prepare
```

배포는 후보 해시와 패킷을 고정하고 사람 승인 후 Wrangler로 실행합니다. 기존 Worker 버전은 롤백용으로 유지합니다.

## 디렉터리

| 경로 | 역할 |
|---|---|
| `platform/v2/` | v2 Worker, 공개 프론트, 계약, 배포·검증 도구 |
| `src/` | 수집·중복검사·Luna 분석·임베딩·DB 적재 |
| `db/` | Neon/메타데이터 스키마와 마이그레이션 |
| `qa/` | 계약·무결성·브라우저·배포 테스트 |
| `docs/` | ADR, 권리 정책, 운영·배포 결정 기록 |
| `data/private-research/` | 로컬 전용 스냅샷·후보·증거(비공개) |

자세한 설계 근거는 [이미지 RAG 파이프라인 결정서](docs/IMAGE_RAG_PIPELINE_DECISION.md)와 [v2 플랫폼 문서](platform/v2/README.md)를 참고하세요.
