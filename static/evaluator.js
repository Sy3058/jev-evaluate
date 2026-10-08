let config;
let items = [];
let editingCase = null;
const expandedCases = new Set();
let mode = 'model';
let bots = [];
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const statusNames = {rated:'판정', unverifiable:'판정 불가', not_applicable:'해당 없음'};
const requirementNames = {MET:'충족', PARTIAL:'부분 충족', UNMET:'미충족', UNKNOWN:'판정 불가', SAFE_REFUSAL:'적절한 안전 거절'};
function notify(message) { $('#toast').textContent=message; $('#toast').classList.add('show'); setTimeout(()=>$('#toast').classList.remove('show'),5000); }
async function api(url, body) {
  const response = await fetch(url, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json(); if (!response.ok) throw new Error(data.error || '요청 실패'); return data;
}
async function uploadArtifact(responseId, file) {
  if (file.size < 1 || file.size > 10*1024*1024) throw new Error('생성 파일은 1바이트 이상 10MB 이하로 올려 주세요.');
  const response=await fetch(`/api/responses/${responseId}/artifact`,{
    method:'POST',headers:{'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(file.name)},body:file
  });
  const data=await response.json();
  if (!response.ok) throw new Error(data.error || '생성 파일 업로드 실패');
  return data;
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
    <p class="hint">수동 등록 출처는 입력한 발췌문을 사용합니다. AI Bot 답변에 인용된 공개 HTTPS 링크는 평가 시 별도로 본문을 확인합니다.</p>
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
function factReviewCounts(fact) {
  const claims = fact?.claims || [];
  return {
    allocation: fact?.scoreAllocationReviewCount ?? claims.filter(c=>c.relation === 'CONTRADICTED' && c.lowConfidenceFields?.includes('importanceConfidence')).length,
    verdict: fact?.factVerdictReviewCount ?? claims.filter(c=>c.lowConfidenceFields?.some(f=>f === 'relationConfidence' || f === 'sourceConfidence')).length
  };
}
function axisCell(axis, scoreType, fact=null) {
  if (!axis) return '—';
  if (axis.score === null) return axis.scoringMethod === 'confirmed_claim_contradictions' && axis.status === 'unverifiable' ? '미검증 · 근거 확인 필요' : esc(statusNames[axis.status]);
  const score = `${scoreType === 'expected_level_0_3' && axis.scoringMethod !== 'confirmed_claim_contradictions' ? Number(axis.score).toFixed(2) : axis.score} / 3`;
  if (axis.scoringMethod === 'confirmed_claim_contradictions' && fact) {
    const review = factReviewCounts(fact);
    const labels = [review.allocation ? '점수 배점 검토 필요' : '', review.verdict ? '사실성 판정 검토 필요' : ''].filter(Boolean);
    if (axis.needsReview && !labels.length) labels.push(
      fact.scoreAllocationReviewCount === undefined ? '이전 기준 검토 필요' : '근거 확인 필요');
    return `${score}${labels.length ? ` · ${labels.join(' · ')}` : ''}`;
  }
  return `${score}${axis.needsReview && scoreType === 'violation_count_0_3' ? ' · 검토 필요' : ''}`;
}
function issueEvidence(v) {
  if (v.requirementRef) return `<article class="issue-card linked-issue">
    <b>${esc(v.severity === 'major' ? '중대' : '경미')} · ${esc(v.label)}</b>
    <p class="hint">JEV 선택 확신도 ${typeof v.confidence === 'number' ? v.confidence.toFixed(2) : '미제공'} · 점수 결정 기준은 아래 지침·관측 근거의 연결입니다.</p>
    <div class="issue-pointers"><span>지침 ${esc(v.requirementRef)}</span><span>관측 ${esc(v.observationRef)}</span><span>대상 ${esc(({MESSAGE:'채팅 답변',ARTIFACT:'생성 파일',BOTH:'전체'})[v.target] || '미확인')}</span></div>
    <div class="linked-evidence"><div><strong>적용 지침 · ${esc(v.requirementSection || '원문 항목')}</strong><p>${esc(v.requirementText)}</p></div>
    <div><strong>실제 관측</strong><p>${['FULL_MESSAGE','FULL_ARTIFACT'].includes(v.observationRef) && v.kind === 'OMISSION' ? 'JEV가 해당 출력 전체에서 필수 내용의 누락을 선택했습니다. 아래에서 본문을 직접 확인할 수 있습니다.' : esc(v.observedText || '관측 내용 없음')}</p></div></div>
    ${['FULL_MESSAGE','FULL_ARTIFACT'].includes(v.observationRef) ? `<details><summary>검사한 출력 본문 보기</summary><pre>${esc(v.observedText)}</pre></details>` : ''}
  </article>`;
  const raw = v.answerText || '';
  const htmlCode = /<\/?(?:style|head|body|html|table|div|section|main)\b|\{[^}]{0,120}:[^}]*\}/i.test(raw);
  const multiRule = (v.sourceText || '').split('\n').filter(line => /^\s*(?:[-*] |\d+[.)] |#{1,6} )/.test(line)).length > 1;
  const uncertain = typeof v.confidence !== 'number' || v.confidence < .7;
  return `<article class="issue-card${uncertain ? ' uncertain' : ''}">
    <b>${esc(v.severity === 'major' ? '중대' : '경미')} · ${esc(v.label)}</b>
    <p class="issue-warning">${uncertain ? `위반 선택의 확신도가 낮습니다 (${typeof v.confidence === 'number' ? v.confidence.toFixed(2) : '미제공'}). ` : ''}${multiRule ? '선택된 기준에 여러 세부 항목이 있지만, 어떤 항목을 어겼는지는 기록되지 않았습니다.' : '평가 기록에 구체적인 위반 행동 설명이 없습니다. 아래 위치와 기준을 직접 확인해야 합니다.'}</p>
    <div class="issue-pointers"><span>답변 위치 ${esc(v.answerRef || '없음')}</span><span>기준 위치 ${esc(v.sourceRef || '없음')}</span></div>
    ${htmlCode ? '<p class="hint">선택된 답변 구간은 HTML/CSS 원본입니다. 화면에 보이는 위반 문장과 직접 연결되지 않았습니다.</p>' : raw ? `<p class="issue-excerpt">${esc(raw.slice(0, 350))}${raw.length > 350 ? '…' : ''}</p>` : '<p class="hint">답변의 특정 문장이 기록되지 않았습니다.</p>'}
    <details><summary>선택된 원문과 기준 전체 보기</summary><h4>답변 ${esc(v.answerRef || '없음')}</h4><pre>${esc(raw || '본문 위치 없음')}</pre><h4>기준 ${esc(v.sourceRef || '없음')}</h4><pre>${esc(v.sourceText || '기준 위치 없음')}</pre></details>
  </article>`;
}
function qualityAxis(key, a, report) {
  const uncertain = (a.violations || []).some(v => !v.requirementRef && (typeof v.confidence !== 'number' || v.confidence < .7));
  const inconsistent = a.score !== null && (uncertain || (report.botAssessment?.rules || []).some(r => r.id === a.sourceRef && r.verdict === 'UNKNOWN'));
  const open = key === 'instruction_following' && (a.score !== 3 || a.needsReview);
  const reasons = a.pendingReasonChoices || [];
  const reasonList = reasons.length ? `<div class="pending-reasons"><b>판정 불가 이유 · 자동 검사 선택 ${reasons.length}개</b><ul>${reasons.map(r=>`<li><strong>${esc(r.label)}</strong><span>${esc(r.detail)}</span><small>${esc(r.code)}</small></li>`).join('')}</ul></div>` : a.pendingReasons?.map(n=>`<p class="issue-warning">판정 불가 이유: ${esc(n)}</p>`).join('') || '';
  const judgeReason = key === 'instruction_following' && a.score === null && a.judgeUnverifiableReason ? `<p class="judge-reason">JEV 자료 관측 선택: ${esc(a.judgeUnverifiableReason.label)} (${esc(a.judgeUnverifiableReason.code)}) · 확신도 ${typeof a.judgeUnverifiableReason.confidence === 'number' ? a.judgeUnverifiableReason.confidence.toFixed(2) : '미제공'}</p>` : '';
  return `<details class="quality-axis" ${open ? 'open' : ''}><summary><b>${esc(config.criteria[key])}: ${axisCell(a, report.scoreType)}</b><span>${esc(a.description)}${inconsistent ? ' · 기존 점수의 근거 검토 필요' : ''}</span></summary>
    ${inconsistent ? '<p class="issue-warning">이 저장된 점수는 불확실한 위반 선택을 감점에 사용했습니다. 강제 재평가 후 새 기준으로 확인해 주세요.</p>' : ''}
    ${a.score === null ? `${reasonList}${judgeReason}<p class="hint">자동 검사 선택은 JEV가 고른 위반의 순서·확신도·근거를 검사한 결과입니다. JEV 자료 관측 선택과 서로 다를 수 있습니다.</p>` : ''}
    ${(a.violations || []).map(issueEvidence).join('') || '<p class="hint">기록된 위반이 없습니다.</p>'}
    ${a.rejectedFindings?.map(n=>`<p class="hint">제외: ${esc(n)}</p>`).join('') || ''}
    ${a.notes?.filter(n=>!(a.pendingReasons || []).includes(n) && !(a.rejectedFindings || []).includes(n)).map(n=>`<p class="hint">${esc(n)}</p>`).join('') || ''}
  </details>`;
}
function modelQualityAxis(key, a, report, factDetails='') {
  const open = (a.score !== null && a.score < 2.5) || a.needsReview;
  const fact = key === 'truthfulness' ? report.factVerification : null;
  const majorCount = fact?.majorCount ?? (fact?.claims || []).filter(c=>c.relation === 'CONTRADICTED' && c.importance === 'HIGH').length;
  const minorCount = fact?.minorCount ?? (fact?.claims || []).filter(c=>c.relation === 'CONTRADICTED' && c.importance === 'LOW').length;
  const factSummary = fact ? a.score === null ? (fact.status === 'unverifiable' ? '근거 대조 불가' : '대조할 사실 주장 없음') : majorCount ? `핵심 주장 모순 ${majorCount}건` : minorCount ? `부수 주장 모순 ${minorCount}건` : '확인된 모순 없음' : a.description;
  const requirementDetails = key === 'instruction_following' && report.requirements?.length
    ? `<div class="bot-rules">${report.requirements.map(r=>`<article class="claim-card"><b>${esc(r.id)} · ${esc(requirementNames[r.status] || r.status)}</b><p>${esc(r.text)}</p></article>`).join('')}</div>` : '';
  return `<details class="quality-axis" ${open ? 'open' : ''}><summary><b>${esc(config.criteria[key])}: ${axisCell(a, report.scoreType, fact)}</b><span>${esc(factSummary)}</span></summary>
    ${fact ? '' : a.scoringMethod === 'confirmed_claim_contradictions' ? '<p class="hint">근거와 모순으로 판정된 주장의 개수와 중요도로 계산한 정수 점수입니다. 미검증 주장은 감점하지 않습니다. 선택 확신도가 낮은 판정은 검토 필요로 표시합니다.</p>' : `<p>JEV 확신도: ${typeof a.confidence === 'number' ? a.confidence.toFixed(2) : '미제공'} (정확도 보장 아님)</p>`}
    ${report.scoreType === 'expected_level_0_3' && a.status === 'rated' && a.probabilities ? `<p>단계별 확률: ${[0,1,2,3].map(n=>`${n}점 ${((a.probabilities[String(n)] || 0)*100).toFixed(1)}%`).join(' · ')}</p><p class="hint">점수는 단계별 확률의 가중 평균입니다. 표시된 설명은 가장 가능성 높은 단계 기준입니다.</p>` : ''}
    ${key === 'truthfulness' && report.factVerification ? '' : a.answerText ? `<div class="linked-evidence"><div><strong>답변 · ${esc(a.answerRef)}</strong><p>${esc(a.answerText)}</p></div>${a.sourceText ? `<div><strong>근거 · ${esc(a.sourceRef)}</strong><p>${esc(a.sourceText)}</p></div>` : ''}</div>` : a.sourceText ? `<p>근거 ${esc(a.sourceRef)}: ${esc(a.sourceText)}</p>` : ''}
    ${fact ? '' : a.notes?.map(n=>`<p class="hint">${esc(n)}</p>`).join('') || ''}${requirementDetails}${factDetails}
  </details>`;
}
function factClaimCard(c, issue=false) {
  const label = c.relation === 'CONTRADICTED' ? '근거와 모순' : c.relation === 'UNVERIFIED' ? '미검증' : c.relation === 'SUPPORTED' ? '근거와 일치' : '사실 주장 아님';
  const allocationReview = c.scoreAllocationReviewFields?.length ?? (c.relation === 'CONTRADICTED' && c.lowConfidenceFields?.includes('importanceConfidence'));
  const verdictReview = c.factVerdictReviewFields?.length ?? c.lowConfidenceFields?.some(f=>f === 'relationConfidence' || f === 'sourceConfidence');
  return `<article class="${issue ? 'issue-card' : 'claim-card'}"><b>${esc(c.id)} · ${label}${c.importance === 'HIGH' ? ' · 핵심' : ''}${allocationReview ? ' · 점수 배점 검토 필요' : ''}${verdictReview ? ' · 사실성 판정 검토 필요' : ''}</b>
    <div class="issue-pointers"><span>답변 ${esc(c.id)}</span>${c.sourceRef && c.sourceRef !== 'NONE' ? `<span>근거 ${esc(c.sourceRef)}</span>` : ''}</div>
    <div class="linked-evidence"><div><strong>답변 주장</strong><p>${esc(c.text)}</p></div>${c.sourceRef && c.sourceRef !== 'NONE' ? `<div><strong>등록 근거</strong><p>${esc(c.sourceText || '')}</p></div>` : ''}</div>
    ${allocationReview ? '<p class="hint">JEV가 선택한 HIGH/LOW 중요도로 점수를 계산했습니다. 중요도 선택의 확신도가 낮아 배점 검토가 필요합니다.</p>' : ''}
    ${verdictReview ? '<p class="hint">JEV가 선택한 일치·모순 관계로 점수를 계산했습니다. 관계 또는 근거 선택의 확신도가 낮아 사실성 판정 검토가 필요합니다.</p>' : ''}
    ${c.relation !== 'NOT_APPLICABLE' ? `<details><summary>판정 정보 보기</summary><p class="hint">JEV 선택 확신도 · 중요도 ${typeof c.importanceConfidence === 'number' ? c.importanceConfidence.toFixed(2) : '미제공'} · 관계 ${typeof c.relationConfidence === 'number' ? c.relationConfidence.toFixed(2) : '미제공'} · 근거 ${typeof c.sourceConfidence === 'number' ? c.sourceConfidence.toFixed(2) : '미제공'}</p>${c.verificationMethod === 'exact_numeric_reference' ? '<p class="hint">등록된 기준 답안과 숫자를 직접 비교했습니다.</p>' : ''}</details>` : ''}
  </article>`;
}
function reportDetails(report, botMode=false) {
  if (!report) return '';
  const fact = report.factVerification;
  const claims = fact?.claims || [];
  const factReview = factReviewCounts(fact);
  const contradictions = claims.filter(c=>c.relation === 'CONTRADICTED');
  const reviewClaims = claims.filter(c=>c.relation !== 'CONTRADICTED' &&
    (c.relation === 'UNVERIFIED' || c.factVerdictReviewFields?.length ||
     c.lowConfidenceFields?.some(f=>f === 'relationConfidence' || f === 'sourceConfidence')));
  const otherClaims = claims.filter(c=>c.relation !== 'CONTRADICTED' && !reviewClaims.includes(c));
  const factDetails = fact ? `<section class="fact-details">
    <p>사실 주장 ${fact.claimCount}개 중 ${fact.verifiedCount}개 대조 · 모순 ${contradictions.length}개 · 미검증 ${fact.unverifiedCount}개${factReview.allocation ? ` · 점수 배점 검토 ${factReview.allocation}개` : ''}${factReview.verdict ? ` · 사실성 판정 검토 ${factReview.verdict}개` : ''}${fact.lowConfidenceExcludedCount ? ` · 사실 주장 여부 검토 ${fact.lowConfidenceExcludedCount}개` : ''}${fact.citedEvidenceReviewCount ? ` · 출처 신뢰성 검토 ${fact.citedEvidenceReviewCount}개` : ''}${fact.skippedCandidateCount ? ` · 검사 제외 후보 ${fact.skippedCandidateCount}개` : ''}</p>
    ${fact.extractionIssue ? `<p class="issue-warning">${esc(fact.extractionIssue)}</p>` : ''}
    ${contradictions.map(c=>factClaimCard(c,true)).join('')}
    ${reviewClaims.length ? `<details><summary>검토가 필요한 주장 ${reviewClaims.length}개 보기</summary>${reviewClaims.map(c=>factClaimCard(c)).join('')}</details>` : ''}
    ${otherClaims.length ? `<details><summary>나머지 주장 ${otherClaims.length}개 보기</summary>${otherClaims.map(c=>factClaimCard(c)).join('')}</details>` : ''}
    ${fact.score !== null && (fact.unverifiedCount || fact.skippedCandidateCount) ? '<p class="hint">미검증 주장은 감점하지 않았습니다. 현재 점수는 답변 전체의 사실성을 보증하지 않습니다.</p>' : ''}
    </section>` : '';
  const axes = Object.entries(report.axes).map(([key,a])=>report.scoreType === 'violation_count_0_3'
    ? qualityAxis(key,a,report) : modelQualityAxis(key,a,report,key === 'truthfulness' ? factDetails : '')).join('');
  const requirements = report.requirements.map(r=>`<li>${esc(r.id)} ${esc(r.text)} — ${esc(requirementNames[r.status])}</li>`).join('');
  const checks = report.checks.map(c=>`<li>${esc(c.kind)}: ${c.passed?'통과':'실패'} ${esc(c.detail)}</li>`).join('');
  const render = report.inspection;
  const sources = Object.entries(report.sourceMetadata || {}).map(([id,m])=>`<li>${esc(id)}: ${esc(m.title)} (${esc(m.kind)}) ${m.reference ? `<a href="${esc(m.reference.url)}" target="_blank" rel="noopener">${m.kind === 'verified_cited_web_page' ? '확인한 웹 페이지' : '등록 출처'}</a> · 확인일 ${esc(m.reference.checkedAt)}` : ''}</li>`).join('');
  const linkNames = {verified:'본문 확인',unavailable:'접속 실패',blocked:'접속 차단',no_content:'본문 부족',unsupported:'형식 미지원'};
  const linkItems = report.linkVerification?.items || [];
  const links = linkItems.map(item=>`<li><b>${esc(linkNames[item.status] || item.status)}</b> · ${item.status === 'verified' ? `<a href="${esc(item.finalUrl)}" target="_blank" rel="noopener">${esc(item.title || item.url)}</a>` : esc(item.url)}${item.sourceRef ? ` · 근거 ${esc(item.sourceRef)}` : ''}${item.claimContext ? `<p>인용 앞 문맥: ${esc(item.claimContext)}</p>` : ''}${item.reason ? `<p>${esc(item.reason)}</p>` : ''}<small>확인 시각 ${esc(item.checkedAt)}${item.httpStatus ? ` · HTTP ${esc(item.httpStatus)}` : ''}</small></li>`).join('');
  const screenshot = render?.rendered && /^[a-f0-9]{64}\.png$/.test(render.screenshot || '') ? `<a href="/api/artifacts/${render.screenshot}" target="_blank" rel="noopener">첫 화면 캡처</a>` : '';
  const groundingNames = {NONE:'확인된 자료 불일치 없음',MISQUOTE:'원문과 다른 인용',INVENTED_EVENT:'자료에 없는 사실',WRONG_ATTRIBUTION:'귀속·시간 오류',OUTSIDE_SOURCE:'자료 밖 사실 사용',UNKNOWN:'대조 보류'};
  const lengthNames = {NONE:'확인된 분량 문제 없음',REPETITION:'본문 반복',IRRELEVANT:'무관한 설명',OVERCOMPRESSED:'과도한 압축',UNKNOWN:'본문 확인 불가'};
  const botEvidence = botMode ? `${report.groundingIssue ? `<p>자료 대조: ${esc(groundingNames[report.groundingIssue] || report.groundingIssue)} · 원문 일치 인용 ${report.quoteObservations?.exactMatches?.length || 0}건 · 확인 필요 인용 ${report.quoteObservations?.unmatchedQuotes?.length || 0}건</p>` : ''}${report.responseLengthIssue ? `<p>본문 분량: ${esc(lengthNames[report.responseLengthIssue] || report.responseLengthIssue)}</p>` : ''}` : '';
  const overview = `<div class="quality-overview">${axes}</div>`;
  return `${overview}<details><summary>검사·원본 기록 보기</summary><p>평가 신뢰성: 미검증 · 종합 순위 미산출 · ${report.scoreType === 'expected_level_0_3' ? '대부분의 축은 Score 확률 가중 평균' : report.scoreType === 'violation_count_0_3' ? '확인된 위반 개수·심각도에 따른 정수 점수' : '기존 단계 선택'}${fact ? ' · Truthfulness는 확인된 주장 모순 기준 정수 점수' : ''}</p>${report.jevRetryCount ? `<p class="hint">JEV 점수·확률 불일치로 ${esc(report.jevRetryCount)}회 재요청했습니다.</p>` : ''}${report.issues.map(s=>`<p>${esc(s)}</p>`).join('')}
    ${botEvidence}${botMode ? factDetails : ''}${links ? `<details><summary>답변 인용 링크 확인 · ${linkItems.filter(item=>item.status === 'verified').length}/${linkItems.length}개 본문 확인</summary><p class="hint">링크 접속과 본문 존재는 주장 정확성 또는 실제 검색 도구 사용의 증거가 아닙니다.</p><ul>${links}</ul>${report.linkVerification.skippedCount ? `<p>검사 상한으로 ${esc(report.linkVerification.skippedCount)}개 링크를 확인하지 않았습니다.</p>` : ''}</details>` : ''}${!botMode && requirements ? `<ul>${requirements}</ul>` : ''}${checks ? `<ul>${checks}</ul>` : ''}${sources ? `<details><summary>등록 근거 목록</summary><ul>${sources}</ul></details>` : ''}${render ? `<p>HTML 렌더링: ${render.rendered?'관측 완료':'미완료'} ${esc(render.reason || render.limitation || '')} ${screenshot}</p>` : ''}
    ${report.executionDetails ? `<p>코드 실행: ${esc(report.executionDetails.status)} ${esc(report.executionDetails.reason || report.executionDetails.scope || '')}</p>` : ''}<p>입력 해시: ${esc(report.inputHash)}</p></details>`;
}
function botDetails(assessment) {
  if (!assessment) return '';
  return `<details class="bot-assessment"><summary>Bot 지침 준수 상세 · ${esc(({violation:'위반 있음',expectation_mismatch:'기대 결과 미충족',review:'사람 검토 필요',compliant:'준수 확인'})[assessment.status] || '미확인')}</summary>
    ${assessment.expectedResult ? `<article class="claim-card"><b>점검 관점·기대 결과 · ${esc(assessment.expectedResult.label)}</b><p><b>점검 관점</b> ${esc(assessment.expectedResult.checkFocus)}</p><p><b>기대 결과</b> ${esc(assessment.expectedResult.expectedBehavior)}</p>${assessment.expectedResult.reason ? `<p>${esc(assessment.expectedResult.reason)}</p>` : ''}<p>답변 근거 ${esc(assessment.expectedResult.answerRef)}: ${esc(assessment.expectedResult.answerText || '특정 위치 없음 / 내용 누락')}</p></article>` : ''}
    <div class="bot-rules">${assessment.rules.map(rule=>`<article class="claim-card"><b>${esc(rule.id)} · ${esc(rule.label)}</b><p>${esc(rule.instruction)}</p><p>${esc(rule.reason)}</p>
      <p>답변 근거 ${esc(rule.answerRef)}: ${esc(rule.answerText || '특정 위치 없음 / 내용 누락')}</p><small>확신도 ${typeof rule.confidence === 'number' ? rule.confidence.toFixed(2) : '미제공'}${rule.needsReview ? ' · 사람 검토 필요' : ''}</small></article>`).join('')}</div></details>`;
}
function resultStatus(i) {
  if (i.needsReclassification) return '유형 재분류 필요';
  if (i.stale) return '재평가 필요';
  if (mode === 'ai_bot' && i.report?.botAssessment) {
    const assessment = i.report.botAssessment;
    const label = assessment.status === 'review' && assessment.expectedResult?.verdict === 'COMPLIANT'
      ? '기대 결과 충족 · 지침 검토 필요'
      : ({violation:'지침 위반 있음',expectation_mismatch:'기대 결과 미충족',review:'사람 검토 필요',compliant:'지침·기대 결과 충족'})[assessment.status] || '판정 확인 필요';
    return i.report.needsReview && assessment.status !== 'review' ? `${label} · 검토 필요` : label;
  }
  if (i.report) return i.report.needsReview ? '검토 필요' : '판정 완료 · 신뢰성 미검증';
  return i.scores ? '구버전 결과' : '미평가';
}
function render() {
  const selected = $('#category-filter').value;
  const botSelected = $('#bot-filter').value;
  $('#export-link').href=`/api/export.csv?mode=${mode}${mode === 'ai_bot' && botSelected ? `&botId=${encodeURIComponent(botSelected)}` : ''}`;
  const filtered = items.filter(i=>(!selected || i.category===selected) && (mode !== 'ai_bot' || !botSelected || String(i.botId)===botSelected));
  $('#empty-state').style.display=filtered.length?'none':'block';
  const modern=filtered.filter(i=>i.report && !i.stale);
  $('#summary').innerHTML=`<article class="summary-card"><b>현재 기준 평가 ${modern.length}개</b><p>${mode === 'ai_bot' ? `지침·기대 결과 미충족 ${modern.filter(i=>['violation','expectation_mismatch'].includes(i.report.botAssessment?.status)).length}개 · ` : ''}사람 검토 필요 ${modern.filter(i=>i.report.needsReview).length}개</p><small>구버전·설정 변경 결과를 평균내지 않습니다. 독립적인 사람 검증 전입니다.</small></article>`;
  const cases = new Map();
  filtered.forEach(i=>{
    if (!cases.has(i.caseId)) cases.set(i.caseId, []);
    cases.get(i.caseId).push(i);
  });
  $('#result-rows').innerHTML=[...cases].map(([caseId, responses])=>{
    const first=responses[0];
    const completed=responses.filter(i=>i.report && !i.stale && !i.needsReclassification).length;
    const review=responses.filter(i=>i.report?.needsReview && !i.stale).length;
    const stale=responses.filter(i=>i.stale || i.needsReclassification).length;
    const violations=responses.filter(i=>!i.stale && i.report?.botAssessment?.status === 'violation').length;
    const mismatches=responses.filter(i=>!i.stale && i.report?.botAssessment?.status === 'expectation_mismatch').length;
    const status=[`${completed}/${responses.length}개 현재 평가`, mode === 'ai_bot' && violations?`${violations}개 지침 위반`:'', mode === 'ai_bot' && mismatches?`${mismatches}개 기대 결과 미충족`:'', stale?`${stale}개 재평가 필요`:'', review?`${review}개 검토 필요`:''].filter(Boolean).join(' · ');
    const open=expandedCases.has(caseId);
    const modelRows=responses.map(i=>{
      const axes=i.report?.axes;
      return `<tr><td class="model">${esc(i.model)}</td><td>${esc(resultStatus(i))}</td>${Object.keys(config.criteria).map(k=>`<td>${axisCell(axes?.[k], i.report?.scoreType, k === 'truthfulness' ? i.report?.factVerification : null)}</td>`).join('')}
        <td><button data-evaluate="${i.responseId}">변경분 평가</button><button data-force-evaluate="${i.responseId}">강제 재평가</button><button data-history="${i.responseId}">이력</button><label class="file-action">${i.artifact ? '파일 교체' : '파일 추가'}<input type="file" data-upload-for="${i.responseId}" accept=".html,.htm,.pdf,.xlsx" hidden></label></td></tr>
        <tr class="model-evidence"><td colspan="8"><small>${esc(i.rubricVersion || config.rubricVersion)} / ${esc(i.evaluatorModel || '')}</small>
        ${i.artifact ? `<p>생성 파일: <a href="${esc(i.artifact.downloadUrl)}">${esc(i.artifact.filename)}</a> · ${esc(i.artifact.format.toUpperCase())} · ${Math.ceil(i.artifact.size/1024)}KB${i.artifact.textTruncated ? ' · 내용 일부만 추출' : ''}${i.artifact.extractionNote ? ` · ${esc(i.artifact.extractionNote)}` : ''}</p>` : '<p>생성 파일 없음</p>'}
        ${mode === 'ai_bot' && i.report?.botAssessment?.expectedResult ? `<p><b>테스트 기대 결과: ${esc(i.report.botAssessment.expectedResult.label)}</b> · JEV 확신도 ${typeof i.report.botAssessment.expectedResult.confidence === 'number' ? i.report.botAssessment.expectedResult.confidence.toFixed(2) : '미제공'}${i.artifact ? ' · 생성 파일 업로드·다운로드 확인' : ''}</p>` : ''}
        ${i.report?.timings ? `<p class="hint">최근 평가 소요: 전체 ${(i.report.timings.totalMs/1000).toFixed(1)}초 · HTML 검사 ${(i.report.timings.htmlInspectionMs/1000).toFixed(1)}초${i.report.timings.linkCheckMs ? ` · 링크 ${(i.report.timings.linkCheckMs/1000).toFixed(1)}초` : ''} · JEV ${(i.report.timings.jevMs/1000).toFixed(1)}초</p>` : ''}
        ${i.stale ? `<p class="risk">재평가 필요: ${esc((i.staleReasons || []).join(' · ') || '현재 기준과 다름')}</p>` : ''}${i.report ? `<p class="quality-heading">${mode === 'ai_bot' ? 'AI Bot 품질 분석 · IF는 Bot 지침과 케이스 기대 결과 기준' : '모델 품질 분석 · IF는 사용자 요청과 등록 기준'}</p>` : ''}${reportDetails(i.report, mode === 'ai_bot')}${botDetails(i.report?.botAssessment)}${i.scores&&!i.report?`<details><summary>기존 점수 (비교 제외)</summary><pre>${esc(JSON.stringify(i.scores,null,2))}</pre></details>`:''}</td></tr>`;
    }).join('');
    return `<tr class="case-row"><td><button class="case-toggle" data-case-toggle="${caseId}" aria-expanded="${open}" aria-controls="case-details-${caseId}"><span class="case-chevron" aria-hidden="true">▸</span><strong>${esc(first.title)}</strong></button><p>${mode === 'ai_bot' ? `${esc(first.botName)} · 지침 v${first.botVersion} · ${first.testType === 'exception' ? '예외 테스트' : '정상 테스트'} · ` : ''}${esc(first.category)} · 답변 ${responses.length}개</p><small class="case-prompt">${esc(first.prompt)}</small></td>
      <td>${esc(status)}</td><td class="case-actions"><button data-evaluate-case="${caseId}">변경분 평가</button><button data-force-case="${caseId}">강제 전체 재평가</button><button data-settings="${caseId}">공통 설정</button>${mode === 'ai_bot' && first.botCurrentVersion > first.botVersion ? `<button data-update-bot-version="${caseId}">최신 지침 적용 (v${first.botCurrentVersion})</button>` : ''}</td></tr>
      <tr id="case-details-${caseId}" class="case-details" ${open?'':'hidden'}><td colspan="3">${mode === 'ai_bot' ? `<div class="scenario-summary"><strong>테스트 시나리오</strong> ${esc(first.title)}${first.inputExample ? `<p><b>입력 예시</b> ${esc(first.inputExample)}</p>` : ''}<p><b>점검 관점</b> ${esc(first.checkFocus || '미입력')}</p><p><b>기대 결과</b> ${esc(first.expectedBehavior || '미입력')}</p><p><b>평가 입력</b> ${esc(first.prompt)}</p></div>` : ''}<div class="case-detail-wrap"><table class="model-results"><thead><tr><th>모델</th><th>상태</th>${Object.keys(config.criteria).map(k=>`<th>${esc(config.criteria[k])}</th>`).join('')}<th>작업</th></tr></thead><tbody>${modelRows}</tbody></table></div>${mode === 'ai_bot' ? `<details class="bot-instructions"><summary>적용된 Bot 지침 v${first.botVersion}</summary><pre>${esc(first.botInstructions)}</pre></details>` : ''}</td></tr>`;
  }).join('');
}
async function loadResults() {
  items=(await api(`/api/results?mode=${mode}`)).items;
  const selected=$('#category-filter').value;
  $('#category-filter').innerHTML='<option value="">모든 유형</option>'+[...new Set(items.map(i=>i.category))].map(c=>`<option>${esc(c)}</option>`).join('');
  $('#category-filter').value=selected; render();
}
async function loadBots() {
  bots=(await api('/api/bots')).items;
  const selected=$('#bot-select').value;
  $('#bot-select').innerHTML='<option value="">Bot 선택</option>'+bots.filter(b=>!b.archived).map(b=>`<option value="${b.id}">${esc(b.name)} · v${b.version}</option>`).join('');
  $('#bot-select').value=selected;
  $('#bot-filter').innerHTML='<option value="">모든 Bot</option>'+bots.map(b=>`<option value="${b.id}">${esc(b.name)}</option>`).join('');
  $('#bot-list').innerHTML=bots.map(b=>`<article class="panel bot-card"><div><h3>${esc(b.name)} · v${b.version}${b.archived ? ' · 보관됨' : ''}</h3><p>${esc(b.description)}</p><small>연결된 케이스 ${b.caseCount}개 · 수정 ${esc(b.updatedAt)}</small><details><summary>지침 보기</summary><pre>${esc(b.instructions)}</pre></details></div><div class="actions"><button data-edit-bot="${b.id}">수정</button><button data-archive-bot="${b.id}">${b.archived ? '보관 해제' : '보관'}</button></div></article>`).join('') || '<p class="empty">등록된 AI Bot이 없습니다.</p>';
  updateBotPreview();
}
function updateBotPreview() {
  const bot=bots.find(b=>String(b.id)===$('#bot-select').value);
  $('#bot-instruction-preview').textContent=bot?`적용 지침 v${bot.version}: ${bot.instructions}`:'Bot을 먼저 선택하세요.';
}
function showView(view) {
  document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id===`${view}-view`));
  document.querySelectorAll('[data-view]').forEach(t=>t.classList.toggle('active',t.dataset.view===view));
}
async function switchMode(nextMode) {
  mode=nextMode;
  $('#input-mode-heading').textContent=mode==='ai_bot'?'AI Bot 평가 질문':'모델 평가 질문';
  $('#results-mode-heading').textContent=mode==='ai_bot'?'AI Bot 평가 결과 분석':'모델 평가 결과 분석';
  document.querySelectorAll('[data-mode]').forEach(t=>t.classList.toggle('active',t.dataset.mode===mode));
  document.querySelectorAll('.bot-only').forEach(el=>el.hidden=mode!=='ai_bot');
  const slot=$('#bot-select-slot');
  if (mode==='ai_bot') {
    if (!slot.querySelector('#bot-select')) {
      slot.append($('#bot-select-template').content.cloneNode(true));
      $('#bot-select').onchange=updateBotPreview;
      slot.querySelector('[name=testType]').addEventListener('change',()=>updateExpectedRequired($('#case-form')));
    }
    slot.hidden=false;
    updateExpectedRequired($('#case-form'));
  } else {
    slot.replaceChildren();
    slot.hidden=true;
  }
  $('#export-link').href=`/api/export.csv?mode=${mode}`;
  $('#case-form [name=title]').closest('label').firstChild.textContent=mode==='ai_bot'?'테스트 시나리오':'케이스 제목';
  $('#case-form [name=prompt]').closest('label').firstChild.textContent=mode==='ai_bot'?'실제 입력값':'사용자 질문';
  $('#category-filter').value='';
  if (mode==='ai_bot') await loadBots();
  showView('input');
}
function openSettings(caseId) {
  editingCase=items.find(i=>i.caseId===caseId);
  const select=$('#settings-form [name=category]');
  select.innerHTML='<option value="">유형 선택</option>'+config.categories.map(c=>`<option>${esc(c)}</option>`).join('');
  select.value=editingCase.category;
  const botFields=$('#edit-bot-scenario');
  botFields.hidden=mode!=='ai_bot';
  if (mode==='ai_bot') {
    for (const key of ['testType','inputExample','checkFocus','expectedBehavior']) botFields.querySelector(`[name=${key}]`).value=editingCase[key] || (key==='testType'?'normal':'');
    updateExpectedRequired($('#settings-form'));
  }
  fillSpec('edit',editingCase.evaluationSpec); $('#settings-dialog').showModal();
}
function updateExpectedRequired(form) {
  const field=form.querySelector('[name=expectedBehavior]');
  if (field) field.required=true;
}
document.addEventListener('click', async event=>{
  const button=event.target.closest('button'); if (!button) return;
  try {
    if (button.dataset.mode) await switchMode(button.dataset.mode);
    if (button.dataset.editBot) {
      const bot=bots.find(b=>b.id===Number(button.dataset.editBot));
      const form=$('#bot-form');
      form.elements.botId.value=bot.id;
      form.elements.name.value=bot.name;
      form.elements.description.value=bot.description;
      form.elements.instructions.value=bot.instructions;
      form.scrollIntoView({behavior:'smooth'});
    }
    if (button.dataset.archiveBot) {
      const bot=bots.find(b=>b.id===Number(button.dataset.archiveBot));
      await api(`/api/bots/${bot.id}`,{archived:!bot.archived});
      await loadBots();
    }
    if (button.id==='cancel-bot-edit') $('#bot-form').reset();
    if (button.dataset.updateBotVersion) {
      await api(`/api/cases/${button.dataset.updateBotVersion}/bot-version`,{});
      await loadResults();
      notify('최신 지침을 적용했습니다. 다시 평가해 주세요.');
    }
    if (button.dataset.caseToggle) {
      const caseId=Number(button.dataset.caseToggle);
      const open=!expandedCases.has(caseId);
      if (open) expandedCases.add(caseId); else expandedCases.delete(caseId);
      button.setAttribute('aria-expanded',String(open));
      $(`#case-details-${caseId}`).hidden=!open;
    }
    if (button.dataset.view) {
      showView(button.dataset.view);
      if (button.dataset.view==='results') await loadResults();
    }
    if (button.dataset.settings) openSettings(Number(button.dataset.settings));
    if (button.dataset.evaluateCase || button.dataset.forceCase) {
      const force=Boolean(button.dataset.forceCase);
      const caseId=Number(button.dataset.evaluateCase || button.dataset.forceCase);
      const responses=items.filter(i=>i.caseId===caseId && (force || !i.evaluationId || i.stale));
      if (!responses.length) {notify('변경된 답변이 없습니다.');return;}
      expandedCases.add(caseId);
      button.disabled=true;
      let completed=0;
      let finished=0;
      let nextIndex=0;
      const errors=[];
      const workers=Array.from({length:Math.min(3,responses.length)},async()=>{
        while (nextIndex < responses.length) {
          const response=responses[nextIndex++];
          try {
            await api(`/api/responses/${response.responseId}/evaluate`,{force});
            completed++;
          } catch(error) {
            errors.push(`${response.model}: ${error.message}`);
          } finally {
            finished++;
            button.textContent=`평가 중 ${finished}/${responses.length}`;
          }
        }
      });
      await Promise.all(workers);
      notify(errors.length?`${completed}/${responses.length}개 완료 · ${errors.join(' / ')}`:`${completed}개 답변의 평가를 저장했습니다.`);
      await loadResults();
    }
    if (button.dataset.evaluate) {
      button.disabled=true; const result=await api(`/api/responses/${button.dataset.evaluate}/evaluate`,{}); await loadResults(); notify(result.cached?'입력이 같아 기존 평가를 사용했습니다.':'평가를 저장했습니다. 근거와 검증 범위를 확인하세요.');
    }
    if (button.dataset.forceEvaluate) {
      button.disabled=true; await api(`/api/responses/${button.dataset.forceEvaluate}/evaluate`,{force:true}); await loadResults(); notify('강제 재평가를 저장했습니다.');
    }
    if (button.dataset.history) {
      const data=await api(`/api/responses/${button.dataset.history}/history`);
      $('#history-content').innerHTML=data.items.map(i=>`<article><h3>${esc(i.createdAt)} · ${esc(i.rubricVersion)}</h3><p>${esc(i.model)}</p>${i.report?`${i.report.botInstructions ? `<details><summary>적용 지침 · ${esc(i.report.botName)} v${i.report.botVersion}</summary><pre>${esc(i.report.botInstructions)}</pre></details>` : ''}<p class="quality-heading">${i.report.botAssessment ? 'AI Bot 품질 분석' : '모델 품질 분석'}</p>${reportDetails(i.report, !!i.report.botAssessment)}${botDetails(i.report.botAssessment)}`:`<pre>${esc(JSON.stringify(i.scores,null,2))}</pre>`}</article>`).join('') || '<p>평가 이력이 없습니다.</p>';
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
    const files=[...event.currentTarget.querySelectorAll('[data-artifact-model]')].filter(input=>input.files.length)
      .map(input=>({model:input.dataset.artifactModel,file:input.files[0]}));
    const created=await api('/api/cases',{...payload,responses,evaluationSpec:readSpec('new'),evaluationMode:mode,
      ...(mode==='ai_bot'?{botId:Number($('#bot-select').value),
        testType:data.get('testType'), inputExample:data.get('inputExample'),
        checkFocus:data.get('checkFocus'), expectedBehavior:data.get('expectedBehavior')}: {})});
    const uploadErrors=[];
    for (const entry of files) {
      const saved=created.responses.find(response=>response.model===entry.model);
      if (!saved) {uploadErrors.push(`${entry.model}: 답변이 없어 파일을 연결할 수 없음`);continue;}
      try {await uploadArtifact(saved.id,entry.file);} catch(error) {uploadErrors.push(`${entry.model}: ${error.message}`);}
    }
    notify(uploadErrors.length?`케이스는 저장됐습니다. 파일 업로드 실패: ${uploadErrors.join(' / ')}. 결과 화면에서 다시 첨부하세요.`:'저장했습니다. 결과 화면에서 평가할 수 있습니다.');
    event.target.reset();
    updateExpectedRequired($('#case-form'));
  } catch(error) { notify(error.message); } finally { button.disabled=false; }
});
$('#bot-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.currentTarget;
  const payload={name:form.elements.name.value,description:form.elements.description.value,
    instructions:form.elements.instructions.value};
  const botId=form.elements.botId.value;
  try {
    await api(botId?`/api/bots/${botId}`:'/api/bots',payload);
    form.reset();
    await loadBots();
    notify(botId?'Bot 새 버전을 저장했습니다.':'Bot을 만들었습니다.');
  } catch(error) { notify(error.message); }
});
$('#settings-form').addEventListener('submit',async event=>{
  event.preventDefault(); const button=event.submitter; button.disabled=true;
  try {
    const form=$('#settings-form');
    await api(`/api/cases/${editingCase.caseId}/settings`,{category:form.elements.category.value,evaluationSpec:readSpec('edit'),
      ...(mode==='ai_bot'?Object.fromEntries(['testType','inputExample','checkFocus','expectedBehavior'].map(key=>[key,form.elements[key].value])):{})});
    $('#settings-dialog').close(); await loadResults(); notify('모든 모델에 적용할 공통 설정을 저장했습니다.');
  } catch(error) { notify(error.message); } finally { button.disabled=false; }
});
$('#close-settings').onclick=()=>$('#settings-dialog').close();
$('#settings-form [name=testType]').addEventListener('change',()=>updateExpectedRequired($('#settings-form')));
document.addEventListener('change',async event=>{
  const input=event.target.closest('[data-upload-for]');
  if (!input || !input.files.length) return;
  try {await uploadArtifact(Number(input.dataset.uploadFor),input.files[0]);await loadResults();notify('생성 파일을 저장했습니다. 현재 기준으로 다시 평가해 주세요.');}
  catch(error) {notify(error.message);input.value='';}
});
$('#close-history').onclick=()=>$('#history-dialog').close();
$('#category-filter').onchange=render;
$('#bot-filter').onchange=render;
$('#evaluate-all').onclick=async event=>{
  const button=event.target;
  const category=$('#category-filter').value;
  const botId=$('#bot-filter').value;
  const pending=items.filter(i=>!i.evaluationId && (!category || i.category===category) &&
    (mode!=='ai_bot' || !botId || String(i.botId)===botId));
  if (!pending.length) { notify('선택한 범위에 미평가 답변이 없습니다.'); return; }
  button.disabled=true;
  let nextIndex=0, finished=0, completed=0;
  const errors=[];
  try {
    await Promise.all(Array.from({length:Math.min(3,pending.length)},async()=>{
      while (nextIndex<pending.length) {
        const response=pending[nextIndex++];
        try { await api(`/api/responses/${response.responseId}/evaluate`,{}); completed++; }
        catch(error) { errors.push(`${response.model}: ${error.message}`); }
        finally { finished++; button.textContent=`평가 중 ${finished}/${pending.length}`; }
      }
    }));
    await loadResults();
    notify(errors.length?`${completed}/${pending.length}개 완료 · ${errors.join(' / ')}`:`${completed}개 답변의 평가를 저장했습니다.`);
  } catch(error) { notify(error.message); }
  finally { button.textContent='미평가 모두 실행'; button.disabled=false; }
};
(async()=>{
  try {
    config=await api('/api/config');
    $('#new-spec').innerHTML=specFields('new'); $('#edit-spec').innerHTML=specFields('edit');
    $('#response-fields').innerHTML=config.models.map((m,n)=>`<div class="response-card"><label for="response-${n}"><span>${esc(m)}</span></label><textarea id="response-${n}" name="response:${esc(m)}" rows="8"></textarea><label class="artifact-input" for="artifact-${n}">생성 파일 <span>선택 · HTML, PDF, XLSX · 최대 10MB</span><input id="artifact-${n}" type="file" data-artifact-model="${esc(m)}" accept=".html,.htm,.pdf,.xlsx"></label></div>`).join('');
    $('#api-status').textContent=config.apiKeyConfigured?`${config.evaluatorModel} · ${config.rubricVersion}`:'API 키 필요';
  } catch(error) {notify(error.message);}
})();
