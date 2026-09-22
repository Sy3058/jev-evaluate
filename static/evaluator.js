let config;
let items = [];
let editingCase = null;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const statusNames = {rated:'판정', unverifiable:'판정 불가', not_applicable:'해당 없음'};
const requirementNames = {MET:'충족', PARTIAL:'부분 충족', UNMET:'미충족', UNKNOWN:'판정 불가', SAFE_REFUSAL:'적절한 안전 거절'};
function notify(message) { $('#toast').textContent=message; $('#toast').classList.add('show'); setTimeout(()=>$('#toast').classList.remove('show'),5000); }
async function api(url, body) {
  const response = await fetch(url, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json(); if (!response.ok) throw new Error(data.error || '요청 실패'); return data;
}
function specFields(prefix) {
  return `<div class="spec-fields">
    <p class="hint">사용자 질문을 모든 모델의 공통 평가 기준으로 사용합니다. 요구사항을 다시 입력할 필요가 없습니다.</p>
    <details><summary>추가 평가 기준 (선택)</summary>
    <label>보충 기준 <span>질문만으로 충분하면 비워 두세요.</span><textarea id="${prefix}-requirements" rows="3" placeholder="평가 시 특별히 확인할 사항이 있을 때만 입력하세요."></textarea></label>
    </details>
    <label>요청한 출력 형식<select id="${prefix}-format"><option value="text">일반 텍스트 / Markdown</option><option value="html">HTML</option><option value="json">JSON</option><option value="python">Python 코드</option></select></label>
    <label>기준 답안 또는 기대 결과 <span>선택 · 사람이 확인한 기준</span><textarea id="${prefix}-expected" rows="3"></textarea></label>
    <details><summary>출처 및 자동 검사 등록</summary>
    <label>1차 출처 목록 (JSON)<textarea id="${prefix}-references" rows="4" placeholder='[{"title":"공식 자료", "url":"https://...", "text":"검증할 원문", "checkedAt":"2026-09-22"}]'>[]</textarea></label>
    <p class="hint">입력한 발췌문만 사용합니다. URL 접속·출처 진위 확인은 자동 수행하지 않습니다.</p>
    <label>검사 조건 (JSON)<textarea id="${prefix}-checks" rows="4" placeholder='[{"kind":"max_chars", "value":500}]'>[]</textarea></label>
    <p class="hint">json, json_keys, max_chars, contains, not_contains, list_count, python_syntax, exact_answer 지원. 코드 구문 검사는 실행 검증이 아닙니다.</p>
    <label>Python 함수 테스트 (JSON, 선택)<textarea id="${prefix}-code-tests" rows="4" placeholder='{"function":"add","cases":[{"args":[2,3],"expected":5}]}'> {}</textarea></label>
    <p class="hint">Python 출력 형식에서 사용. Docker와 로컬 python:3.12-slim 이미지가 있어야 실행합니다. 없으면 미실행으로 기록합니다.</p>
    </details>
  </div>`;
}
function readSpec(prefix) {
  return {requirements:$(`#${prefix}-requirements`).value.split('\n').map(t=>t.trim()).filter(Boolean),
    outputFormat:$(`#${prefix}-format`).value, expectedAnswer:$(`#${prefix}-expected`).value,
    references:JSON.parse($(`#${prefix}-references`).value || '[]'), checks:JSON.parse($(`#${prefix}-checks`).value || '[]'),
    codeTests:JSON.parse($(`#${prefix}-code-tests`).value || '{}')};
}
function fillSpec(prefix,spec) {
  $(`#${prefix}-requirements`).value=(spec.requirements || []).join('\n');
  $(`#${prefix}-format`).value=spec.outputFormat || 'text';
  $(`#${prefix}-expected`).value=spec.expectedAnswer || '';
  $(`#${prefix}-references`).value=JSON.stringify(spec.references || [],null,2);
  $(`#${prefix}-checks`).value=JSON.stringify(spec.checks || [],null,2);
  $(`#${prefix}-code-tests`).value=JSON.stringify(spec.codeTests || {},null,2);
}
function axisCell(axis) { return !axis ? '—' : axis.score === null ? esc(statusNames[axis.status]) : `${axis.score} / 3`; }
function reportDetails(report) {
  if (!report) return '';
  const axes = Object.entries(report.axes).map(([key,a])=>`<article class="claim-card"><b>${esc(config.criteria[key])}: ${axisCell(a)}</b><p>${esc(a.description)}</p>
    <p>JEV 확신도: ${typeof a.confidence === 'number' ? a.confidence.toFixed(2) : '미제공'} (정확도 보장 아님)</p>
    ${a.answerText ? `<p>답변 ${esc(a.answerRef)}: ${esc(a.answerText)}</p>` : ''}
    ${a.sourceText ? `<p>근거 ${esc(a.sourceRef)}: ${esc(a.sourceText)}</p>` : ''}
    ${a.notes.map(n=>`<p class="hint">${esc(n)}</p>`).join('')}</article>`).join('');
  const requirements = report.requirements.map(r=>`<li>${esc(r.id)} ${esc(r.text)} — ${esc(requirementNames[r.status])}</li>`).join('');
  const checks = report.checks.map(c=>`<li>${esc(c.kind)}: ${c.passed?'통과':'실패'} ${esc(c.detail)}</li>`).join('');
  const render = report.inspection;
  const sources = Object.entries(report.sourceMetadata || {}).map(([id,m])=>`<li>${esc(id)}: ${esc(m.title)} (${esc(m.kind)}) ${m.reference ? `<a href="${esc(m.reference.url)}" target="_blank" rel="noopener">등록 출처</a> · 확인일 ${esc(m.reference.checkedAt)}` : ''}</li>`).join('');
  const screenshot = render?.rendered && /^[a-f0-9]{64}\.png$/.test(render.screenshot || '') ? `<a href="/api/artifacts/${render.screenshot}" target="_blank" rel="noopener">첫 화면 캡처</a>` : '';
  return `<details><summary>판정·근거·검사 보기</summary><p>평가 신뢰성: 미검증 · 종합 순위 미산출</p>${report.issues.map(s=>`<p>${esc(s)}</p>`).join('')}
    <ul>${requirements}</ul>${checks ? `<ul>${checks}</ul>` : ''}${sources ? `<details><summary>등록 근거 목록</summary><ul>${sources}</ul></details>` : ''}${render ? `<p>HTML 렌더링: ${render.rendered?'관측 완료':'미완료'} ${esc(render.reason || render.limitation || '')} ${screenshot}</p>` : ''}
    ${report.executionDetails ? `<p>코드 실행: ${esc(report.executionDetails.status)} ${esc(report.executionDetails.reason || report.executionDetails.scope || '')}</p>` : ''}<div class="claim-list">${axes}</div><p>입력 해시: ${esc(report.inputHash)}</p></details>`;
}
function render() {
  const selected = $('#category-filter').value;
  const filtered = items.filter(i=>!selected || i.category===selected);
  $('#empty-state').style.display=filtered.length?'none':'block';
  const modern=filtered.filter(i=>i.report && !i.stale);
  $('#summary').innerHTML=`<article class="summary-card"><b>현재 기준 평가 ${modern.length}개</b><p>사람 검토 필요 ${modern.filter(i=>i.report.needsReview).length}개</p><small>구버전·설정 변경 결과를 평균내지 않습니다. 독립적인 사람 검증 전입니다.</small></article>`;
  $('#result-rows').innerHTML=filtered.map(i=>{
    const axes=i.report?.axes;
    let status=i.report?(i.report.needsReview?'검토 필요':'판정 완료 · 신뢰성 미검증'):i.scores?'구버전 결과':'미평가';
    if (i.stale) status='설정 변경 · 재평가 필요';
    if (i.needsReclassification) status='유형 재분류 필요';
    return `<tr><td><strong>${esc(i.title)}</strong><p>${esc(i.category)}</p><small>${esc(i.rubricVersion || config.rubricVersion)} / ${esc(i.evaluatorModel || '')}</small>
      ${reportDetails(i.report)}${i.scores&&!i.report?`<details><summary>기존 점수 (비교 제외)</summary><pre>${esc(JSON.stringify(i.scores,null,2))}</pre></details>`:''}</td>
      <td>${esc(i.model)}</td><td>${esc(status)}</td>${Object.keys(config.criteria).map(k=>`<td>${axisCell(axes?.[k])}</td>`).join('')}
      <td><button data-evaluate="${i.responseId}">평가</button><button data-settings="${i.caseId}">공통 설정</button><button data-history="${i.responseId}">이력</button></td></tr>`;
  }).join('');
}
async function loadResults() {
  items=(await api('/api/results')).items;
  const selected=$('#category-filter').value;
  $('#category-filter').innerHTML='<option value="">모든 유형</option>'+[...new Set(items.map(i=>i.category))].map(c=>`<option>${esc(c)}</option>`).join('');
  $('#category-filter').value=selected; render();
}
function openSettings(caseId) {
  editingCase=items.find(i=>i.caseId===caseId);
  const select=$('#settings-form [name=category]');
  select.innerHTML='<option value="">유형 선택</option>'+config.categories.map(c=>`<option>${esc(c)}</option>`).join('');
  select.value=editingCase.category;
  fillSpec('edit',editingCase.evaluationSpec); $('#settings-dialog').showModal();
}
document.addEventListener('click', async event=>{
  const button=event.target.closest('button'); if (!button) return;
  try {
    if (button.dataset.view) {
      document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id===`${button.dataset.view}-view`));
      document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t===button));
      if (button.dataset.view==='results') await loadResults();
    }
    if (button.dataset.settings) openSettings(Number(button.dataset.settings));
    if (button.dataset.evaluate) {
      button.disabled=true; await api(`/api/responses/${button.dataset.evaluate}/evaluate`,{}); await loadResults(); notify('평가를 저장했습니다. 근거와 검증 범위를 확인하세요.');
    }
    if (button.dataset.history) {
      const data=await api(`/api/responses/${button.dataset.history}/history`);
      $('#history-content').innerHTML=data.items.map(i=>`<article><h3>${esc(i.createdAt)} · ${esc(i.rubricVersion)}</h3><p>${esc(i.model)}</p>${i.report?reportDetails(i.report):`<pre>${esc(JSON.stringify(i.scores,null,2))}</pre>`}</article>`).join('') || '<p>평가 이력이 없습니다.</p>';
      $('#history-dialog').showModal();
    }
  } catch(error) { notify(error.message); } finally { button.disabled=false; }
});
$('#case-form').addEventListener('submit',async event=>{
  event.preventDefault(); const button=event.submitter; button.disabled=true;
  try {
    const data=new FormData(event.currentTarget);
    const responses=Object.fromEntries(config.models.map(m=>[m,data.get(`response:${m}`)]));
    const payload=Object.fromEntries(['title','category','prompt','conversationHistory','attachmentText'].map(k=>[k,data.get(k)]));
    await api('/api/cases',{...payload,responses,evaluationSpec:readSpec('new')});
    notify('저장했습니다. 결과 화면에서 평가할 수 있습니다.');
    event.target.reset();
  } catch(error) { notify(error.message); } finally { button.disabled=false; }
});
$('#settings-form').addEventListener('submit',async event=>{
  event.preventDefault(); const button=event.submitter; button.disabled=true;
  try {
    await api(`/api/cases/${editingCase.caseId}/settings`,{category:$('#settings-form [name=category]').value,evaluationSpec:readSpec('edit')});
    $('#settings-dialog').close(); await loadResults(); notify('모든 모델에 적용할 공통 설정을 저장했습니다.');
  } catch(error) { notify(error.message); } finally { button.disabled=false; }
});
$('#close-settings').onclick=()=>$('#settings-dialog').close();
$('#close-history').onclick=()=>$('#history-dialog').close();
$('#category-filter').onchange=render;
$('#evaluate-all').onclick=async event=>{
  event.target.disabled=true;
  try { const result=await api('/api/evaluate-pending',{}); await loadResults(); notify(`${result.completed}개 완료 ${result.errors.map(e=>e.error).join(' / ')}`); }
  catch(error) {notify(error.message);} finally {event.target.disabled=false;}
};
(async()=>{
  try {
    config=await api('/api/config');
    $('#new-spec').innerHTML=specFields('new'); $('#edit-spec').innerHTML=specFields('edit');
    $('#response-fields').innerHTML=config.models.map(m=>`<label class="response-card"><span>${esc(m)}</span><textarea name="response:${esc(m)}" rows="8"></textarea></label>`).join('');
    $('#api-status').textContent=config.apiKeyConfigured?`${config.evaluatorModel} · ${config.rubricVersion}`:'API 키 필요';
  } catch(error) {notify(error.message);}
})();
