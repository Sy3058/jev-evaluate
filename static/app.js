const labels = {
  instruction_following: "지시 준수",
  relevance: "관련성",
  completeness: "완전성",
  apparent_correctness: "표면 정확성",
  clarity_style: "명확성·스타일",
  overall_quality: "종합 품질",
  unsupported_claim_risk: "근거 없는 단정 위험",
  fabrication_risk: "조작 위험",
  critical_failure: "치명적 실패",
};
const evidenceStatusLabels = {
  SUPPORTED: "근거 있음",
  CONTRADICTED: "근거와 충돌",
  NOT_ENOUGH_EVIDENCE: "근거 부족",
};

let config = null;
let resultItems = [];
const toast = document.querySelector("#toast");

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function notify(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "요청에 실패했습니다.");
  return body;
}

function switchView(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `${name}-view`));
  if (name === "results") loadResults();
}

function setupModelFields() {
  document.querySelector("#response-fields").innerHTML = config.models.map((model) => `
    <label class="response-card">
      <span class="model-chip">${escapeHtml(model)}</span>
      <textarea name="response:${escapeHtml(model)}" placeholder="${escapeHtml(model)}의 답변을 붙여넣으세요."></textarea>
    </label>`).join("");
}

async function saveCase(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  button.disabled = true;
  const data = new FormData(form);
  const responses = Object.fromEntries(config.models.map((model) => [model, data.get(`response:${model}`)]));
  try {
    const created = await api("/api/cases", {
      method: "POST",
      body: JSON.stringify({
        title: data.get("title"), category: data.get("category"), prompt: data.get("prompt"),
        conversationHistory: data.get("conversationHistory"), attachmentText: data.get("attachmentText"), responses,
      }),
    });
    form.reset();
    notify(`${created.responses.length}개 모델 답변을 저장했습니다.`);
    switchView("results");
  } catch (error) {
    notify(error.message);
  } finally {
    button.disabled = false;
  }
}

function mean(values) {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

function renderSummary(items) {
  const evaluated = items.filter((item) => item.scores);
  const cards = config.models.map((model) => {
    const own = evaluated.filter((item) => item.model === model);
    return {
      model, score: mean(own.map((item) => item.scores.overall_quality)), count: own.length,
      instruction: mean(own.map((item) => item.scores.instruction_following)),
      correctness: mean(own.map((item) => item.scores.apparent_correctness)),
      clarity: mean(own.map((item) => item.scores.clarity_style)),
    };
  }).filter((item) => item.count).sort((a, b) => b.score - a.score);
  document.querySelector("#summary").innerHTML = cards.map((item, index) => `
    <article class="summary-card"><span class="model">${index + 1}. ${escapeHtml(item.model)}</span><strong>${item.score.toFixed(1)}</strong><small>종합 품질 · ${item.count}개 평가</small><div class="metric-list"><span>지시 준수</span><b>${item.instruction.toFixed(1)}</b><span>표면 정확성</span><b>${item.correctness.toFixed(1)}</b><span>명확성·스타일</span><b>${item.clarity.toFixed(1)}</b></div></article>
  `).join("");
}

function renderRows() {
  const category = document.querySelector("#category-filter").value;
  const items = category ? resultItems.filter((item) => item.category === category) : resultItems;
  renderSummary(items);
  document.querySelector("#empty-state").style.display = items.length ? "none" : "block";
  document.querySelector("#result-rows").innerHTML = items.map((item) => {
    const scores = item.scores;
    const risk = scores ? Math.max(scores.unsupported_claim_risk, scores.fabrication_risk, scores.critical_failure) : null;
    const evidence = item.evidence;
    const citedClaims = evidence ? evidence.claims.filter((claim) => claim.evidenceId !== "NONE" && claim.evidenceText) : [];
    const evidenceDetails = citedClaims.length ? `<details class="evidence-details"><summary>연결된 근거 ${citedClaims.length}개 보기</summary><div class="claim-list">${citedClaims.map((claim) => `
      <article class="claim-card ${claim.status.toLowerCase()}">
        <div class="claim-head"><b>${escapeHtml(claim.answerId)}</b><span>${escapeHtml(evidenceStatusLabels[claim.status] || claim.status)}</span>${typeof claim.confidence === "number" ? `<small>판정 신뢰도 ${(claim.confidence * 100).toFixed(1)}</small>` : ""}</div>
        <p>${escapeHtml(claim.answerText)}</p>
        <div class="evidence-quote"><b>${escapeHtml(claim.evidenceId)}</b><span>${escapeHtml(claim.evidenceText)}</span></div>
      </article>`).join("")}</div></details>` : "";
    return `<tr>
      <td><strong>${escapeHtml(item.title)}</strong><br><small>${escapeHtml(item.category)}</small>${scores ? `<details class="score-details"><summary>전체 기준 보기</summary><dl>${Object.entries(scores).map(([key, value]) => `<dt>${escapeHtml(labels[key] || key)}</dt><dd>${value.toFixed(1)}</dd>`).join("")}</dl></details>` : ""}${evidenceDetails}</td>
      <td class="model">${escapeHtml(item.model)}</td>
      <td><span class="badge ${scores ? "done" : ""}">${scores ? "평가 완료" : "미평가"}</span></td>
      <td class="score">${scores ? scores.overall_quality.toFixed(1) : "-"}</td>
      <td>${scores ? scores.apparent_correctness.toFixed(1) : "-"}</td>
      <td>${scores ? scores.instruction_following.toFixed(1) : "-"}</td>
      <td class="risk">${risk === null ? "-" : risk.toFixed(1)}</td>
      <td><button class="evaluate" data-id="${item.responseId}">${scores ? "다시 평가" : "JEV 평가"}</button></td>
    </tr>`;
  }).join("");
}

async function loadResults() {
  try {
    const data = await api("/api/results");
    resultItems = data.items;
    const filter = document.querySelector("#category-filter");
    const selected = filter.value;
    const categories = [...new Set(resultItems.map((item) => item.category))];
    filter.innerHTML = '<option value="">모든 유형</option>' + categories.map((value) => `<option>${escapeHtml(value)}</option>`).join("");
    filter.value = selected;
    renderRows();
  } catch (error) {
    notify(error.message);
  }
}

async function evaluate(event) {
  const button = event.target.closest(".evaluate");
  if (!button) return;
  button.disabled = true;
  button.textContent = "평가 중...";
  try {
    await api(`/api/responses/${button.dataset.id}/evaluate`, { method: "POST", body: "{}" });
    notify("JEV 평가를 저장했습니다.");
    await loadResults();
  } catch (error) {
    notify(error.message);
    button.disabled = false;
    button.textContent = "JEV 평가";
  }
}

async function evaluateAll() {
  const button = document.querySelector("#evaluate-all");
  button.disabled = true;
  button.textContent = "일괄 평가 중...";
  try {
    const result = await api("/api/evaluate-pending", { method: "POST", body: "{}" });
    notify(`${result.completed}개 평가 완료${result.errors.length ? ` · 오류: ${result.errors[0].error}` : ""}`);
    await loadResults();
  } catch (error) {
    notify(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "미평가 모두 실행";
  }
}

async function init() {
  try {
    config = await api("/api/config");
    setupModelFields();
    const status = document.querySelector("#api-status");
    status.textContent = config.apiKeyConfigured ? `${config.evaluatorModel} 준비됨` : "API 키 필요";
    status.classList.add(config.apiKeyConfigured ? "ready" : "error");
  } catch (error) {
    notify(error.message);
  }
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => switchView(tab.dataset.view)));
document.querySelector("#case-form").addEventListener("submit", saveCase);
document.querySelector("#result-rows").addEventListener("click", evaluate);
document.querySelector("#category-filter").addEventListener("change", renderRows);
document.querySelector("#evaluate-all").addEventListener("click", evaluateAll);
init();
