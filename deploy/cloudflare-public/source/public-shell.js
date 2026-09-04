(function () {
  const form = document.querySelector("#filterForm");
  if (form) {
    Array.from(form.elements).forEach(function (element) {
      element.disabled = true;
    });
  }

  const headline = document.querySelector("#resultHeadline");
  const summary = document.querySelector("#resultSummary");
  const metrics = document.querySelector("#toolbarMetrics");
  const grid = document.querySelector("#grid");
  const loadMore = document.querySelector("#loadMore");
  const activeFilters = document.querySelector("#activeFiltersPanel");

  if (headline) headline.textContent = "공개 프론트 배포 완료";
  if (summary) summary.textContent = "공개 승인된 레퍼런스는 아직 0개입니다. 관리자 검토가 끝난 항목부터 이 화면에 추가됩니다.";
  if (metrics) {
    metrics.innerHTML = '<div class="metric-card"><span class="metric-label">Public</span><strong class="metric-value">0</strong></div>';
  }
  if (activeFilters) activeFilters.hidden = true;
  if (loadMore) loadMore.hidden = true;
  if (grid) {
    grid.innerHTML =
      '<section class="empty-state">' +
      '<h3>프론트는 공개되었습니다.</h3>' +
      '<p>19,005건의 내부 원장을 그대로 노출하지 않고, 공개 승인된 이미지와 프롬프트만 순차적으로 연결합니다. 관리자 화면은 우측 상단의 관리자 로그인에서 접근할 수 있습니다.</p>' +
      '</section>';
  }
})();
