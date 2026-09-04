(function () {
  const STORAGE_KEY = "image-archive-featured-selection-v1";
  const data = window.IMAGE_ARCHIVE_FEATURED_FIVE;
  const grid = document.getElementById("cardGrid");
  const selectionStatus = document.getElementById("selectionStatus");
  const platformRoot = (document.body.dataset.platformRoot || ".").replace(/\/$/, "");

  if (!data || !Array.isArray(data.items) || data.items.length !== 5) {
    grid.innerHTML = '<div class="empty-state">대표 예시 데이터가 준비되지 않았다.</div>';
    selectionStatus.textContent = "현재 선택 가능한 대표 예시를 불러오지 못했다.";
    return;
  }

  const savedId = window.localStorage.getItem(STORAGE_KEY);
  let selectedId = data.items.some((item) => item.reference_style_id === savedId) ? savedId : "";

  function resolvePath(path) {
    return platformRoot === "." ? path : platformRoot + "/" + path;
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderCardMedia(item, index) {
    const imagePath = resolvePath(item.delivery_fallback_path || item.image_path);
    const webpPath = item.webp_path ? resolvePath(item.webp_path) : "";
    const width = Number(item.image_width) > 0 ? ' width="' + escapeHtml(item.image_width) + '"' : "";
    const height = Number(item.image_height) > 0 ? ' height="' + escapeHtml(item.image_height) + '"' : "";
    const loadingAttributes = index === 0
      ? ' loading="eager" fetchpriority="high"'
      : ' loading="lazy" decoding="async"';
    const webpSource = webpPath
      ? '<source srcset="' + escapeHtml(webpPath) + '" type="image/webp">'
      : "";

    return (
      '<picture class="card-media-shell">' +
      webpSource +
      '<img class="card-media" src="' + escapeHtml(imagePath) + '" alt="' + escapeHtml(item.title) + '"' +
      width +
      height +
      loadingAttributes +
      ">" +
      "</picture>"
    );
  }

  function updateStatus() {
    const selected = data.items.find((item) => item.reference_style_id === selectedId);
    if (!selected) {
      selectionStatus.innerHTML = '<strong>아직 선택하지 않았습니다.</strong><br><span class="meta-line">이미지와 Style ID를 비교한 뒤 하나를 고르세요.</span>';
      return;
    }
    selectionStatus.innerHTML =
      '<strong>현재 선택:</strong> ' +
      escapeHtml(selected.reference_style_id) +
      " · " +
      escapeHtml(selected.title) +
      '<br><span class="meta-line">' +
      escapeHtml(selected.selection_rationale) +
      "</span>";
  }

  function copyPrompt(prompt, button) {
    const done = function () {
      const previous = button.textContent;
      button.textContent = "프롬프트 복사됨";
      window.setTimeout(function () {
        button.textContent = previous;
      }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(prompt).then(done);
      return;
    }
    const area = document.createElement("textarea");
    area.value = prompt;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    done();
  }

  function render() {
    grid.innerHTML = "";

    data.items.forEach(function (item, index) {
      const card = document.createElement("article");
      const isSelected = item.reference_style_id === selectedId;
      card.className = "archive-card" + (isSelected ? " is-selected" : "");
      card.setAttribute("data-style-id", item.reference_style_id);

      const tags = (item.tags || []).slice(0, 4).map(function (tag) {
        return '<span class="chip">' + escapeHtml(tag) + "</span>";
      }).join("");

      const techniques = (item.visual_techniques || []).slice(0, 3).map(function (tag) {
        return '<span class="chip">' + escapeHtml(tag) + "</span>";
      }).join("");

      card.innerHTML =
        renderCardMedia(item, index) +
        '<div class="card-body">' +
        '<div class="id-row">' +
        '<span class="style-id">' + escapeHtml(item.reference_style_id) + "</span>" +
        '<span class="selection-badge">' + (isSelected ? "선택됨" : "후보") + "</span>" +
        "</div>" +
        '<div class="meta-stack">' +
        '<h3 class="card-title">' + escapeHtml(item.title) + "</h3>" +
        '<p class="card-summary">' + escapeHtml(item.summary) + "</p>" +
        '<p class="card-rationale"><strong>왜 대표 예시인가:</strong> ' + escapeHtml(item.selection_rationale) + "</p>" +
        '<p class="meta-line"><strong>출처:</strong> ' + escapeHtml(item.source_name) + "</p>" +
        "</div>" +
        '<div class="chip-row">' + tags + techniques + "</div>" +
        '<div class="card-actions">' +
        '<button type="button" class="select-button' + (isSelected ? " is-active" : "") + '" data-action="select">이 예시 선택</button>' +
        '<button type="button" class="copy-button" data-action="copy">프롬프트 복사</button>' +
        (item.source_url ? '<a class="secondary-link" href="' + escapeHtml(item.source_url) + '" target="_blank" rel="noreferrer">원문 출처</a>' : "") +
        "</div>" +
        '<div class="prompt-shell">' +
        '<details>' +
        '<summary class="details-toggle">프롬프트 보기</summary>' +
        '<pre class="prompt-block">' + escapeHtml(item.prompt) + "</pre>" +
        "</details>" +
        "</div>" +
        "</div>";

      const selectButton = card.querySelector('[data-action="select"]');
      selectButton.setAttribute("aria-pressed", String(isSelected));
      selectButton.setAttribute("aria-label", item.reference_style_id + " " + item.title + " 선택");
      card.querySelector('[data-action="copy"]').setAttribute("aria-label", item.reference_style_id + " 프롬프트 복사");

      selectButton.addEventListener("click", function () {
        selectedId = item.reference_style_id;
        window.localStorage.setItem(STORAGE_KEY, selectedId);
        updateStatus();
        render();
      });

      card.querySelector('[data-action="copy"]').addEventListener("click", function (event) {
        copyPrompt(item.prompt, event.currentTarget);
      });

      grid.appendChild(card);
    });
  }

  updateStatus();
  render();
})();
