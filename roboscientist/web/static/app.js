/* The console deliberately renders evidence defensively: the API can add fields
 * without making an old campaign impossible to inspect. */
const form = document.querySelector('#task-form');
const runButton = document.querySelector('#run-button');
const campaignButton = document.querySelector('#campaign-button');
const iterateButton = document.querySelector('#iterate-button');
const resetButton = document.querySelector('#reset-button');
const stopButton = document.querySelector('#stop-button');
const state = { history: [], campaign: null, config: null };

const byId = (id) => document.querySelector(`#${id}`);
const pretty = (value) => JSON.stringify(value ?? {}, null, 2);
const last = (items) => items?.[items.length - 1] || null;
const finite = (value) => typeof value === 'number' && Number.isFinite(value);
const metric = (value, digits = 3) => finite(value) ? value.toFixed(digits) : '-';
const metricWithUnit = (value, unit, digits = 3) => finite(value) ? `${value.toFixed(digits)} ${unit}` : '-';
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
})[character]);

function pick(object, paths, fallback = null) {
  for (const path of paths) {
    const parts = path.split('.');
    let value = object;
    for (const part of parts) value = value?.[part];
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return fallback;
}

function selectedScenario() {
  return document.querySelector('input[name="scenario"]:checked')?.value || 'pose_offset';
}

function selectedMode() {
  return document.querySelector('input[name="mode"]:checked')?.value || 'mock';
}

function statusLabel(status) {
  return {
    succeeded: '成功', failed: '失败', timed_out: '超时', rejected: '已拒绝',
    blocked: '已拦截', pending: '待处理', accepted: '已接受', stopped: '已停止',
  }[status] || status || '未知';
}

function statusClass(status) {
  return `status-${String(status || 'idle').replace(/[^a-z0-9_-]/gi, '-')}`;
}

function modeLabel(source) {
  return {
    mock: ['MOCK · 流程验证', 'source-mock', '流程 Mock 仅验证软件闭环，不代表仿真或实机实验结果。'],
    simulation: ['SIMULATION · 仿真证据', 'source-simulation', '虚拟仿真结果；除非有明确运行证据，不代表真实机械臂验证。'],
    real_arm: ['REAL ARM · 实机证据', 'source-real', '真实机械臂结果；自主闭环必须由视觉评价和完整日志证明。'],
  }[source] || [String(source || 'UNKNOWN').toUpperCase(), 'source-unknown', '数据来源未分类。'];
}

function classificationLabel(value, source = '') {
  const normalized = String(value || '').toLowerCase();
  const labels = {
    autonomous_vision_closed_loop: '自主视觉闭环',
    autonomous: '自主闭环',
    autonomous_closed_loop: '自主闭环',
    real_arm_autonomous: '实机自主闭环',
    supervised: '受监督闭环',
    supervised_operator: '受监督：人工结果判定',
    supervised_or_incomplete: '受监督或闭环未完成',
    manual_review_required: '需人工复核',
    failed_before_closed_loop: 'Qwen 调用失败，未完成闭环',
    failed_runtime: '运行失败，未完成闭环',
    deterministic_only: '确定性流程（未调用 Qwen）',
    simulation: '仿真闭环（非实机）',
    mock: 'Mock 流程（非实验）',
  };
  if (labels[normalized]) {
    if (source === 'mock' && normalized.includes('autonomous')) return 'Mock 自动流程（非实机自主证据）';
    if (source === 'simulation' && normalized.includes('autonomous')) return '仿真自动流程（非实机证据）';
    return labels[normalized];
  }
  if (source === 'mock') return 'Mock 流程（非实验）';
  if (source === 'simulation') return '仿真执行（非实机）';
  return value || '未分类';
}

function setBusy(isBusy, label = '正在运行...') {
  runButton.disabled = isBusy;
  campaignButton.disabled = isBusy;
  runButton.textContent = isBusy ? label : '仅运行 P0（诊断）';
  iterateButton.disabled = isBusy || !candidateUnderTest();
  resetButton.disabled = isBusy || !candidateUnderTest();
}

async function requestStop() {
  if (!stopButton || stopButton.disabled) return;
  stopButton.disabled = true;
  const previous = stopButton.textContent;
  stopButton.textContent = '停止请求中...';
  try {
    const response = await request('/api/stop', {
      // A stop request must target the physical adapter even if the mode radio
      // was changed while an experiment was running.
      mode: 'real_arm',
      reason: 'web_operator_request',
    });
    const result = response.stop || {};
    const stopped = result.status === 'succeeded' || result.stopped === true;
    byId('system-state').textContent = stopped ? '已请求停止，等待人工检查' : '停止请求已返回，请立即人工检查';
    byId('current-evidence').innerHTML = `<span class="field-label">人工接管 · ${escapeHtml(stopped ? '已确认' : '需检查')}</span><p>${escapeHtml(result.message || '停止结果未提供详细信息。')}</p>`;
  } catch (error) {
    byId('system-state').textContent = '停止请求失败，立即执行物理急停';
    byId('current-evidence').innerHTML = `<span class="field-label">人工接管 · 失败</span><p>${escapeHtml(error.message)}。请立即执行现场物理急停。</p>`;
  } finally {
    stopButton.disabled = false;
    stopButton.textContent = previous;
  }
}

function candidateUnderTest() {
  return state.history[0]?.candidate_skill || state.campaign?.candidate_skill || null;
}

function syncConditionControls() {
  const locked = Boolean(candidateUnderTest());
  const qwenConfig = state.config?.qwen || state.config || {};
  document.querySelectorAll('input[name="mode"], input[name="scenario"]').forEach((input) => { input.disabled = locked; });
  byId('scenario-fieldset').classList.toggle('baseline-locked', locked);
  byId('task-form').classList.toggle('baseline-locked', locked);
  byId('use-qwen').disabled = locked || !(qwenConfig.configured === true || qwenConfig.enabled === true);
  resetButton.disabled = !locked;
}

function recordResult(record) {
  return record?.result || record?.execution_result || {};
}

function recordPlan(record) {
  return record?.plan || record?.experiment_plan || {};
}

function sourceOf(record) {
  return recordResult(record).data_source || recordResult(record).source || recordPlan(record).expected_data_source || selectedMode();
}

function evaluatorOf(record) {
  const result = recordResult(record);
  return result.evaluator_type || result.artifacts?.evaluator_type || result.evaluation?.evaluator_type || '-';
}

function qwenSection(phase) {
  const qwen = state.campaign?.qwen || state.campaign?.qwen_evidence || {};
  const keys = phase === 'planning'
    ? ['planning', 'plan', 'planning_invocation', 'planning_evidence']
    : ['feedback', 'adjustment', 'feedback_invocation', 'adjustment_evidence'];
  for (const key of keys) if (qwen[key]) return qwen[key];
  const direct = phase === 'planning'
    ? ['planning_qwen', 'qwen_planning']
    : ['feedback_qwen', 'qwen_feedback', 'adjustment_qwen'];
  for (const key of direct) if (state.campaign?.[key]) return state.campaign[key];
  const candidateRecord = phase === 'planning' ? state.history[0] : last(state.history);
  const plan = recordPlan(candidateRecord);
  return plan.qwen || plan.qwen_evidence || null;
}

function qwenMeta(value) {
  const metadata = value?.metadata || value?.meta || value || {};
  return {
    model: pick(value, ['model', 'metadata.model', 'meta.model'], '-'),
    requestId: pick(value, ['request_id', 'requestId', 'metadata.request_id', 'meta.request_id'], '-'),
    promptHash: pick(value, ['prompt_sha256', 'promptSha256', 'metadata.prompt_sha256', 'meta.prompt_sha256'], '-'),
    called: Boolean(value && (value.called || value.metadata || value.meta || value.model || value.request_id || value.requestId || metadata.schema_valid)),
    error: pick(value, ['error', 'metadata.error', 'meta.error'], null),
  };
}

function renderAuditCard(id, value) {
  const card = byId(id);
  const meta = qwenMeta(value);
  const stateText = meta.error ? '调用失败' : meta.called ? '已记录' : '未调用';
  const stateClassName = meta.error ? 'is-error' : meta.called ? 'is-recorded' : '';
  card.querySelector('.audit-state').textContent = stateText;
  card.querySelector('.audit-state').className = `audit-state ${stateClassName}`;
  const values = card.querySelectorAll('dd');
  values[0].textContent = meta.model;
  values[1].textContent = meta.requestId;
  values[2].textContent = meta.promptHash;
  card.classList.toggle('has-evidence', meta.called);
  card.classList.toggle('has-error', Boolean(meta.error));
}

function evidenceText(record) {
  const result = recordResult(record);
  const analysis = record?.analysis || result.failure_analysis;
  if (result.hardware_status === 'motion_completed_result_unverified') return '动作链已完成；抓取、提起和放置尚无可验证结果证据。';
  if (result.failure) return `${result.failure.stage || '执行'}：${result.failure.message || result.failure.code || '失败'}`;
  if (analysis?.evidence?.length) return analysis.evidence.join(' / ');
  if (result.status === 'succeeded' && sourceOf(record) === 'real_arm') {
    if (evaluatorOf(record) === 'vision') return '视觉评价已确认抓取、提起与放置。';
    if (evaluatorOf(record) === 'operator' || evaluatorOf(record) === 'hybrid') return '结果由现场人员或混合方式确认；本轮不算自主视觉判定。';
    return '结果状态成功，但评价器证据不足。';
  }
  if (result.status === 'succeeded' && sourceOf(record) === 'mock') return '流程 Mock 已完成；该结论不代表物理抓取成功。';
  if (result.status === 'succeeded' && sourceOf(record) === 'simulation') return '仿真任务完成；该结论不代表真实机械臂成功。';
  return result.outcome ? '已产生结果，但尚无结构化失败归因。' : '未产生可用实验结果。';
}

function decisionInfo() {
  const raw = state.campaign?.decision || state.campaign?.feedback_decision || state.campaign?.decision_status || null;
  if (typeof raw === 'string') return { code: raw.toLowerCase(), reason: '' };
  if (raw && typeof raw === 'object') return {
    code: String(raw.status || raw.decision || raw.outcome || raw.code || '').toLowerCase(),
    reason: raw.reason || raw.message || raw.explanation || '',
  };
  const adjustment = state.campaign?.feedback_adjustment || state.campaign?.adjustment;
  if (adjustment?.strategy === 'stop_for_human') return { code: 'stop_for_human', reason: adjustment.alternative_explanation || '' };
  if (!state.history.length) return { code: 'pending', reason: '先运行 P0，系统才会产生可审计证据。' };
  if (!candidateUnderTest()) return { code: 'rejected', reason: '没有允许的单参数候选，系统不会伪造 P1。' };
  if (state.history.length < 2) return { code: 'candidate_only', reason: '候选已生成，等待固定条件验证。' };
  return { code: 'candidate_only', reason: 'P1 已运行，但重复样本不足，尚不能晋升。' };
}

function decisionLabel(code) {
  const value = String(code || '').toLowerCase();
  if (value.includes('stop') || value.includes('human') || value.includes('awaiting_confirmation')) return ['STOP', '转人工 / 停止自动运行', 'decision-stop'];
  if (value.includes('reject') || value.includes('mismatch') || value.includes('block')) return ['REJECT', '反馈候选被拒绝或拦截', 'decision-reject'];
  if (value.includes('accept') || value.includes('promot')) return ['ACCEPT', '反馈策略已接受', 'decision-accept'];
  if (value.includes('p1_executed')) return ['ACCEPT', '反馈策略已接受，P1 已执行（候选未晋升）', 'decision-accept'];
  if (value.includes('candidate')) return ['CANDIDATE', '候选已生成，尚未晋升', 'decision-candidate'];
  return ['WAIT', '反馈决策待定', 'decision-pending'];
}

function updateSource(record) {
  const source = sourceOf(record);
  const [label, className, notice] = modeLabel(source);
  const badge = byId('mode-label');
  badge.textContent = label;
  badge.className = `source-badge ${className}`;
  byId('mode-notice').textContent = state.campaign?.mode_notice || notice;
}

function updateFacts(record) {
  const result = recordResult(record);
  const plan = recordPlan(record);
  const source = sourceOf(record);
  const safety = result.safety_check || result.safety || {};
  const classification = pick(state.campaign, ['autonomy_classification', 'classification', 'execution_classification'], null)
    || (state.campaign?.planning_mode === 'deterministic_only' ? 'deterministic_only' : null);
  const facts = document.querySelectorAll('#run-facts dd');
  [
    pick(state.campaign, ['campaign_id', 'id'], pick(record, ['campaign_id'], '-')),
    classificationLabel(classification, source),
    modeLabel(source)[0],
    result.scene_id || plan.scene_id || '-',
    evaluatorOf(record),
    safety.allowed === false ? '拦截' : safety.allowed === true ? '通过' : '-',
  ].forEach((value, index) => { if (facts[index]) facts[index].textContent = value; });
  byId('system-state').textContent = state.history.length ? `已记录 ${state.history.length} 轮实验` : '准备实验';
  const status = result.status || 'idle';
  byId('run-status').textContent = statusLabel(status).toUpperCase();
  byId('run-status').className = `status ${statusClass(status)}`;
  byId('planning-source').textContent = plan.planning_source === 'qwen' || state.campaign?.planning_mode === 'qwen' ? 'Qwen 规划' : state.campaign?.planning_mode === 'deterministic_only' ? '确定性规划' : '等待规划';
  byId('planning-source').className = `status ${statusClass(status === 'idle' ? 'idle' : 'succeeded')}`;
  byId('current-evidence').innerHTML = `<span class="field-label">当前结论 · ${escapeHtml(statusLabel(status))}</span><p>${escapeHtml(evidenceText(record))}</p>`;
  const scientific = state.campaign?.scientific_plan || plan.scientific_plan || plan.scientific_intent || {};
  byId('research-question').textContent = scientific.research_question || '未返回研究问题。';
  byId('hypothesis').textContent = scientific.hypothesis || '未返回可检验假设。';
  byId('plan-output').textContent = pretty({
    task: plan.task, scene_id: plan.scene_id, target_pose: plan.target_pose,
    destination_pose: plan.destination_pose, skill: plan.skill,
    planning_source: plan.planning_source, scientific_plan: scientific,
    feedback_adjustment: plan.feedback_adjustment, safety_check: safety,
  });
  byId('result-output').textContent = pretty({
    status: result.status, outcome: result.outcome, failure: result.failure,
    analysis: record.analysis || result.failure_analysis, actions: result.actions,
    metrics: result.metrics, artifacts: result.artifacts,
  });
  byId('simulation-output').textContent = result.simulation
    ? pretty(result.simulation)
    : '本轮不是已验证的 Gazebo/MoveIt2 仿真执行。';
  byId('campaign-output').textContent = pretty({
    campaign_id: state.campaign?.campaign_id || state.campaign?.id,
    planning_mode: state.campaign?.planning_mode,
    classification: classification,
    decision: state.campaign?.decision || state.campaign?.feedback_decision,
    qwen: state.campaign?.qwen || state.campaign?.qwen_evidence,
  });
  renderAuditCard('planning-evidence', qwenSection('planning'));
  renderAuditCard('feedback-evidence', qwenSection('feedback'));
}

function renderLoop() {
  const baseline = state.history[0];
  const validation = state.history.length > 1 ? last(state.history) : null;
  const scientific = state.campaign?.scientific_plan || recordPlan(baseline).scientific_plan;
  const feedback = state.campaign?.feedback_adjustment || state.campaign?.adjustment || recordPlan(validation).feedback_adjustment;
  const result = recordResult(baseline);
  const decision = decisionInfo();
  const stages = [
    { index: '01', title: 'Qwen 规划', text: scientific ? '研究问题与假设已记录。' : '等待研究问题、假设和成功标准。', state: scientific ? 'is-complete' : 'is-pending' },
    { index: 'P0', title: '安全执行', text: baseline ? `${recordPlan(baseline).skill?.version || 'p0'} · ${statusLabel(result.status)}` : '等待确定性安全检查和基线运行。', state: baseline ? (result.safety_check?.allowed === false ? 'is-alert' : 'is-complete') : 'is-pending' },
    { index: '03', title: '结果评价', text: baseline ? evidenceText(baseline) : '等待结构化结果与失败残差。', state: baseline ? (result.failure || result.hardware_status === 'motion_completed_result_unverified' ? 'is-alert' : 'is-complete') : 'is-pending' },
    { index: '04', title: '反馈决策', text: feedback ? `${feedback.strategy || '策略已记录'} · ${decisionLabel(decision.code)[1]}` : '等待 Qwen 解释结果并作出决策。', state: feedback || decision.code !== 'pending' ? (decision.code.includes('reject') || decision.code.includes('stop') ? 'is-alert' : 'is-complete') : 'is-pending' },
    { index: 'P1', title: '同条件验证', text: validation ? `${recordPlan(validation).skill?.version || 'candidate'} · ${statusLabel(recordResult(validation).status)}` : candidateUnderTest() ? '候选已生成，等待固定条件验证。' : '需先得到允许的单参数候选。', state: validation ? (recordResult(validation).status === 'succeeded' ? 'is-complete' : 'is-alert') : candidateUnderTest() ? 'is-muted' : 'is-pending' },
  ];
  byId('loop-track').innerHTML = stages.map((stage) => `<article class="loop-step ${stage.state}"><span class="step-index">${escapeHtml(stage.index)}</span><h3>${escapeHtml(stage.title)}</h3><p>${escapeHtml(stage.text)}</p></article>`).join('');
  byId('sample-note').textContent = state.history.length ? `已记录 ${state.history.length} 轮 · 每版 10 次为项目内部策略` : '尚未开始';
  renderDecision();
}

function renderDecision() {
  const output = byId('decision-output');
  const info = decisionInfo();
  const [code, title, className] = decisionLabel(info.code);
  output.className = `decision-output ${className}`;
  output.innerHTML = `<span class="decision-code">${escapeHtml(code)}</span><div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(info.reason || '系统保留完整决策证据。')}</p></div>`;
}

function parameterDelta(record) {
  const candidate = record?.candidate_skill;
  const plan = recordPlan(record);
  if (!candidate) return '';
  if (candidate.changed_parameter_family === 'path_profile') {
    return `transit_height: ${metric(plan.skill?.parameters?.transit_height_m)} m → ${metric(candidate.parameters?.transit_height_m)} m`;
  }
  const before = plan.skill?.parameters?.grasp_offset_m || [];
  const after = candidate.parameters?.grasp_offset_m || [];
  return `grasp_offset: [${before.map((item) => metric(item)).join(', ')}] m → [${after.map((item) => metric(item)).join(', ')}] m`;
}

function renderLineage() {
  const list = byId('lineage-list');
  byId('lineage-count').textContent = `${state.history.length} 轮`;
  if (!state.history.length) {
    list.innerHTML = '<li class="empty-lineage">每轮实验追加保存；P1 不覆盖 P0。</li>';
    return;
  }
  list.innerHTML = state.history.map((record, index) => {
    const result = recordResult(record);
    const plan = recordPlan(record);
    const label = index === 0 ? 'P0 · 基线' : `P1 · 候选验证 ${index}`;
    const change = index === 0 && record.candidate_skill ? `<p class="lineage-change">单参数族改动：${escapeHtml(parameterDelta(record))}</p>` : '';
    return `<li class="lineage-item"><div class="lineage-marker">${index === 0 ? 'P0' : 'P1'}</div><div><div class="lineage-topline"><strong>${escapeHtml(label)}</strong><span class="status ${statusClass(result.status)}">${escapeHtml(statusLabel(result.status))}</span></div><p>${escapeHtml(result.experiment_id || '-')} · ${escapeHtml(plan.skill?.version || result.skill_version || '-')} · ${escapeHtml(modeLabel(sourceOf(record))[0])}</p><p>${escapeHtml(evidenceText(record))}</p>${change}</div></li>`;
  }).join('');
}

function canonicalCondition(record) {
  const plan = recordPlan(record);
  const result = recordResult(record);
  const target = plan.target_pose || {};
  const destination = plan.destination_pose || {};
  return {
    data_source: sourceOf(record), scene_id: result.scene_id || plan.scene_id,
    task: plan.task?.source_text || plan.task?.task_id,
    target: [target.x, target.y, target.z, target.frame_id, target.calibration_version],
    destination: [destination.x, destination.y, destination.z, destination.frame_id],
    evaluator: evaluatorOf(record),
  };
}

function sameConditionResult() {
  const explicit = state.campaign?.same_condition || state.campaign?.same_conditions || state.campaign?.condition_check;
  if (explicit && typeof explicit === 'object' && explicit.verified !== undefined) return explicit;
  if (state.history.length < 2) return { verified: false, pending: true, differences: [] };
  const base = canonicalCondition(state.history[0]);
  const candidate = canonicalCondition(last(state.history));
  const differences = Object.keys(base).filter((key) => JSON.stringify(base[key]) !== JSON.stringify(candidate[key]));
  return { verified: differences.length === 0, pending: false, differences };
}

function renderComparison() {
  const baseline = state.history[0];
  const candidateRun = state.history.length > 1 ? last(state.history) : null;
  const output = byId('comparison-output');
  const condition = sameConditionResult();
  const conditionStatus = byId('condition-status');
  conditionStatus.textContent = condition.pending ? '待验证' : condition.verified ? '一致' : '不一致';
  conditionStatus.className = `status ${condition.pending ? 'status-idle' : condition.verified ? 'status-succeeded' : 'status-rejected'}`;
  const differences = (condition.differences || []).map((difference) => typeof difference === 'string' ? difference : difference.field || '未标注字段');
  byId('condition-output').innerHTML = condition.pending
    ? '<p>运行 P1 后检查模式、场景、目标、标定与评价口径是否一致。</p>'
    : `<p class="condition-${condition.verified ? 'ok' : 'bad'}">${condition.verified ? 'P0 与 P1 条件一致，可以比较结果。' : `不能作同条件结论。差异：${escapeHtml(differences.join('、'))}`}</p>`;
  if (!baseline || !candidateRun) {
    output.textContent = '获得 P0 与 P1 后显示结果和运动指标。';
    output.className = 'comparison-empty';
    return;
  }
  const baseMetrics = recordResult(baseline).metrics || {};
  const candidateMetrics = recordResult(candidateRun).metrics || {};
  const baseResult = recordResult(baseline);
  const candidateResult = recordResult(candidateRun);
  const rows = [
    ['实验状态', statusLabel(baseResult.status), statusLabel(candidateResult.status)],
    ['定位误差', metricWithUnit(baseMetrics.position_error_m ?? baseResult.outcome?.position_error_m, 'm'), metricWithUnit(candidateMetrics.position_error_m ?? candidateResult.outcome?.position_error_m, 'm')],
    ['TCP / 仿真路径', metricWithUnit(baseMetrics.path_length_m, 'm'), metricWithUnit(candidateMetrics.path_length_m, 'm')],
    ['执行时间', metricWithUnit(baseMetrics.execution_time_s, 's'), metricWithUnit(candidateMetrics.execution_time_s, 's')],
    ['安全事件', metric(baseMetrics.safety_events, 0), metric(candidateMetrics.safety_events, 0)],
  ];
  output.className = 'comparison-table-wrap';
  output.innerHTML = `<table><thead><tr><th>指标</th><th>P0 基线</th><th>P1 候选</th></tr></thead><tbody>${rows.map((row) => `<tr><th>${escapeHtml(row[0])}</th><td>${escapeHtml(row[1])}</td><td>${escapeHtml(row[2])}</td></tr>`).join('')}</tbody></table><p class="comparison-note">${condition.verified ? '当前为单次同条件链路证据。' : '条件不一致，禁止把这次结果解释为性能提升。'} 候选仍需按项目内部策略重复验证后再晋升。</p>`;
}

function render(record) {
  if (!record) return;
  updateSource(record);
  updateFacts(record);
  renderLoop();
  renderLineage();
  renderComparison();
  iterateButton.disabled = !candidateUnderTest();
  syncConditionControls();
}

function clearExperimentView() {
  byId('research-question').textContent = '运行 Qwen 科研闭环后显示。';
  byId('hypothesis').textContent = '系统必须先记录 P0 结果，再决定是否生成 P1。';
  byId('planning-source').textContent = '等待规划';
  byId('planning-source').className = 'status status-idle';
  byId('run-status').textContent = 'IDLE';
  byId('run-status').className = 'status status-idle';
  byId('current-evidence').innerHTML = '<span class="field-label">当前结论</span><p>尚未运行实验。Mock、仿真与实机结果会保持明确区分。</p>';
  byId('plan-output').textContent = '尚未生成计划。';
  byId('result-output').textContent = '尚未执行。';
  byId('campaign-output').textContent = '尚未运行闭环。';
  byId('simulation-output').textContent = '仅 Simulation 模式显示规划、轨迹与碰撞字段。';
  renderAuditCard('planning-evidence', null);
  renderAuditCard('feedback-evidence', null);
  renderLoop();
  renderLineage();
  renderComparison();
  syncConditionControls();
}

function normaliseCampaign(payload) {
  const campaign = payload?.campaign && typeof payload.campaign === 'object' ? { ...payload.campaign, ...payload } : { ...(payload || {}) };
  const records = Array.isArray(campaign.records) ? campaign.records : [];
  campaign.records = records;
  state.campaign = campaign;
  state.history = records;
  return campaign;
}

function formatError(payload, response) {
  const error = payload?.error;
  if (typeof error === 'object') return error.message || error.detail || `请求失败（${response.status}）`;
  return error || payload?.message || `请求失败（${response.status}）`;
}

async function request(url, body) {
  const options = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(formatError(payload, response));
    error.details = payload?.error || null;
    throw error;
  }
  return payload;
}

function visibleError(error) {
  const campaignId = error?.details?.campaign_id;
  return campaignId ? `${error.message}（Campaign ${campaignId} 已写入失败证据）` : error.message;
}

async function loadCampaignIfNeeded(payload) {
  const campaign = normaliseCampaign(payload);
  const campaignId = campaign.campaign_id || campaign.id;
  if (campaignId && !campaign.records.length) {
    try { return normaliseCampaign(await request(`/api/campaigns/${encodeURIComponent(campaignId)}`)); } catch (error) { throw error; }
  }
  return campaign;
}

async function refreshRuntime() {
  const mode = selectedMode();
  try {
    const runtime = await request(`/api/runtime?mode=${encodeURIComponent(mode)}`);
    const capabilities = mode === 'real_arm' ? [
      ['桥接', runtime.available && runtime._bridge_mode === 'real_motion'],
      ['停止', runtime.stop_available === true],
      ['结果评估', runtime.result_evaluator_configured === true],
      ['候选参数', runtime.parameterized_skill_configured === true],
    ] : [[runtime.hardware_status || runtime.motion_state || mode, mode === 'mock']];
    byId('runtime-capabilities').innerHTML = capabilities.map(([label, ready]) => `<span class="${ready ? 'is-ready' : 'is-blocked'}">${escapeHtml(label)} · ${ready ? '就绪' : '未就绪'}</span>`).join('');
  } catch (error) {
    byId('runtime-capabilities').innerHTML = `<span class="is-blocked">运行时不可用 · ${escapeHtml(error.message)}</span>`;
  }
}

async function refreshConfig() {
  const qwenStatus = byId('qwen-status');
  try {
    const config = await request('/api/config');
    state.config = config;
    const qwen = config.qwen || config;
    const configured = qwen.configured === true || qwen.enabled === true;
    const model = qwen.model || '未配置模型';
    const toggle = byId('use-qwen');
    toggle.disabled = !configured;
    toggle.checked = configured && qwen.enabled !== false;
    byId('qwen-control').classList.toggle('is-unavailable', !configured);
    byId('qwen-control-help').textContent = configured ? `${qwen.provider || '百炼'} · ${model}` : '未配置 DASHSCOPE_API_KEY，闭环不会静默降级';
    qwenStatus.textContent = configured ? `Qwen · ${model}` : 'Qwen · 未配置';
    qwenStatus.className = `service-chip ${configured ? 'is-ready' : 'is-blocked'}`;
  } catch (error) {
    byId('use-qwen').disabled = true;
    byId('use-qwen').checked = false;
    byId('qwen-control-help').textContent = `配置接口不可用：${error.message}`;
    qwenStatus.textContent = 'Qwen · 配置未知';
    qwenStatus.className = 'service-chip is-blocked';
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  state.history = [];
  state.campaign = null;
  clearExperimentView();
  setBusy(true, '正在运行 P0...');
  try {
    const record = await request('/api/tasks', { task_text: byId('task-text').value, scenario: selectedScenario(), mode: selectedMode() });
    state.history = [record];
    render(record);
  } catch (error) {
    const title = byId('result-title');
    if (title) title.textContent = error.message;
    byId('current-evidence').innerHTML = `<span class="field-label">请求失败</span><p>${escapeHtml(visibleError(error))}</p>`;
  } finally { setBusy(false); }
});

campaignButton.addEventListener('click', async () => {
  state.history = [];
  state.campaign = null;
  clearExperimentView();
  setBusy(true, '正在运行科研闭环...');
  try {
    const payload = await request('/api/campaigns', {
      task_text: byId('task-text').value,
      scenario: selectedScenario(),
      mode: selectedMode(),
      use_qwen: byId('use-qwen').checked,
      auto_run_p1: true,
    });
    await loadCampaignIfNeeded(payload);
    render(last(state.history));
  } catch (error) {
    byId('current-evidence').innerHTML = `<span class="field-label">闭环未完成</span><p>${escapeHtml(visibleError(error))}</p>`;
    byId('system-state').textContent = '闭环请求失败，未静默降级';
  } finally { setBusy(false); }
});

iterateButton.addEventListener('click', async () => {
  const baselineExperimentId = recordResult(state.history[0]).experiment_id;
  if (!baselineExperimentId || !candidateUnderTest()) return;
  setBusy(true, '正在按固定条件运行 P1...');
  try {
    const record = await request(`/api/experiments/${encodeURIComponent(baselineExperimentId)}/iterate`, {});
    state.history.push(record);
    render(record);
  } catch (error) {
    byId('current-evidence').innerHTML = `<span class="field-label">P1 未执行</span><p>${escapeHtml(visibleError(error))}</p>`;
  } finally { setBusy(false); }
});

resetButton.addEventListener('click', () => window.location.reload());
stopButton.addEventListener('click', requestStop);

document.querySelectorAll('input[name="mode"]').forEach((input) => input.addEventListener('change', () => {
  const isMock = selectedMode() === 'mock';
  byId('scenario-fieldset').classList.toggle('is-disabled', !isMock);
  refreshRuntime();
}));

byId('use-qwen').addEventListener('change', () => {
  byId('qwen-control-help').textContent = byId('use-qwen').checked ? '两次结构化调用：规划 + 反馈；证据会写入 campaign' : '确定性流程模式；不会伪造 Qwen 证据';
});

refreshConfig();
refreshRuntime();
