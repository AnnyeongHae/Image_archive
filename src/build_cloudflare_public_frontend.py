#!/usr/bin/env python3
"""Build the bounded Cloudflare public reference MVP.

The legacy archive remains an immutable internal input.  The public bundle only
contains the 529-case awesome-gpt-image-2 snapshot and its paired local preview
assets.  Private research lanes and administrator tools are never copied.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy" / "current_archive"
DEPLOY_ROOT = ROOT / "deploy" / "cloudflare-public"
OUTPUT = DEPLOY_ROOT / "public"
SOURCE = DEPLOY_ROOT / "source"
ADMIN_URL = "https://image-prompt-archive-staging.andrew4may.workers.dev/admin/"
SOURCE_URL = "https://github.com/freestylefly/awesome-gpt-image-2"
LICENSE_URL = f"{SOURCE_URL}/blob/main/LICENSE"
DISCLAIMER_URL = f"{SOURCE_URL}/blob/main/docs/disclaimer.md"


def replace_required(text: str, pattern: str, replacement: str, *, flags: int = 0) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"required transform did not match exactly once: {pattern}")
    return updated


def build_index() -> str:
    html = (LEGACY / "index.html").read_text(encoding="utf-8")
    html = html.replace("../../favicon.svg", "favicon.svg")
    html = html.replace("Universal Visual Prompt Portfolio", "Image Prompt Archive")
    html = replace_required(
        html,
        r'<div class="header-utility-links" aria-label="운영 도구">.*?</div>',
        (
            '<div class="header-utility-links" aria-label="운영 도구">'
            f'<a href="{SOURCE_URL}" target="_blank" rel="noopener noreferrer">원 출처</a>'
            '<a href="notice.html">출처·사용 범위</a>'
            f'<a href="{ADMIN_URL}">관리자 로그인</a>'
            "</div>"
        ),
        flags=re.DOTALL,
    )
    html = replace_required(
        html,
        r'<nav class="collection-nav" aria-label="컬렉션 전환">.*?</nav>',
        (
            '<nav class="collection-nav" aria-label="컬렉션 전환">'
            '<button type="button" class="collection-tab" data-collection-tab="all" aria-pressed="true">통합</button>'
            '<button type="button" class="collection-tab" data-collection-tab="external-archive" aria-pressed="false">'
            "awesome-gpt-image-2 529"
            "</button></nav>"
        ),
        flags=re.DOTALL,
    )
    html = replace_required(
        html,
        r'\s*<details class="panel-block info-panel resource-block">.*?</details>',
        "",
        flags=re.DOTALL,
    )
    html = html.replace(
        "이 라이브러리는 연구용 레퍼런스입니다. 실제 판매물에 쓰기 전에는 원출처 권리, 제품 사실, 문구 근거를 별도로 확인해야 합니다.",
        "공개 커뮤니티 자료를 학습·연구용 레퍼런스로 재구성한 MVP입니다. 원 출처와 항목 링크를 유지하며, 개별 이미지의 상업 이용 권리를 보증하지 않습니다.",
    )
    html = replace_required(
        html,
        r'\s*<script src="catalog-data\.js.*?</script>\s*<script src="external-catalog-data\.js.*?</script>\s*<script src="generated-preview-assets\.js.*?</script>\s*<script src="source-signals-data\.js.*?</script>\s*<script src="record-presentation-overrides\.js.*?</script>\s*<script src="bul001-template-collection\.js.*?</script>\s*<script src="social-catalog-data\.js.*?</script>\s*<script src="manual-catalog-data\.js.*?</script>\s*<script src="secret-code-catalog-data\.js.*?</script>\s*<script src="opennana-catalog-data\.js.*?</script>\s*<script src="dashboard\.js.*?</script>',
        (
            '\n  <script src="catalog-data.js?v=public-mvp-20260903-1"></script>'
            '\n  <script src="source-signals-data.js?v=public-mvp-20260903-1"></script>'
            '\n  <script src="dashboard.js?v=public-mvp-20260903-1"></script>'
        ),
        flags=re.DOTALL,
    )
    html = html.replace(
        "CASE Style ID는 원본 갤러리의 사례 번호와 연결되고, SOURCECODE Style ID는 외부 소스 색인 레코드를 가리킵니다. 이 자료는 스타일 연구용 레퍼런스이며,\n        실제 판매물에는 제품 원본·표시 문구·사용 권리 검토가 별도로 필요합니다.",
        (
            'CASE Style ID는 <a href="' + SOURCE_URL + '" target="_blank" rel="noopener noreferrer">'
            "awesome-gpt-image-2</a>의 사례 번호와 연결됩니다. "
            '<a href="notice.html">출처·라이선스·사용 범위</a>를 확인하세요. '
            "상업 사용 전에는 각 항목의 원 권리자와 플랫폼 조건을 별도로 확인해야 합니다."
        ),
    )
    return html


def build_dashboard() -> str:
    script = (LEGACY / "dashboard.js").read_text(encoding="utf-8")
    old = 'return "외부 레퍼런스 " + counts["external-archive"] + "개";'
    new = 'return "awesome-gpt-image-2 " + counts["external-archive"] + "개";'
    if script.count(old) != 1:
        raise RuntimeError("public collection label transform did not match exactly once")
    script = script.replace(old, new)
    focus_repairs = (
        (
            '        render();\n        announce(label + "의 전체 시안을 표시합니다.");',
            '        render();\n        document.getElementById("results").focus();\n        announce(label + "의 전체 시안을 표시합니다.");',
        ),
        (
            '      elements.bul001ReviewBanner.scrollIntoView({ block: "start" });\n    }\n    announce("BUL-001의 25개 생성 시안을 비교합니다. 사람 승인 검토 대기 상태입니다.");',
            '      elements.bul001ReviewBanner.scrollIntoView({ block: "start" });\n    }\n    document.getElementById("results").focus();\n    announce("BUL-001의 25개 생성 시안을 비교합니다. 사람 승인 검토 대기 상태입니다.");',
        ),
        (
            '      resetFilters();\n      syncUrlState();\n      render();\n    });\n    announce("조건에 맞는 결과가 없습니다.");',
            '      resetFilters();\n      syncUrlState();\n      render();\n      elements.search.focus();\n    });\n    announce("조건에 맞는 결과가 없습니다.");',
        ),
    )
    for before, after in focus_repairs:
        if script.count(before) != 1:
            raise RuntimeError("public focus repair did not match exactly once")
        script = script.replace(before, after)
    return script


def build_source_signals() -> str:
    raw = (LEGACY / "source-signals-data.js").read_text(encoding="utf-8")
    try:
        payload = json.loads(raw.split("=", 1)[1].rsplit(";", 1)[0])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("unable to parse legacy source signal bundle") from exc
    matches = [
        signal
        for signal in payload.get("signals", [])
        if signal.get("source_id") == "freestylefly-gpt-image-2-legacy"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one public source signal, found {len(matches)}")
    signal = matches[0]
    public_signal = {
        key: signal.get(key)
        for key in (
            "schema_version",
            "source_id",
            "source_code",
            "title",
            "publisher",
            "source_url",
            "source_kind",
            "github_repo",
            "platform",
            "authority",
            "comparability_group",
            "popularity",
            "repository",
            "freshness",
            "release_eligible",
        )
    }
    observation = signal.get("observation") or {}
    public_signal["observation"] = {
        "status": observation.get("status"),
        "observed_at": observation.get("observed_at"),
    }
    public_payload = {
        "schema_version": "public-source-signals-1.0",
        "generated_at": payload.get("generated_at"),
        "source_count": 1,
        "signals": [public_signal],
    }
    return "window.DETAILPAGE_SOURCE_SIGNALS = " + json.dumps(
        public_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ) + ";\n"


def reset_output() -> None:
    resolved_output = OUTPUT.resolve()
    resolved_parent = DEPLOY_ROOT.resolve()
    if resolved_output.parent != resolved_parent or resolved_output.name != "public":
        raise RuntimeError(f"refusing to clean unexpected output path: {resolved_output}")
    if resolved_output.exists():
        for child in resolved_output.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        resolved_output.mkdir(parents=True)


def main() -> int:
    reset_output()
    (OUTPUT / "index.html").write_text(build_index(), encoding="utf-8")
    shutil.copy2(LEGACY / "dashboard.css", OUTPUT / "dashboard.css")
    (OUTPUT / "dashboard.js").write_text(build_dashboard(), encoding="utf-8")
    shutil.copy2(LEGACY / "catalog-data.js", OUTPUT / "catalog-data.js")
    (OUTPUT / "source-signals-data.js").write_text(build_source_signals(), encoding="utf-8")
    shutil.copytree(LEGACY / "assets" / "images", OUTPUT / "assets" / "images")
    shutil.copy2(ROOT / "favicon.svg", OUTPUT / "favicon.svg")
    shutil.copy2(SOURCE / "404.html", OUTPUT / "404.html")
    shutil.copy2(SOURCE / "robots.txt", OUTPUT / "robots.txt")
    shutil.copy2(SOURCE / "_headers", OUTPUT / "_headers")
    shutil.copy2(SOURCE / "notice.html", OUTPUT / "notice.html")
    shutil.copy2(SOURCE / "THIRD_PARTY_NOTICES.txt", OUTPUT / "THIRD_PARTY_NOTICES.txt")
    print(f"built public frontend shell: {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
