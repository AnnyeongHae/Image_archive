# GitHub Actions Enablement

상태: 2026-09-01

현재 아카이브 루트는 독립 Git 저장소이며 오프라인 canary와 공개 GitHub source의 일일 metadata 관측 workflow가 분리돼 있다. OpenNana의 DB-backed delta 수집은 아직 활성 workflow가 아니다.

## 현재 완료된 것

- `github-source-canary.yml`: push/수동 offline contract
- `github-source-daily-observation.yml`: 하루 1회 + 수동 public GitHub metadata snapshot
- 액션은 full SHA pinning
- top-level permission은 `contents: read`
- GitHub public-source canary는 schedule, write 권한, DB secret 사용을 금지
- 일일 관측도 DB secret, package install, git push, prompt/image body download를 하지 않는다
- 원격 저장소 commit SHA와 Git blob SHA를 수집해 추후 Neon ingest가 멱등적으로 비교할 수 있게 한다
- 대용량 canonical/legacy/media/private 자료는 GitHub에 올리지 않음

## 로컬 부트스트랩

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\init_platform_git.ps1
```

원격까지 함께 지정하려면:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\init_platform_git.ps1 -RemoteUrl https://github.com/<owner>/<repo>.git -Apply
```

## 원격 활성화 순서

1. 로컬 Git 초기화
2. 검토된 source allowlist만 stage하고 `qa/validate_repository_boundary.py` 통과
3. 원격 `origin` 연결 후 첫 push
4. GitHub에서 `Actions` 탭의 `GitHub source collector canary` 수동 실행
5. `GitHub source daily observation`을 수동 실행해 live metadata artifact 확인
6. schedule 결과를 Source Master에서 수집 run으로 승계할 DB adapter를 별도 검토
7. 그 후에만 OpenNana delta collector와 Neon secret 사용 여부 결정

## 경계

- source canary는 metadata-only이며 public release를 수행하지 않는다.
- 일일 GitHub 관측은 수집 후보를 승인하거나 public archive로 승계하지 않는다.
- repository license는 개별 prompt/image의 권리 허가로 간주하지 않는다.
- OpenNana schedule 초안은 `deploy/github-actions/opennana-daily-sync.blocked.yml`에 보존돼 있으며 활성 workflow가 아니다.
- 로컬 Neon DSN을 GitHub Secrets로 전송하지 않았다.
