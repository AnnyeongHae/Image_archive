# Automatic Approval Promotion

## Outcome

`approval-requests.html`에서 수집 후보를 결정한 뒤 JSON을 다운로드하거나 Codex에게 다시 전달하지 않는다. 관리자는 현재 큐 전체를 결정한 후 한 번의 `확정 및 아카이브 승계`로 아래 흐름을 실행한다.

```text
browser draft
  -> same-origin preview API
  -> decision counts confirmation
  -> one-time commit
  -> durable decision ledger
  -> approved/grouped private OpenNana lane
  -> canonical + legacy search projections
  -> refreshed empty/remaining review queue
```

JSON export는 API 장애나 브라우저 복구에만 쓰는 비상 수단이다.

## Local contract

- `GET /api/review/v1/state`: 현재 queue revision과 수량을 읽고 HttpOnly session과 CSRF token을 발급한다.
- `POST /api/review/v1/preview`: 현재 큐 전체와 정확히 일치하는 완결된 결정을 검증하고 5분 유효 commit token을 발급한다.
- `POST /api/review/v1/commit`: preview한 결정이 바뀌지 않았는지 다시 확인하고 멱등적으로 반영한다.

서버는 loopback에만 bind하고, API는 same-origin request만 받는다. apply나 아카이브 승계 중 하나라도 실패하면 큐와 결정 상태를 이전 버전으로 되돌리고 실패로 보고한다.

## Release boundary

이 흐름의 `승인`은 **내 아카이브에서 다시 찾을 가치가 있다**는 큐레이션 결정이다. 다음을 의미하지 않는다.

- 원작자가 프롬프트나 이미지의 재배포를 허락했다.
- 상업적 이용이 가능하다.
- 공개 웹사이트에 프롬프트 원문이나 원본 이미지를 노출해도 된다.
- R2에 원본 이미지 바이너리를 복사해도 된다.

따라서 승계 레코드는 항상 `release_eligible=false`로 시작한다. 공개 export는 프롬프트와 미디어를 제거한 metadata-only 투영이다.

## Future Cloudflare mapping

로컬 API 계약은 배포 때에도 그대로 유지하고 저장소만 바꾼다.

| Local | Future Cloudflare |
|---|---|
| Python `review_server.py` routes | Worker routes with the same `/api/review/v1/*` contract |
| queue/state/ledger JSON | Neon Postgres transaction tables |
| loopback + CSRF | Cloudflare Access identity + same-origin CSRF |
| immutable applied/pending JSON | append-only decision and source-version rows |
| generated internal projection | build job or Worker-side private query |
| remote source image URL | remote reference; R2 copy is a later rights-cleared operation only |

중요한 배포 조건은 preview grant 생성부터 decision ledger, queue consumption, approved lane upsert까지를 하나의 Neon transaction으로 처리하는 것이다. Worker 연결은 Hyperdrive 또는 Neon serverless driver canary로 검증한다. 전체 queue revision과 source content hash가 신청시의 값과 다르면 충돌로 종료한다. 동일 decision batch ID 재시도는 기존 receipt를 반환하여 중복 승계를 막는다.

이 문서는 배포 구조 계약이며 실제 Cloudflare 리소스 생성이나 배포 승인을 의미하지 않는다.
