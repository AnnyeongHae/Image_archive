# GitHub Actions Enablement

상태: 2026-09-01

현재 아카이브 루트는 독립 Git 저장소로 초기화됐고 `.github/workflows/github-source-canary.yml`와 로컬 검증기가 준비돼 있다. 원격 GitHub Actions가 실제로 돌려면 안전한 source allowlist 커밋, `origin` 연결, 첫 push가 남아 있다.

## 현재 완료된 것

- 워크플로는 `workflow_dispatch` 수동 canary 기준으로 고정
- 액션은 full SHA pinning
- top-level permission은 `contents: read`
- schedule, write 권한, DB secret 사용은 금지
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
5. 수동 canary 성공 후에만 schedule 검토

## 경계

- `NEON_DATABASE_KEY`는 GitHub Actions에 넣지 않는다.
- source canary는 metadata-only이며 public release를 수행하지 않는다.
- 원격 schedule은 수동 canary 성공 전까지 추가하지 않는다.
