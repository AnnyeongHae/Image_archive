# ADR-IMAGE-ARCHIVE-002 · 공개 정적 갤러리와 개인 RAG API 분리

- 날짜: 2026-09-04
- 상태: Implementing. 목표·구현 권한은 사용자 Final Goal 요청에 근거하며, 정확한 배포 artifact 승인·실서버 검증은 별도다.
- 대체하지 않는 결정: [ADR-IMAGE-RAG-001](IMAGE_RAG_PIPELINE_DECISION.md).

## 결정

기존 공개 529 CASE bundle을 유지한다. 소유자용 `platform/v2/worker/` API, 신규 비공개 `image_archive_v2` Neon schema, snapshot마다 image1024/text512 Qdrant 컬렉션을 추가한다. local CLI는 이미 검증된 캐시를 사용하고 원문·권리·사람 메모·메타데이터 QA·그룹 관계를 보존한다. `ready` 전환과 API snapshot pin은 두 외부 DB 적재가 모두 검증된 뒤에만 한다.

Actions는 허용 소스의 정확한 commit/tree/blob 버전만 읽고, 지원 adapter에서 본문을 추출하여 소유자 public key로 암호화한다. private key는 로컬에만 둔다. 무작위 외부 링크·로그인·유료 자료·자동 LLM 호출·공개 승격은 하지 않는다. 여러 출처 추가는 source registry와 해당 adapter의 작은 canary를 먼저 통과해야 한다.

## 왜 이 구조인가

- 모든 공개 요청에서 DB·모델을 호출하는 방식은 지연·사용량·공격 비용을 높인다. 공개 열람은 정적 제공, 개인 RAG만 동적 호출한다.
- Neon 단독 pgvector는 서비스 수를 줄일 수 있으나, 현재 모델별 캐시와 Qdrant 계획을 버리고 재이관할 근거가 없다. Qdrant는 작은 vector+ID payload, Neon은 원문·메타데이터·권리의 원장이다. 두 서비스의 운영/복구 부담을 수용한다.
- Qdrant 무료 플랜은 복구 원장이 아니다. 단일 노드/미사용 중지 위험에 대비해 로컬 벡터·검증된 export를 보존한다. API 응답에서 Qdrant 실패를 잘못된 빈 검색 성공으로 바꾸지 않는다.
- 한 그룹의 최고 유사 멤버로 순위를 정하지만 화면/응답의 대표는 사람 지정 대표다. 그룹당 top-k 한 자리이며 멤버 자체를 삭제하지 않는다.
- 19,005건 전부를 지금 공개/클라우드로 옮기지 않는다. 우선 실제 이미지 검토·분석이 된 379건, 사용 가능한 텍스트 벡터 377건의 작은 수직 경로를 연결한다.
- 기존 경로 이동은 과거 SHA·승인 계보를 깨뜨릴 수 있어 additive v2를 사용한다. 완전한 standalone legacy 분석 환경까지 만들어졌다고 주장하지 않는다.

## 비용 가드와 한계

신규 문서 임베딩은 이번 이관에서 0회다. 11개 저장 질의로 실제 연결을 검증한다. 새 질의 임베딩은 기본 비활성, 활성화 시 토큰 scope·분당 제한·Neon의 전역 일별 원자적 예약·사용량 영수증을 요구한다. 기본 상한은 20회/일, 보수적 예약 40,000 token/일이다. byte 기반 예약은 정확한 사용량이 아니며, 실제량과 대조하고 실패/불확실 호출을 자동 환불·재시도하지 않는다. 캐시는 isolate별 128개/10분으로, 영구 캐시나 전역 단일 호출 보장이 아니다.

공식 조건 확인: [Workers](https://developers.cloudflare.com/workers/platform/pricing/), [R2](https://developers.cloudflare.com/r2/pricing/), [Neon](https://neon.com/pricing), [Qdrant](https://qdrant.tech/pricing/). 무료 구간 내 설계이지 계정의 기존 사용량·가용성·향후 무료 정책을 보장하는 약속은 아니다. 추가 유료 요금제는 자동 가입하지 않는다.

## 배포·복구

2026-09-04 사전 로컬 백업: 19,583 regular files / 5,962,272,067 bytes. ZIP 4,585,787,810 bytes, SHA-256 `bad9b3ef8e7694251d8d6ce37657c9c6f92547d2b7a2b82cdfd3d488af1cf33f`. 파일별 ZIP readback와 원본 전후 hash가 모두 일치했다. 별도 git bundle은 전체 이력을 포함하며 로컬/원격 main은 `097f03d883e0f720048da74c131f2156d507b61e`에서 일치했다.

백업은 repository 밖 `_image_archive_backups/snapshots/20260904T051425Z-a751e23b0fd1485199ab024b122ad204/`에 있다. **secret도 포함한 로컬 전용 백업이므로 공개 업로드하지 않는다.** `Reference/`·공유 계약·외부 pointer 대상은 원래 위치를 유지한다. 기존 파일 위에 ZIP을 풀지 않고 새 빈 디렉터리에 복원 후 manifest를 검증한다. ACL/ADS/VSS 또는 외부 원격 URL의 가용성 보장은 아니다.

실제 배포 전 `workflows/release_measurement`의 새 artifact/target 사람 승인, 권리·보안·비용·preflight와 외부 receipt를 기록한다. private image approval은 public release 승인이 아니다. Git은 코드/계약/합성 fixture만 scoped stage하며 강제 history 삭제를 기본값으로 삼지 않는다.

## 실행 증거

[운영 문서](../platform/v2/README.md)에 실행 명령과 완료/대기 상태를 기록한다. 상세 로컬 보고서는 parent workspace의 `Reports/2026-09-04-10_이미지아카이브_v2_배포전략과_검증.md`다.
