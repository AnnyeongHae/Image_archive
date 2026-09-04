"use strict";
(() => {
  const $ = selector => document.querySelector(selector);
  const $$ = selector => Array.from(document.querySelectorAll(selector));
  const clone = value => JSON.parse(JSON.stringify(value));
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const STAGES = [
    {title:"기존 정리 결과 확인", short:"기존 정리", subtitle:"컴퓨터 비교와 이전 사람의 동일 판정이 반영된 결과를 확인합니다.", action:"정리 결과 확인 → 다음 단계"},
    {title:"동일 이미지 확인", short:"동일 확인", subtitle:"같은 최종 이미지만 골라 대표 이미지 하나를 남깁니다.", action:"동일 이미지 판정 승인 → 다음 단계"},
    {title:"유사 이미지 그룹", short:"그룹 확인", subtitle:"함께 참고할 이미지를 한 그룹으로 묶습니다.", action:"유사 그룹 판정 승인 → 다음 단계"},
    {title:"이미지 승인과 메모", short:"승인 · 메모", subtitle:"남길 이미지를 확인하고, 떠오르는 아이디어만 자유롭게 적으세요.", action:"승인 내용 저장"}
  ];
  const FIELD = {2:"duplicate_reviews",3:"similarity_reviews",4:"image_approvals"};
  let csrf = "", server = null, draft = null;
  let editVersion = 0, savedVersion = 0, saveTimer = null, savePromise = null;
  let pendingSave = null, pendingTransition = null, writeFailure = null, conflict = false, transitioning = false;
  let recoveryDraft = null;
  const dialogReturnFocus = new Map();
  let gallery = null, galleryPage = 0, galleryGroupPage = 0, galleryQuery = "", galleryFilter = "all", gallerySerial = 0;
  const expandedGroups = new Set(), promptCache = new Map();
  let promptSerial = 0, activePrompt = null;
  const pages = {2:0,3:0,4:0}, filters = {2:"all",3:"all",4:"new"}, search = {4:""};
  const pageSize = 12;
  const uid = () => window.crypto?.randomUUID ? crypto.randomUUID() : "admin-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  const activeStage = () => Number(server?.active_stage || 1);
  const dirty = () => editVersion !== savedVersion;
  const finalComplete = () => activeStage()===4&&(server?.completed_stages||[]).includes(4)&&!dirty()&&!pendingTransition&&!writeFailure;
  const spec = () => server?.spec || {};
  const items = () => spec().items || [];
  const itemMap = () => new Map(items().map(item => [item.id,item]));
  const readonlyIds = () => new Set(spec().baseline?.read_only_ids || []);
  const baselineChoices = () => new Map((spec().baseline?.image_approvals || []).map(row => [row.id,row]));
  const itemLabel = id => itemMap().get(id)?.style_id || "이미지";
  const selectedIds = values => [...new Set(values || [])];
  const announce = text => { $("#live-message").textContent = text; };
  function mediaUrl(item) {
    if (!item?.media_url) return "";
    try {
      const url = new URL(item.media_url, location.origin);
      return url.origin === location.origin && url.pathname.startsWith("/media/") ? url.pathname + url.search : "";
    } catch { return ""; }
  }
  function localTime(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "" : new Intl.DateTimeFormat("ko-KR",{hour:"2-digit",minute:"2-digit",second:"2-digit"}).format(date);
  }
  function sourceUrl(value) {
    if(typeof value!=="string"||/[\u0000-\u0020\u007f]/.test(value))return "";
    try{
      const url=new URL(value);
      return ["https:","http:"].includes(url.protocol)&&!url.username&&!url.password?url.href:"";
    }catch{return "";}
  }
  function rightsMarkup(item) {
    const raw=item?.rights_display,valid=raw?.schema_version==="image-rights-notice-1";
    const rights=valid?raw:{},label=(value,fallback)=>typeof value==="string"&&value.trim()?value:fallback;
    const href=sourceUrl(rights.source_url),source=label(rights.source_name,"출처 미확인"),badge=label(rights.badge,"권리 미확인");
    const scope={repository_only:"저장소에만 적용 · 개별 이미지 이용 허가가 아님",image:"개별 이미지에 관한 근거",unknown:"이미지 이용 조건 미확인"}[rights.license_scope]||label(rights.license_scope,"이 이미지에 적용되는 이용 조건은 확인되지 않았습니다.");
    return `<section class="rights-note" aria-label="이미지 출처와 권리"><span class="rights-badge">${escape(badge)}</span><details class="rights-details"><summary>출처 · 권리 확인</summary><dl><div><dt>출처</dt><dd>${escape(source)}${href?` · <a href="${escape(href)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">원문 보기 ↗</a>`:' · 안전한 출처 링크 없음'}</dd></div><div><dt>제작자</dt><dd>${escape(label(rights.creator_name,"제작자 미확인"))}</dd></div><div><dt>라이선스</dt><dd>${escape(label(rights.license_label,"개별 이미지 라이선스 미확인"))}</dd></div><div><dt>적용 범위</dt><dd>${escape(scope)}</dd></div><div><dt>출처 표기</dt><dd>${escape(label(rights.attribution_text,"확인된 출처 표기 정보 없음"))}</dd></div></dl><p class="rights-warning">${escape(label(rights.notice_text,"이미지의 사용·수정·재배포 권한이 확인되지 않았습니다. 이용 전에 권리자와 적용 조건을 별도로 확인하세요."))}</p><p class="rights-disclaimer">이미지 검토 승인은 공개·상업 이용 허가가 아닙니다.</p></details></section>`;
  }
  function handoffNotice() {
    return {
      prepared:"후속 분석 자료 준비 완료 · 실제 분석 실행 대기",
      pending:"승인은 저장되었습니다. 후속 분석 자료는 준비 대기 중입니다.",
      preparation_failed:"승인은 정상 저장되었습니다. 후속 분석 자료 준비만 확인이 필요합니다."
    }[server?.handoff?.status]||"후속 분석 자료의 준비 상태는 아직 확인되지 않았습니다.";
  }
  function promptActions(item) {
    const id=escape(item?.id||""),missing=["missing","unavailable"].includes(item?.prompt_status);
    return `<div class="prompt-actions"><button type="button" class="button small" data-prompt-view="${id}">${missing?"원문 상태 확인":"원문 프롬프트"}</button><button type="button" class="button small" data-prompt-copy="${id}" ${missing?"disabled":""}>복사</button></div>`;
  }
  function approvedLibrary(data) {
    const rows=data?.items||[],library=data?.library;
    if(library?.schema_version!=="image-approved-library-1"||!Array.isArray(library.display_groups)||!Array.isArray(library.ungrouped_ids))throw new Error("그룹형 승인 목록의 형식을 확인할 수 없습니다. 서버를 확인한 뒤 다시 열어주세요.");
    const map=new Map(rows.map(item=>[item.id,item])),groupIds=new Set(),membership=new Map();
    if(map.size!==rows.length)throw new Error("승인 목록에 중복 이미지 ID가 있습니다.");
    for(const group of library.display_groups){
      const members=group.member_ids;
      if(typeof group.group_id!=="string"||groupIds.has(group.group_id)||!Array.isArray(members)||!members.length||new Set(members).size!==members.length||members.some(id=>!map.has(id))||!members.includes(group.representative_id))throw new Error("승인 그룹의 구성원을 확인할 수 없습니다. 이미지를 숨기지 않고 검토를 중단했습니다.");
      groupIds.add(group.group_id);for(const id of members)membership.set(id,(membership.get(id)||0)+1);
    }
    const singles=library.ungrouped_ids;
    if(new Set(singles).size!==singles.length||singles.some(id=>!map.has(id)||membership.has(id))||membership.size+singles.length!==rows.length)throw new Error("그룹과 개별 이미지 수가 전체 승인 수와 맞지 않습니다.");
    return {rows,map,groups:library.display_groups,singles,counts:{approved:rows.length,grouped:membership.size,overlap:[...membership.values()].filter(count=>count>1).length}};
  }
  function matchesGallery(item,query) {
    return !query||[item.style_id,item.title,item.memo_text,item.rights_display?.source_name].some(value=>typeof value==="string"&&value.toLocaleLowerCase().includes(query));
  }
  function galleryPager(kind,count,current,label) {
    return `<div class="pager"><button type="button" class="icon-button" data-gallery-page="-1" data-gallery-kind="${kind}" aria-label="이전 ${label}" ${current<=0?"disabled":""}>‹</button><strong>${count?current+1:0} / ${count}</strong><button type="button" class="icon-button" data-gallery-page="1" data-gallery-kind="${kind}" aria-label="다음 ${label}" ${current>=count-1?"disabled":""}>›</button></div>`;
  }
  function retainedIds() {
    const readonly = readonlyIds(), active = new Set(spec().stage1?.active_ids || []);
    for (const candidate of spec().duplicate_candidates || []) {
      const row = (draft?.duplicate_reviews || []).find(value => value.candidate_id === candidate.id);
      if (!row || row.decision !== "same_image_subset") continue;
      const chosen = selectedIds(row.selected_ids), remaining = candidate.member_ids.filter(id => !chosen.includes(id));
      if (chosen.length < 2 || (remaining.length > 1 && !row.remainder_distinct) || chosen.filter(id => readonly.has(id)).length > 1) continue;
      const keeper = representative(candidate, chosen);
      for (const id of chosen) if (id !== keeper && !readonly.has(id)) active.delete(id);
    }
    return [...active];
  }
  function representative(candidate, chosen) {
    const readonly = readonlyIds(), old = chosen.find(id => readonly.has(id));
    if (old) return old;
    const explicit = (candidate.representative_priority_ids || []).find(id => chosen.includes(id));
    if (explicit) return explicit;
    const map = itemMap();
    return [...chosen].sort((a,b) => (Number(map.get(a)?.priority?.rank_index) || 1e9) - (Number(map.get(b)?.priority?.rank_index) || 1e9) || a.localeCompare(b))[0];
  }
  function duplicateStatus(candidate) {
    const row = draft.duplicate_reviews.find(value => value.candidate_id === candidate.id);
    if (!row || row.decision === "defer") return {done:false,label:"아직 판정하지 않았어요."};
    if (row.decision === "distinct_images") return {done:true,label:"서로 다른 이미지로 유지"};
    const chosen = selectedIds(row.selected_ids), remaining = candidate.member_ids.filter(id => !chosen.includes(id));
    if (chosen.length < 2) return {done:false,label:"동일한 이미지를 2개 이상 선택하세요."};
    if (chosen.filter(id => readonlyIds().has(id)).length > 1) return {done:false,label:"기존 대표 이미지는 서로 합칠 수 없어요."};
    if (remaining.length > 1 && !row.remainder_distinct) return {done:false,label:"선택하지 않은 나머지도 확인해주세요."};
    return {done:true,label:`대표 ${itemLabel(representative(candidate,chosen))} · ${chosen.length-1}개 제외`};
  }
  function similarityStatus(candidate) {
    const row = draft.similarity_reviews.find(value => value.candidate_id === candidate.id), active = new Set(retainedIds()), readonly = readonlyIds();
    const eligible = candidate.member_ids.filter(id => active.has(id)), anchors = candidate.baseline_anchor_ids || [];
    if (eligible.length < 2 || (anchors.length && !eligible.some(id => !readonly.has(id)))) return {done:true,skipped:true,label:"중복 정리 후 추가 그룹 판정 불필요"};
    if (!row || row.decision === "defer") return {done:false,label:"아직 판정하지 않았어요."};
    if (row.decision === "keep_separate") return {done:true,label:"각 이미지로 유지"};
    const chosen = row.selected_ids.filter(id => active.has(id)), chosenSet = new Set(chosen);
    if (chosen.length < 2) return {done:false,label:"함께 묶을 이미지를 2개 이상 선택하세요."};
    const selectedAnchors = anchors.filter(id => chosenSet.has(id));
    if (selectedAnchors.length && selectedAnchors.length !== anchors.length) return {done:false,label:"기존 기준 그룹은 전체 포함하거나 전체 제외해주세요."};
    if (anchors.length && !chosen.some(id => !readonly.has(id))) return {done:false,label:"이 그룹에 추가할 새 이미지를 선택하세요."};
    const negative = (candidate.known_negative_pairs || []).some(pair => chosenSet.has(pair.left_id || pair.left?.id) && chosenSet.has(pair.right_id || pair.right?.id));
    if (negative) return {done:false,label:"이전에 다르다고 판정한 이미지가 함께 선택됐어요."};
    return {done:true,label:`${chosen.length}개 이미지를 한 그룹으로 묶음`};
  }
  function imageChoice(id) {
    if (readonlyIds().has(id)) return baselineChoices().get(id) || {id,approved:false,memo_text:""};
    const row = (draft.image_approvals || []).find(value => value.id === id);
    const seed = (spec().initial_image_approvals || []).find(value => value.id === id);
    return row || seed || {id,approved:true,memo_text:""};
  }
  function setImageChoice(id, change) {
    if (readonlyIds().has(id)) return;
    const row = {...imageChoice(id),...change,id};
    draft.image_approvals ||= [];
    const index = draft.image_approvals.findIndex(value => value.id === id);
    if (index >= 0) draft.image_approvals[index] = {id,approved:row.approved,memo_text:row.memo_text || ""};
    else draft.image_approvals.push({id,approved:row.approved,memo_text:row.memo_text || ""});
  }
  function candidateRows(stage) {
    const rows = stage === 2 ? spec().duplicate_candidates || [] : spec().similarity_candidates || [];
    return filters[stage] === "pending" ? rows.filter(row => !(stage === 2 ? duplicateStatus(row) : similarityStatus(row)).done) : rows;
  }
  function candidateCount(stage) {
    const rows = stage === 2 ? spec().duplicate_candidates || [] : spec().similarity_candidates || [];
    return {total:rows.length,done:rows.filter(row => (stage === 2 ? duplicateStatus(row) : similarityStatus(row)).done).length};
  }
  function focusSnapshot() {
    const node = document.activeElement;
    return node?.dataset?.focusKey ? {key:node.dataset.focusKey,start:node.selectionStart,end:node.selectionEnd} : null;
  }
  function restoreFocus(value) {
    if (!value) return;
    const node = $$("[data-focus-key]").find(element => element.dataset.focusKey === value.key);
    if (!node || node.disabled) return;
    node.focus({preventScroll:true});
    if (typeof value.start === "number" && node.setSelectionRange) try { node.setSelectionRange(value.start,value.end); } catch {}
  }
  function errorMessage(error) {
    const messages = {revision_conflict:"다른 창에서 검토 내용이 바뀌었습니다. 이 창의 미저장 내용은 그대로 보관하고 있습니다.",
      stale_revision:"다른 창에서 먼저 저장했습니다. 이 창의 미저장 내용은 덮어쓰지 않았습니다.",
      invalid_stage:"현재 단계가 서버와 달라졌습니다. 최신 상태를 확인해주세요.",
      csrf_invalid:"서버 연결 정보가 만료되었습니다. 최신 내용을 다시 불러와주세요."};
    return messages[error.code] || error.message || "서버와 통신하지 못했습니다. 입력한 내용은 이 창에 남아 있습니다.";
  }
  function showError(error) {
    writeFailure = error;
    if (error.status === 409) conflict = true;
    $("#connection-banner").hidden = false;
    $("#error-title").textContent = conflict ? "다른 변경 내용과 충돌했습니다." : error.network || error.status >= 500 ? "아직 저장을 확인하지 못했습니다." : "조금 더 확인이 필요합니다.";
    $("#error-text").textContent = errorMessage(error);
    $("#retry-write").hidden = conflict || !(error.network || error.status >= 500);
    $("#reload-state").hidden = !conflict && !error.network && ![401,403].includes(error.status);
    updateChrome();
  }
  function clearError() { writeFailure = null; conflict = false; $("#connection-banner").hidden = true; }
  async function request(path, method="GET", body=null) {
    let response;
    try {
      response = await fetch(path,{method,credentials:"same-origin",headers:{Accept:"application/json",...(body ? {"Content-Type":"application/json","X-Admin-CSRF":csrf} : {})},...(body ? {body:JSON.stringify(body)} : {})});
    } catch {
      const error = new Error("서버 응답을 받지 못했습니다. 같은 요청으로 다시 시도할 수 있습니다."); error.network = true; throw error;
    }
    let payload;
    try { payload = await response.json(); } catch {
      const error = new Error("서버 응답을 확인할 수 없습니다. 입력 내용은 보존됩니다."); error.status = response.status || 500; error.network = true; throw error;
    }
    if (!response.ok) {
      const info = typeof payload.error === "object" ? payload.error : {};
      const error = new Error(info.message || "요청을 처리하지 못했습니다.");
      error.status=response.status;error.code=info.code;error.details=info.details;throw error;
    }
    return payload;
  }
  function acceptState(value, capturedVersion=null) {
    if (!value || !Number.isInteger(value.revision) || !value.decisions || !value.spec) throw new Error("서버 상태 형식을 확인할 수 없습니다.");
    const latest = clone(draft || value.decisions), previousStage = activeStage();
    server = value;
    if (capturedVersion !== null && editVersion > capturedVersion && previousStage === activeStage()) {
      draft = clone(value.decisions);
      if (FIELD[activeStage()]) draft[FIELD[activeStage()]] = clone(latest[FIELD[activeStage()]] || []);
      draft.reviewer = latest.reviewer;
    } else draft = clone(value.decisions);
    if (capturedVersion !== null) savedVersion = capturedVersion;
    else savedVersion = editVersion;
    $("#reviewer").value = draft.reviewer || "";
    renderAll();
  }
  function mutationPayload(extra={}) {
    return {run_id:server.run_id || spec().run_id,expected_revision:server.revision,request_id:uid(),stage:activeStage(),...extra};
  }
  function markEdited() {
    editVersion++;
    if (writeFailure && !conflict && !pendingSave && !pendingTransition) clearError();
    clearTimeout(saveTimer);
    updateChrome();
    if (!conflict && !writeFailure && !transitioning) saveTimer=setTimeout(()=>saveNow().catch(()=>{}),600);
  }
  async function saveNow() {
    clearTimeout(saveTimer);
    if (!server || conflict) return false;
    if (savePromise) { await savePromise; return dirty() && !writeFailure ? saveNow() : !writeFailure; }
    if (!dirty() && !pendingSave) return true;
    const job = pendingSave || {path:"/api/admin/draft",payload:mutationPayload({decisions:clone(draft)}),version:editVersion};
    pendingSave = job;
    savePromise = (async()=>{
      try {
        const value = await request(job.path,"PUT",job.payload);
        clearError();pendingSave=null;acceptState(value,job.version);return true;
      } catch (error) {
        if (!error.network && !(error.status >= 500)) pendingSave=null;
        showError(error);return false;
      } finally { savePromise=null;updateChrome(); }
    })();
    updateChrome();
    const ok = await savePromise;
    if (ok && dirty() && !transitioning) saveTimer=setTimeout(()=>saveNow().catch(()=>{}),200);
    return ok;
  }
  async function flushSaves() {
    if (savePromise && !(await savePromise)) return false;
    while (dirty() || pendingSave) if (!(await saveNow())) return false;
    return !conflict && !writeFailure;
  }
  async function executeTransition(job) {
    pendingTransition=job;
    try {
      const value=await request(job.path,"POST",job.payload);
      pendingTransition=null;clearError();acceptState(value,job.version);
      if (job.path.endsWith("/advance") && job.payload.stage===4) {
        announce("이미지 승인과 메모를 저장했습니다. 승인된 이미지 목록에 반영되었습니다.");
        gallery=null;
        $("#footer-gallery").focus({preventScroll:true});
        await openGallery();
      } else {
        pages[activeStage()]=0;
        announce(`${activeStage()}단계로 이동했습니다.`);
        $("#stage-content").focus({preventScroll:true});window.scrollTo({top:0,behavior:"smooth"});
      }
      return true;
    } catch(error) {
      if (!error.network && !(error.status>=500)) pendingTransition=null;
      showError(error);return false;
    }
  }
  async function transition(target=null) {
    if (transitioning || conflict || !server) return;
    if (!target && !(draft.reviewer || "").trim()) {
      showError(Object.assign(new Error("검토자 이름을 입력한 뒤 승인해주세요."),{status:400}));$("#reviewer").focus();return;
    }
    transitioning=true;updateChrome();$("#stage-content").inert=true;
    try {
      if (pendingTransition) { await executeTransition(pendingTransition);return; }
      if (!(await flushSaves())) return;
      const stage=activeStage(),path=target ? "/api/admin/rewind" : "/api/admin/advance";
      const payload=mutationPayload(target ? {target_stage:target} : {decisions:clone(draft)});
      await executeTransition({path,payload,version:editVersion});
    } finally { transitioning=false;$("#stage-content").inert=false;updateChrome(); }
  }
  function updateChrome() {
    const stage=activeStage(),ready=Boolean(server),done=new Set(server?.completed_stages || []);
    $("#header-stage").textContent=ready ? `${stage}단계 · ${finalComplete()?"검토 완료":STAGES[stage-1].title}` : "";
    $("#reviewer").disabled=!ready||transitioning||conflict;
    $("#stage-content").inert=transitioning||conflict;
    const candidateTotal=(spec().stage1?.active_ids||[]).length;
    $("#batch-title").textContent=ready ? `${candidateTotal.toLocaleString()}개 이미지` : "불러오는 중";
    $("#batch-detail").textContent=ready ? `기존 ${readonlyIds().size}개 + 새 검토 ${Math.max(0,candidateTotal-readonlyIds().size)}개` : "저장된 검토 내용을 확인합니다.";
    $("#stage-nav").innerHTML=STAGES.map((row,index)=>{
      const number=index+1,current=number===stage,complete=done.has(number),enabled=ready&&!transitioning&&!conflict&&number<stage;
      const stateLabel=complete&&(!current||finalComplete())?"승인 완료":current?"현재 진행 중":"앞 단계 승인 후";
      return `<button type="button" class="stage-link ${current?"current":""} ${complete?"complete":""}" data-go-stage="${number}" aria-label="${number}단계 ${row.short} · ${stateLabel}" ${current?'aria-current="step"':""} ${!enabled?"disabled":""}><span class="step-number">${complete&&(!current||finalComplete())?"✓":number}</span><span><strong>${row.short}</strong><small>${stateLabel}</small></span></button>`;
    }).join("");
    const saveLabel=$("#save-label"),saveDetail=$("#save-detail"),dot=$("#save-dot");
    dot.className="status-dot";
    if(writeFailure){saveLabel.textContent=conflict?"저장 충돌 · 입력 내용 보존":"저장 확인 필요";saveDetail.textContent="미저장 내용은 이 창에 남아 있습니다.";dot.classList.add("error");}
    else if(transitioning){saveLabel.textContent="단계 승인을 처리하고 있어요.";saveDetail.textContent="저장 완료 응답을 기다립니다.";dot.classList.add("pending");}
    else if(savePromise){saveLabel.textContent="변경 내용을 저장하고 있어요.";saveDetail.textContent="이 창에서 계속 검토해도 됩니다.";dot.classList.add("pending");}
    else if(dirty()){saveLabel.textContent="변경 내용 저장 대기";saveDetail.textContent="잠시 후 자동 저장됩니다.";dot.classList.add("pending");}
    else if(ready){saveLabel.textContent="서버에 저장됨";saveDetail.textContent=localTime(server.saved_at)?`마지막 저장 ${localTime(server.saved_at)} · 중간 저장은 승인이 아닙니다.`:"중간 저장은 승인이 아닙니다.";dot.classList.add("saved");}
    $("#previous-stage").disabled=!ready||stage===1||transitioning||conflict;
    $("#save-draft").disabled=!ready||transitioning||conflict||Boolean(savePromise)||(!dirty()&&!pendingSave);
    $("#recovery-banner").hidden=!recoveryDraft;
    if(recoveryDraft){
      const restorable=recoveryDraft.run_id===server?.run_id&&recoveryDraft.stage===stage;
      $("#recovery-detail").textContent=restorable?"서버의 현재 내용을 확인한 뒤, 이 단계의 이전 선택과 메모를 명시적으로 복원할 수 있습니다. 창을 닫으면 임시 보관은 사라집니다.":`${recoveryDraft.stage}단계의 이전 편집입니다. 해당 단계로 돌아오면 복원할 수 있습니다. 창을 닫으면 임시 보관은 사라집니다.`;
      $("#restore-draft").disabled=!restorable||transitioning||conflict||Boolean(savePromise);
    }
    const complete=finalComplete();
    if($("#completion-summary"))$("#completion-summary").hidden=!complete;
    $("#advance-stage").hidden=complete;
    $("#advance-stage").textContent=transitioning?"처리 중…":STAGES[stage-1].action;
    $("#advance-stage").disabled=!ready||transitioning||conflict||complete||Boolean(pendingSave&&writeFailure);
    $("#footer-gallery").disabled=!ready;
    $("#footer-gallery").className=`button ${complete?"primary":"secondary"}`;
    $("#gallery-open").disabled=!ready;
  }
  function heading(stage, count="") {
    const row=STAGES[stage-1];
    return `<div class="stage-heading"><div><div class="step-kicker">STEP ${String(stage).padStart(2,"0")} / 04</div><h1>${row.title}</h1><p>${row.subtitle}</p></div>${count?`<span class="count-pill">${escape(count)}</span>`:""}</div>`;
  }
  function renderStage1(){
    const stage1=spec().stage1||{},archived=stage1.archived||[],active=stage1.active_ids||[];
    const examples=archived.slice(0,8).map(row=>`<div><strong>${escape(itemLabel(row.id))}</strong><span>대표 ${escape(itemLabel(row.representative_id))}에 기록 보관</span></div>`).join("");
    return heading(1,"확인 후 다음 단계")+
      `<div class="stats-grid"><article class="stat-card"><span>정리 후 검토 대상</span><strong>${active.length}</strong><small>이미지별 기존 기록 유지</small></article><article class="stat-card"><span>검토 전 제외</span><strong>${archived.length}</strong><small>컴퓨터 비교 · 이전 사람의 동일 판정</small></article><article class="stat-card"><span>동일 여부 확인</span><strong>${(spec().duplicate_candidates||[]).length}</strong><small>사람이 비교할 이미지 묶음</small></article><article class="stat-card"><span>유사 그룹 후보</span><strong>${(spec().similarity_candidates||[]).length}</strong><small>함께 참고할 이미지 묶음</small></article></div>
      <div class="note"><strong>확인된 완전 중복과 기존 승인 기록을 먼저 반영했습니다.</strong><p>컴퓨터가 확인한 완전 중복과 이전에 사람이 동일하다고 승인한 기록을 반영했습니다. 원본은 삭제하지 않고 대표 이미지에 연결해 보관합니다. 추가 동일 판정과 유사 그룹 확인은 다음 단계에서 진행합니다.</p></div>
      <section class="panel"><div class="panel-head"><h2>이번 검토의 기준</h2><span class="badge">원본 보존</span></div><ol class="timeline"><li><span class="check">✓</span><div><h3>완전 중복은 대표 이미지로 정리</h3><p>별칭, 출처, 프롬프트는 대표 이미지와 연결해 보관합니다.</p></div></li><li><span class="check">✓</span><div><h3>기존 승인 기록은 변경하지 않음</h3><p>기존 ${readonlyIds().size}개 이미지는 기준 이미지로만 사용합니다. 기존 미승인 기록도 그대로 유지합니다.</p></div></li><li><span class="check">→</span><div><h3>한 단계씩 확인하고 승인</h3><p>동일 이미지 확인 → 유사 그룹 확인 → 이미지 승인과 선택 메모 순서로 진행합니다.</p></div></li></ol>
      ${examples?`<details class="evidence"><summary>검토 전에 정리된 기록 예시</summary><div class="mini-list">${examples}</div></details>`:""}</section>`;
  }
  function pager(stage,count,current,label="묶음"){
    return `<div class="pager"><button type="button" class="icon-button" data-page-stage="${stage}" data-page-delta="-1" aria-label="이전 ${label}" ${current<=0?"disabled":""}>‹</button><strong>${count?current+1:0} / ${count}</strong><button type="button" class="icon-button" data-page-stage="${stage}" data-page-delta="1" aria-label="다음 ${label}" ${current>=count-1?"disabled":""}>›</button></div>`;
  }
  function photoCard(item,body="",options={}){
    const url=mediaUrl(item),title=escape(item?.style_id || "이미지"),id=escape(item?.id || "");
    return `<article class="image-card ${options.selected?"selected":""} ${options.locked?"locked":""} ${options.excluded?"excluded":""}" data-card-id="${id}"><button class="image-zoom" type="button" data-zoom-id="${id}" aria-label="${title} 이미지 크게 보기">${url?`<img src="${escape(url)}" alt="${title}" loading="lazy">`:`<span class="empty">미리보기를 준비하지 못했습니다.</span>`}<span class="zoom-hint">크게 보기 ↗</span></button><div class="card-body"><div class="card-title"><strong>${title}</strong>${options.locked?'<span class="badge anchor">기존 기준 · 읽기 전용</span>':options.badge?`<span class="badge">${escape(options.badge)}</span>`:""}</div>${body}${promptActions(item)}${rightsMarkup(item)}</div></article>`;
  }
  function renderCandidateStage(stage){
    const rows=candidateRows(stage),count=candidateCount(stage),all=stage===2?spec().duplicate_candidates||[]:spec().similarity_candidates||[];
    pages[stage]=Math.max(0,Math.min(pages[stage],rows.length-1));
    const candidate=rows[pages[stage]],filter=`<label class="sr-only" for="candidate-filter">표시할 묶음</label><select id="candidate-filter" data-candidate-filter="${stage}" class="filter-select" data-focus-key="candidate-filter"><option value="all" ${filters[stage]==="all"?"selected":""}>모든 묶음</option><option value="pending" ${filters[stage]==="pending"?"selected":""}>판정이 남은 묶음</option></select>`;
    const top=heading(stage,`${count.done} / ${count.total}개 판정`)+`<div class="review-toolbar">${filter}${pager(stage,rows.length,pages[stage])}</div>`;
    if(!candidate)return top+`<div class="empty"><h2>${all.length?"모든 묶음을 확인했어요.":"이 단계에서 확인할 묶음이 없습니다."}</h2><p>아래 승인 버튼을 눌러 다음 단계로 이동하세요.</p></div>`;
    const row=draft[FIELD[stage]].find(value=>value.candidate_id===candidate.id),readonly=readonlyIds(),active=new Set(retainedIds()),status=stage===2?duplicateStatus(candidate):similarityStatus(candidate);
    const names=candidate.member_ids.map(itemLabel),titles=names.slice(0,4).join(" · ")+(names.length>4?` 외 ${names.length-4}개`:"");
    const map=itemMap(),cards=candidate.member_ids.map(id=>{
      const locked=readonly.has(id),inactive=stage===3&&!active.has(id),checked=row.selected_ids.includes(id);
      const body=`<label class="card-check"><input type="checkbox" data-candidate-member="${escape(candidate.id)}" data-member-stage="${stage}" value="${escape(id)}" data-focus-key="member-${stage}-${escape(id)}" ${checked?"checked":""} ${locked||inactive||status.skipped?"disabled":""}><span>${locked?"기존 기준 이미지":inactive?"앞 단계에서 중복으로 제외":stage===2?"같은 최종 이미지":"이 그룹에 포함"}${locked?"<small>새 이미지와 비교하는 기준입니다.</small>":""}</span></label>`;
      return photoCard(map.get(id),body,{selected:checked,locked,excluded:inactive,badge:stage===2&&id===candidate.suggested_representative_id?"제안 대표":""});
    }).join("");
    const choices=stage===2?[["same_image_subset","선택한 이미지는 동일"],["distinct_images","서로 다른 이미지"],["defer","판단 보류"]]:[["approve_selected","선택한 이미지끼리 한 그룹"],["keep_separate","그룹으로 묶지 않고 각각 유지"],["defer","판단 보류"]];
    const remainder=candidate.member_ids.filter(id=>!row.selected_ids.includes(id));
    const decisionOptions=choices.map(([value,label])=>`<label class="decision-option"><input type="radio" name="decision" value="${value}" data-candidate-decision="${escape(candidate.id)}" data-decision-stage="${stage}" data-focus-key="decision-${stage}-${value}" ${row.decision===value?"checked":""} ${status.skipped?"disabled":""}>${label}</label>`).join("");
    const evidence={...(candidate.evidence?{comparison:candidate.evidence}:{}),...(candidate.known_positive_pairs?{prior_positive_pairs:candidate.known_positive_pairs}:{}),...(candidate.known_negative_pairs?{prior_negative_pairs:candidate.known_negative_pairs}:{})};
    return top+`<section class="panel"><div class="candidate-heading"><div><h2>${stage===2?"동일 이미지 후보":"함께 참고할 이미지"} ${all.indexOf(candidate)+1}</h2><p>${escape(titles)}</p></div><span class="candidate-status ${status.done?"":"pending"}">${status.done?"판정 완료":"확인 필요"}</span></div>
      <div class="review-toolbar"><div class="filters"><button type="button" class="button small" data-select-members="all" data-select-stage="${stage}" data-select-candidate="${escape(candidate.id)}" ${status.skipped?"disabled":""}>전체 선택</button><button type="button" class="button small" data-select-members="clear" data-select-stage="${stage}" data-select-candidate="${escape(candidate.id)}" ${status.skipped?"disabled":""}>선택 해제</button></div><span class="gallery-count">${candidate.member_ids.length}개 함께 비교</span></div>
      ${stage===3&&(candidate.baseline_anchor_ids||[]).length?`<label class="remainder"><input type="checkbox" data-anchor-toggle="${escape(candidate.id)}" data-focus-key="anchor-toggle" ${(candidate.baseline_anchor_ids||[]).every(id=>row.selected_ids.includes(id))?"checked":""} ${status.skipped?"disabled":""}>기존 기준 그룹 전체에 함께 묶기 · 해제하면 새 이미지끼리만 그룹을 만들 수 있습니다.</label>`:""}
      <div class="image-grid">${cards}</div><div class="decision-box"><fieldset><legend>${stage===2?"선택한 이미지의 동일 여부":"선택한 이미지의 그룹 관계"}</legend><div class="decision-options">${decisionOptions}</div></fieldset>
      ${stage===2&&row.decision==="same_image_subset"&&remainder.length>1?`<label class="remainder"><input type="checkbox" data-remainder="${escape(candidate.id)}" data-focus-key="remainder" ${row.remainder_distinct?"checked":""}>선택하지 않은 나머지 ${remainder.length}개 이미지는 서로 다른 이미지임을 확인했습니다.</label>`:""}
      <p class="decision-help">${escape(status.label)}</p></div><details class="evidence"><summary>컴퓨터 비교 근거 보기</summary><pre>${escape(JSON.stringify(evidence,null,2))}</pre></details></section>`;
  }
  function stage4Ids(){
    const readonly=readonlyIds(),query=search[4].trim().toLocaleLowerCase();
    return retainedIds().filter(id=>{
      if(filters[4]==="new"&&readonly.has(id))return false;
      if(filters[4]==="approved"&&!imageChoice(id).approved)return false;
      if(filters[4]==="excluded"&&imageChoice(id).approved)return false;
      return !query||itemLabel(id).toLocaleLowerCase().includes(query)||(imageChoice(id).memo_text||"").toLocaleLowerCase().includes(query);
    });
  }
  function renderStage4(){
    const ids=stage4Ids(),count=Math.ceil(ids.length/pageSize),readonly=readonlyIds(),all=retainedIds(),approved=all.filter(id=>imageChoice(id).approved).length;
    pages[4]=Math.max(0,Math.min(pages[4],count-1));
    const cards=ids.slice(pages[4]*pageSize,(pages[4]+1)*pageSize).map(id=>{
      const row=imageChoice(id),locked=readonly.has(id),bytes=new TextEncoder().encode(row.memo_text||"").length;
      const body=locked?`<p class="muted">${row.approved?"기존 승인":"기존 미승인"} · 변경하지 않습니다.</p>${row.memo_text?`<p class="decision-help">${escape(row.memo_text)}</p>`:""}`:
        `<label class="card-check"><input type="checkbox" data-image-approval="${escape(id)}" data-focus-key="approval-${escape(id)}" ${row.approved?"checked":""}><span>이 이미지 승인<small>제외하려면 체크를 해제하세요.</small></span></label><label class="memo-label">자유 메모 <span class="muted">(선택)</span><textarea class="memo-input" data-image-memo="${escape(id)}" data-focus-key="memo-${escape(id)}" placeholder="어디에 활용하면 좋을까요? 떠오르는 영감을 남겨보세요.">${escape(row.memo_text||"")}</textarea></label><span class="memo-counter ${bytes>8000?"over":""}" data-memo-counter="${escape(id)}">${bytes.toLocaleString()} / 8,000 bytes</span>`;
      return photoCard(itemMap().get(id),body,{selected:row.approved,locked,excluded:!row.approved});
    }).join("");
    const committedAt=server.last_commit?.committed_at||server.last_commit?.created_at||server.saved_at;
    return heading(4,`승인 ${approved}개 / 유지 ${all.length}개`)+
      `<section id="completion-summary" class="completion-summary" ${finalComplete()?"":"hidden"} aria-label="검토 완료"><span class="completion-check" aria-hidden="true">✓</span><div><h2>이미지 검토를 완료했습니다.</h2><p>승인 ${Number(server.summary?.confirmed_front_count||0).toLocaleString()}개가 비공개 승인 목록에 반영되었습니다.${localTime(committedAt)?` · ${escape(localTime(committedAt))} 저장`:""}</p><p>아래 <strong>승인된 이미지 보기</strong>로 결과를 확인하세요. 수정한 내용은 다시 승인해야 반영됩니다.</p><small id="handoff-notice">${escape(handoffNotice())}</small><small>LLM 분석·텍스트 임베딩은 자동 실행되지 않습니다. 검토 승인은 이미지 이용 허가와 별개입니다.</small></div></section>`+
      `<div class="note"><strong>남은 새 이미지는 기본 승인입니다.</strong><p>그룹에 속한 이미지도 하나씩 제외할 수 있습니다. 메모는 모든 이미지에 작성할 필요가 없습니다. 아래 <b>승인 내용 저장</b>을 누르면 승인 목록에 반영됩니다.</p></div>
      <div class="review-toolbar panel"><div class="filters"><label class="sr-only" for="image-search">이미지 또는 메모 검색</label><input id="image-search" class="search-field" type="search" value="${escape(search[4])}" placeholder="이미지 번호 또는 메모 검색" data-focus-key="image-search"><label class="sr-only" for="image-filter">표시할 이미지</label><select id="image-filter" class="filter-select" data-focus-key="image-filter">${[["new","새 이미지"],["all","기존 이미지 포함 전체"],["approved","승인된 이미지"],["excluded","제외한 이미지"]].map(([value,label])=>`<option value="${value}" ${filters[4]===value?"selected":""}>${label}</option>`).join("")}</select></div>${pager(4,count,pages[4],"이미지 페이지")}</div>
      ${cards?`<div class="image-grid">${cards}</div>`:'<div class="empty"><h2>표시할 이미지가 없습니다.</h2><p>검색어나 표시 조건을 바꿔보세요.</p></div>'}`;
  }
  function renderAll(){
    if(!server)return;
    const focus=focusSnapshot(),stage=activeStage();
    $("#stage-content").setAttribute("aria-busy","false");
    $("#stage-content").dataset.stage=String(stage);
    $("#stage-content").innerHTML=stage===1?renderStage1():stage===4?renderStage4():renderCandidateStage(stage);
    updateChrome();restoreFocus(focus);
  }
  function openZoom(id, source=null){
    const map=source||itemMap(),item=map.get(id);if(!item)return;
    dialogReturnFocus.set("zoom-dialog",document.activeElement);
    $("#zoom-title").textContent=item.style_id||"이미지 크게 보기";
    $("#zoom-image").src=mediaUrl(item);$("#zoom-image").alt=item.style_id||"확대 이미지";
    $("#zoom-caption").textContent=readonlyIds().has(id)?"기존 기준 이미지 · 읽기 전용":"이미지의 세부 형태와 구성을 비교하세요.";
    $("#zoom-rights").innerHTML=rightsMarkup(item);
    $("#zoom-prompt-actions").innerHTML=promptActions(item);
    $("#zoom-dialog").showModal();$("#zoom-dialog .icon-button").focus();
  }
  function closeDialog(id){
    const dialog=document.getElementById(id);if(dialog.open)dialog.close();
    const target=dialogReturnFocus.get(id);
    if(target?.isConnected)target.focus();
  }
  function renderGallery(){
    const focus=focusSnapshot(),model=approvedLibrary(gallery),query=galleryQuery.trim().toLocaleLowerCase();
    const groups=model.groups.filter(group=>group.member_ids.some(id=>matchesGallery(model.map.get(id),query))),singles=model.singles.filter(id=>matchesGallery(model.map.get(id),query));
    const groupPages=Math.ceil(groups.length/6),singlePages=Math.ceil(singles.length/pageSize);
    galleryGroupPage=Math.max(0,Math.min(galleryGroupPage,groupPages-1));galleryPage=Math.max(0,Math.min(galleryPage,singlePages-1));
    const card=(id,badge="승인 완료")=>{const item=model.map.get(id);return photoCard(item,item.memo_text?`<p class="personal-memo"><strong>개인 메모</strong>${escape(item.memo_text)}</p>`:"",{badge});};
    const groupCards=groups.slice(galleryGroupPage*6,(galleryGroupPage+1)*6).map(group=>{
      const rest=group.member_ids.filter(id=>id!==group.representative_id),number=model.groups.indexOf(group)+1,names=group.member_ids.map(id=>model.map.get(id).style_id||"이미지").join(" · ");
      return `<article class="library-group" data-library-group="${escape(group.group_id)}"><header><div><span class="eyebrow">사람이 승인한 유사 그룹 ${number}</span><h3>${escape(model.map.get(group.representative_id).style_id||"대표 이미지")} 그룹</h3></div><span class="count-pill">승인 ${group.member_ids.length}개</span></header><p class="group-member-names">${escape(names)}</p>${group.hidden_member_count?`<p class="decision-help">이 그룹의 미승인 ${Number(group.hidden_member_count)}개는 승인 목록에서 제외되어 있습니다.</p>`:""}<div class="group-representative">${card(group.representative_id,"그룹 대표")}</div>${rest.length?`<details class="group-members" data-group-members="${escape(group.group_id)}" ${expandedGroups.has(group.group_id)?"open":""}><summary>함께 묶인 나머지 ${rest.length}개 펼쳐보기</summary><div class="image-grid">${rest.map(id=>card(id,"그룹 구성원")).join("")}</div></details>`:'<p class="decision-help">현재 승인된 구성원은 대표 이미지 1개입니다.</p>'}</article>`;
    }).join("");
    const singleCards=singles.slice(galleryPage*pageSize,(galleryPage+1)*pageSize).map(id=>card(id)).join("");
    const groupSection=galleryFilter!=="singles"?`<section class="library-section" aria-label="승인된 유사 그룹"><div class="review-toolbar"><div><h3>유사 그룹 <span class="muted">${groups.length}개</span></h3><p class="decision-help">검색에 한 이미지가 맞아도 그룹 전체를 함께 보여줍니다. 페이지도 그룹 단위로 나눕니다.</p></div>${galleryPager("groups",groupPages,galleryGroupPage,"유사 그룹 페이지")}</div>${groupCards?`<div class="library-groups">${groupCards}</div>`:'<div class="empty"><p>이 조건에 맞는 승인 그룹이 없습니다.</p></div>'}</section>`:"";
    const singleSection=galleryFilter!=="groups"?`<section class="library-section" aria-label="그룹 없는 승인 이미지"><div class="review-toolbar"><h3>개별 이미지 <span class="muted">${singles.length}개</span></h3>${galleryPager("singles",singlePages,galleryPage,"개별 이미지 페이지")}</div>${singleCards?`<div class="image-grid">${singleCards}</div>`:'<div class="empty"><p>이 조건에 맞는 개별 이미지가 없습니다.</p></div>'}</section>`:"";
    $("#gallery-content").innerHTML=`<div class="library-overview"><strong>승인 이미지 ${model.counts.approved}개</strong><span>유사 그룹 ${model.groups.length}개 · 그룹 안 ${model.counts.grouped}개 · 개별 ${model.singles.length}개</span>${model.counts.overlap?`<span>여러 그룹에 속한 이미지 ${model.counts.overlap}개는 각 그룹에서 다시 표시됩니다. 전체 수는 중복 없이 셉니다.</span>`:""}${localTime(gallery?.committed_at)?`<small>${escape(localTime(gallery.committed_at))} 승인 결과</small>`:""}</div><div class="review-toolbar"><div class="filters"><label class="sr-only" for="gallery-search">승인 이미지 번호, 메모, 출처 검색</label><input id="gallery-search" class="search-field" type="search" value="${escape(galleryQuery)}" placeholder="이미지 번호 · 메모 · 출처 검색" data-focus-key="gallery-search"><label class="sr-only" for="gallery-filter">라이브러리 표시 방식</label><select id="gallery-filter" class="filter-select" data-focus-key="gallery-filter">${[["all","그룹과 개별 이미지"],["groups","유사 그룹만"],["singles","개별 이미지만"]].map(([value,label])=>`<option value="${value}" ${galleryFilter===value?"selected":""}>${label}</option>`).join("")}</select></div></div>${model.rows.length?groupSection+singleSection:'<div class="empty"><h2>아직 승인된 이미지가 없습니다.</h2><p>4단계 승인을 완료하면 여기에 표시됩니다.</p></div>'}`;
    restoreFocus(focus);
  }
  async function openGallery(){
    const serial=++gallerySerial;
    dialogReturnFocus.set("gallery-dialog",document.activeElement);$("#gallery-content").innerHTML='<div class="loading"><span class="spinner"></span><p>승인 목록을 불러옵니다.</p></div>';
    $("#gallery-dialog").showModal();$("#gallery-dialog .icon-button").focus();
    try{const value=await request("/api/admin/gallery");if(serial!==gallerySerial||!$("#gallery-dialog").open)return;gallery=value;galleryPage=0;galleryGroupPage=0;galleryQuery="";galleryFilter="all";renderGallery();}
    catch(error){if(serial===gallerySerial&&$("#gallery-dialog").open)$("#gallery-content").innerHTML=`<div class="empty"><h2>승인 목록을 불러오지 못했습니다.</h2><p>${escape(errorMessage(error))}</p></div>`;}
  }
  function promptStatus(text,failed=false){
    $("#prompt-status").textContent=text;$("#prompt-status").classList.toggle("error",failed);
  }
  function selectPrompt(){
    if(!activePrompt?.available)return;
    $("#prompt-text").focus();$("#prompt-text").select();
    promptStatus("원문 전체를 선택했습니다. Ctrl+C (Mac: ⌘C)로 복사하세요. 자동 복사 완료를 의미하지 않습니다.");
  }
  async function copyPrompt(){
    if(!activePrompt?.available||!$("#prompt-dialog").open)return;
    const serial=promptSerial,text=activePrompt.text;$("#prompt-copy").disabled=true;
    promptStatus("원문 전체를 복사하고 있습니다…");
    try{
      if(!navigator.clipboard?.writeText)throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(text);
      if(serial===promptSerial&&$("#prompt-dialog").open)promptStatus("원문 프롬프트 전체를 복사했습니다.");
    }catch{
      if(serial===promptSerial&&$("#prompt-dialog").open){
        $("#prompt-text").focus();$("#prompt-text").select();
        promptStatus("자동 복사가 허용되지 않았습니다. 선택된 원문을 Ctrl+C (Mac: ⌘C)로 직접 복사하세요.",true);
      }
    }finally{if(serial===promptSerial)$("#prompt-copy").disabled=!activePrompt?.available;}
  }
  async function openPrompt(id,copyAfterLoad=false){
    const item=(gallery?.items||[]).find(row=>row.id===id)||itemMap().get(id);if(!item)return;
    if(!$("#prompt-dialog").open)dialogReturnFocus.set("prompt-dialog",document.activeElement);
    const serial=++promptSerial;activePrompt={id,available:false,text:""};
    $("#prompt-title").textContent=`${item.style_id||"이미지"} · 원본 프롬프트`;
    $("#prompt-source").textContent="임베딩용 축약문이 아닌, 고정된 출처의 원문을 확인합니다.";
    $("#prompt-text").value="";$("#prompt-copy").disabled=true;$("#prompt-select").disabled=true;$("#prompt-retry").hidden=true;
    $("#prompt-rights").innerHTML=rightsMarkup(item);promptStatus("원본 프롬프트를 불러오고 있습니다…");
    if(!$("#prompt-dialog").open)$("#prompt-dialog").showModal();
    $("#prompt-dialog .icon-button").focus();
    try{
      const key=JSON.stringify([spec().spec_sha256,id]),payload=promptCache.get(key)||await request("/api/admin/prompt/"+encodeURIComponent(id));
      if(payload.schema_version!=="image-original-prompt-1"||payload.id!==id)throw new Error("원문 프롬프트의 이미지 근거가 현재 화면과 일치하지 않습니다.");
      if(serial!==promptSerial||!$("#prompt-dialog").open)return;
      if(payload.status!=="available"){
        promptStatus(payload.status==="missing"?"이 이미지에 저장된 원본 프롬프트가 없습니다.":"고정된 원문 근거를 읽을 수 없습니다. 서버 상태를 확인해주세요.",true);
        $("#prompt-retry").hidden=payload.status==="missing";return;
      }
      if(payload.source_binding?.run_id!==server.run_id||payload.source_binding?.spec_sha256!==spec().spec_sha256||payload.source_binding?.prompt_field!=="prompt")throw new Error("원문 프롬프트의 검토 근거가 현재 화면과 일치하지 않습니다.");
      if(typeof payload.full_prompt!=="string"||!/^[a-f0-9]{64}$/.test(payload.prompt_sha256||""))throw new Error("원본 프롬프트의 내용 또는 해시를 확인할 수 없습니다.");
      if(crypto.subtle){
        const digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(payload.full_prompt));
        if([...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,"0")).join("")!==payload.prompt_sha256)throw new Error("원본 프롬프트의 내용 해시가 일치하지 않습니다.");
      }
      if(serial!==promptSerial||!$("#prompt-dialog").open)return;
      promptCache.set(key,payload);activePrompt={id,available:true,text:payload.full_prompt};
      $("#prompt-text").value=payload.full_prompt;$("#prompt-copy").disabled=false;$("#prompt-select").disabled=false;
      $("#prompt-source").textContent=`원문 전체 · UTF-8 ${new TextEncoder().encode(payload.full_prompt).length.toLocaleString()} bytes · JSON과 공백을 다시 정리하지 않습니다.`;
      promptStatus("원문을 불러왔습니다. 복사 버튼을 누르면 표시용 텍스트가 아니라 원본 문자열 전체를 복사합니다.");
      if(copyAfterLoad)await copyPrompt();
    }catch(error){
      if(serial!==promptSerial||!$("#prompt-dialog").open)return;
      promptStatus(errorMessage(error),true);$("#prompt-retry").hidden=false;
    }
  }
  async function reloadServer(){
    if(dirty()||conflict||pendingSave||pendingTransition){
      if(!window.confirm("서버의 최신 내용을 불러올까요? 이 창의 미저장 내용은 복구용으로 메모리에 보관되며 서버에 덮어쓰지 않습니다."))return;
      recoveryDraft={run_id:server?.run_id,stage:activeStage(),revision:server?.revision,decisions:clone(draft),saved_at:new Date().toISOString()};
    }
    clearTimeout(saveTimer);
    try{const session=await request("/api/admin/session");csrf=session.csrf_token;const value=await request("/api/admin/state");
      pendingSave=null;pendingTransition=null;editVersion=0;savedVersion=0;clearError();acceptState(value);
      announce(recoveryDraft?"최신 서버 내용을 불러왔습니다. 이전 미저장 내용은 이 창의 복구용 메모리에 보관했습니다.":"최신 서버 내용을 불러왔습니다.");
    }catch(error){showError(error);}
  }
  document.addEventListener("click",event=>{
    const button=event.target.closest("button");if(!button)return;
    if(button.dataset.closeDialog){closeDialog(button.dataset.closeDialog);return;}
    if(button.dataset.promptView){openPrompt(button.dataset.promptView);return;}
    if(button.dataset.promptCopy){openPrompt(button.dataset.promptCopy,true);return;}
    if(button.dataset.zoomId){const map=$("#gallery-dialog").open?new Map((gallery?.items||[]).map(item=>[item.id,item])):itemMap();openZoom(button.dataset.zoomId,map);return;}
    if(button.dataset.galleryPage){if(button.dataset.galleryKind==="groups")galleryGroupPage+=Number(button.dataset.galleryPage);else galleryPage+=Number(button.dataset.galleryPage);renderGallery();return;}
    if(button.dataset.goStage){transition(Number(button.dataset.goStage));return;}
    if(button.dataset.pageStage){const stage=Number(button.dataset.pageStage);pages[stage]+=Number(button.dataset.pageDelta);renderAll();return;}
    if(button.dataset.selectMembers&&!transitioning&&!conflict){
      const stage=Number(button.dataset.selectStage),candidate=(stage===2?spec().duplicate_candidates:spec().similarity_candidates).find(row=>row.id===button.dataset.selectCandidate);
      const row=draft[FIELD[stage]].find(value=>value.candidate_id===candidate.id),active=new Set(retainedIds());
      row.selected_ids=button.dataset.selectMembers==="all"?selectedIds([...row.selected_ids,...candidate.member_ids.filter(id=>stage===2||active.has(id)),...(candidate.baseline_anchor_ids||[])]):(candidate.baseline_anchor_ids||[]).filter(id=>row.selected_ids.includes(id));
      markEdited();renderAll();
    }
  });
  document.addEventListener("change",event=>{
    const node=event.target;
    if(node.id==="gallery-filter"){galleryFilter=node.value;galleryPage=0;galleryGroupPage=0;renderGallery();return;}
    if(transitioning||conflict||!draft)return;
    if(node.dataset.candidateFilter){const stage=Number(node.dataset.candidateFilter);filters[stage]=node.value;pages[stage]=0;renderAll();return;}
    if(node.id==="image-filter"){filters[4]=node.value;pages[4]=0;renderAll();return;}
    if(node.dataset.anchorToggle){
      const candidate=(spec().similarity_candidates||[]).find(row=>row.id===node.dataset.anchorToggle),row=draft.similarity_reviews.find(value=>value.candidate_id===candidate.id),anchors=candidate.baseline_anchor_ids||[];
      row.selected_ids=node.checked?selectedIds([...row.selected_ids,...anchors]):row.selected_ids.filter(id=>!anchors.includes(id));
    }else if(node.dataset.candidateMember){
      if(readonlyIds().has(node.value))return;
      const stage=Number(node.dataset.memberStage),row=draft[FIELD[stage]].find(value=>value.candidate_id===node.dataset.candidateMember);
      row.selected_ids=node.checked?selectedIds([...row.selected_ids,node.value]):row.selected_ids.filter(id=>id!==node.value);
    }else if(node.dataset.candidateDecision){const stage=Number(node.dataset.decisionStage);draft[FIELD[stage]].find(value=>value.candidate_id===node.dataset.candidateDecision).decision=node.value;}
    else if(node.dataset.remainder)draft.duplicate_reviews.find(value=>value.candidate_id===node.dataset.remainder).remainder_distinct=node.checked;
    else if(node.dataset.imageApproval)setImageChoice(node.dataset.imageApproval,{approved:node.checked});
    else return;
    markEdited();renderAll();
  });
  document.addEventListener("input",event=>{
    const node=event.target;
    if(node.id==="gallery-search"){galleryQuery=node.value;galleryPage=0;galleryGroupPage=0;renderGallery();return;}
    if(transitioning||conflict||!draft)return;
    if(node.id==="image-search"){search[4]=node.value;pages[4]=0;renderAll();return;}
    if(node.id==="reviewer")draft.reviewer=node.value;
    else if(node.dataset.imageMemo){
      const id=node.dataset.imageMemo;setImageChoice(id,{memo_text:node.value});
      const counter=$$("[data-memo-counter]").find(element=>element.dataset.memoCounter===id),size=new TextEncoder().encode(node.value).length;
      if(counter){counter.textContent=`${size.toLocaleString()} / 8,000 bytes`;counter.classList.toggle("over",size>8000);}
    }else return;
    markEdited();
  });
  $("#previous-stage").addEventListener("click",()=>transition(activeStage()-1));
  $("#advance-stage").addEventListener("click",()=>transition());
  $("#save-draft").addEventListener("click",()=>saveNow());
  $("#restore-draft").addEventListener("click",()=>{
    if(!recoveryDraft||conflict||transitioning||recoveryDraft.run_id!==server?.run_id||recoveryDraft.stage!==activeStage())return;
    if(!window.confirm("현재 단계의 서버 초안을 이 창에 보관한 이전 선택과 메모로 바꿀까요? 다른 단계는 바꾸지 않으며 다시 검토 후 승인해야 합니다."))return;
    if(FIELD[activeStage()])draft[FIELD[activeStage()]]=clone(recoveryDraft.decisions[FIELD[activeStage()]]||[]);
    draft.reviewer=recoveryDraft.decisions.reviewer||"";recoveryDraft=null;
    $("#reviewer").value=draft.reviewer;markEdited();renderAll();announce("이전 편집을 복원했습니다. 현재 단계 내용을 다시 확인해주세요.");
  });
  $("#gallery-open").addEventListener("click",openGallery);
  $("#footer-gallery").addEventListener("click",openGallery);
  $("#prompt-copy").addEventListener("click",copyPrompt);
  $("#prompt-select").addEventListener("click",selectPrompt);
  $("#prompt-retry").addEventListener("click",()=>{if(activePrompt)openPrompt(activePrompt.id);});
  document.addEventListener("toggle",event=>{if(event.target.dataset?.groupMembers){const id=event.target.dataset.groupMembers;if(event.target.open)expandedGroups.add(id);else expandedGroups.delete(id);}},true);
  $("#reload-state").addEventListener("click",reloadServer);
  $("#retry-write").addEventListener("click",async()=>{
    if(conflict||transitioning)return;
    writeFailure=null;$("#connection-banner").hidden=true;
    if(pendingTransition)await transition();
    else await saveNow();
    updateChrome();
  });
  for(const dialog of $$("dialog")){
    dialog.addEventListener("close",()=>{if(dialog.id==="prompt-dialog"){promptSerial++;activePrompt=null;}if(dialog.id==="gallery-dialog")gallerySerial++;});
    dialog.addEventListener("cancel",()=>{const target=dialogReturnFocus.get(dialog.id);if(target?.isConnected)setTimeout(()=>target.focus(),0);});
    dialog.addEventListener("click",event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeDialog(dialog.id);}});
  }
  window.addEventListener("beforeunload",event=>{if(dirty()||pendingSave||pendingTransition||recoveryDraft){event.preventDefault();event.returnValue="";}});
  (async()=>{
    try{const session=await request("/api/admin/session");if(typeof session.csrf_token!=="string")throw new Error("서버 연결 정보가 올바르지 않습니다.");csrf=session.csrf_token;acceptState(await request("/api/admin/state"));}
    catch(error){showError(error);$("#reload-state").hidden=false;$("#stage-content").setAttribute("aria-busy","false");$("#stage-content").innerHTML='<div class="empty"><h1>검토 서버에 연결하지 못했습니다.</h1><p>로컬 서버가 실행 중인지 확인하고 최신 내용을 다시 불러와주세요.</p></div>';}
  })();
})();
