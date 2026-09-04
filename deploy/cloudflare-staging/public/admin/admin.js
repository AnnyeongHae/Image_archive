async function loadStatus() {
  const status = document.querySelector("#status");
  try {
    const response = await fetch("/api/admin/v1/status", {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    document.querySelector("#status-list").innerHTML = `
      <div><dt>Access JWT</dt><dd>${payload.access_jwt_validated ? "Validated" : "Blocked"}</dd></div>
      <div><dt>Private records</dt><dd>${Number(payload.private_records || 0).toLocaleString()}</dd></div>
      <div><dt>Private R2 binding</dt><dd>${payload.r2_binding_present ? "Connected" : "Missing"}</dd></div>
      <div><dt>Mutation</dt><dd>${payload.mutation_enabled ? "Enabled" : "Disabled"}</dd></div>
    `;
    status.textContent = "Access와 Worker 내부 인증 검증이 정상입니다.";
  } catch (error) {
    status.textContent = `상태 확인 실패: ${error instanceof Error ? error.message : "unknown error"}`;
  }
}

loadStatus();
