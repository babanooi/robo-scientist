const form = document.querySelector('#task-form');
const runButton = document.querySelector('#run-button');
const campaignButton = document.querySelector('#campaign-button');
const iterateButton = document.querySelector('#iterate-button');
const history = [];

const pretty = (value) => JSON.stringify(value, null, 2);
const byId = (id) => document.querySelector(`#${id}`);
const metric = (value, digits = 3) => Number.isFinite(value) ? value.toFixed(digits) : '-';
const metricWithUnit = (value, unit, digits = 3) => Number.isFinite(value) ? `${value.toFixed(digits)} ${unit}` : '-';
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
})[character]);

function selectedScenario() {
  return document.querySelector('input[name="scenario"]:checked').value;
}

function selectedMode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

function setBusy(isBusy, label = '正在运行...') {
  runButton.disabled = isBusy;
  campaignButton.disabled = isBusy;
  runButton.textContent = isBusy ? label : '运行 P0 基线实验';
  iterateButton.disabled = isBusy || !candidateUnderTest();
}

function candidateUnderTest() {
  return history[0]?.candidate_skill || null;
}

function statusLabel(status) {
  return { succeeded: '成功', failed: '失败', timed_out: '超时', rejected: '已拒绝' }[status] || status;
}

function statusClass(status) {
  return `status-${status || 'idle'}`;
}

function parameterDelta(record) {
  const candidate = record.candidate_skill;
  if (!candidate) return null;
  if (candidate.changed_parameter_family === 'path_profile') {
    return `transit_height: ${record.plan.skill.parameters.transit_height_m.toFixed(3)} m -> ${candidate.parameters.transit_height_m.toFixed(3)} m`;
  }
  const before = record.plan.skill.parameters.grasp_offset_m;
  const after = candidate.parameters.grasp_offset_m;
  return `grasp_offset: [${before.map((item) => item.toFixed(3)).join(', ')}] m -> [${after.map((item) => item.toFixed(3)).join(', ')}] m`;
}

function evidenceText(record) {
  const { result, analysis } = record;
  if (result.hardware_status === 'motion_completed_result_unverified') {
    return '动作链已完成；抓取、提起和放置尚无可验证结果证据。';
  }
  if (result.failure) return `${result.failure.stage}: ${result.failure.message}`;
  if (analysis) return analysis.evidence.join(' / ');
  if (result.status === 'succeeded' && result.data_source === 'real_arm') {
    const evaluator = result.artifacts.evaluator_type;
    return evaluator === 'vision'
      ? '视觉结果评估已确认抓取、提起与放置。'
      : evaluator === 'operator'
        ? '现场人员已确认抓取、提起与放置；本轮不是自主视觉判定。'
        : '结果评估器已确认抓取、提起与放置。';
  }
  if (result.status === 'succeeded' && result.data_source === 'mock') {
    return '流程 Mock 已完成；该结论不代表物理抓取成功。';
  }
  return '未产生可用的失败归因。';
}

function updateFacts(record) {
  const { result } = record;
  byId('result-title').textContent = result.hardware_status === 'motion_completed_result_unverified'
    ? '动作完成，结果待验证'
    : result.status === 'succeeded' ? '当前实验完成' : '当前实验需要分析';
  byId('system-state').textContent = `第 ${history.length} 轮实验已记录`;
  byId('mode-label').textContent = result.data_source.toUpperCase();
  byId('mode-notice').textContent = record.mode_notice;
  const status = byId('run-status');
  status.textContent = statusLabel(result.status);
  status.className = `status ${statusClass(result.status)}`;
  const facts = document.querySelectorAll('#run-facts dd');
  const factsData = [
    result.experiment_id,
    result.data_source,
    result.skill_version,
    result.scene_id,
    result.safety_check.allowed ? '通过' : '拦截',
    result.hardware_status,
  ];
  facts.forEach((fact, index) => { fact.textContent = factsData[index]; });
  byId('current-evidence').innerHTML = `<span class="evidence-label">当前结论</span><p>${escapeHtml(evidenceText(record))}</p>`;
  byId('plan-output').textContent = pretty({
    task: record.plan.task,
    target_pose: record.plan.target_pose,
    destination_pose: record.plan.destination_pose,
    skill: record.plan.skill,
    safety_check: result.safety_check,
  });
  byId('result-output').textContent = pretty({
    status: result.status,
    outcome: result.outcome,
    failure: result.failure,
    analysis: record.analysis,
    actions: result.actions,
    metrics: result.metrics,
    artifacts: result.artifacts,
  });
  byId('simulation-output').textContent = result.simulation
    ? pretty(result.simulation)
    : '本轮不是已验证的 Gazebo/MoveIt2 仿真执行。';
}

function renderLoop() {
  const baseline = history[0];
  const latest = history.at(-1);
  const candidate = baseline?.candidate_skill;
  const validation = history.length > 1 ? latest : null;
  const steps = [
    { index: 'P0', title: '基线计划', text: baseline ? `${baseline.plan.skill.version}: ${statusLabel(baseline.result.status)} · ${baseline.result.experiment_id}` : '等待任务输入与安全检查。', state: baseline ? (baseline.result.status === 'rejected' ? 'is-alert' : 'is-complete') : 'is-pending' },
    { index: '02', title: '结果与证据', text: baseline ? evidenceText(baseline) : '等待一次可追溯实验结果。', state: baseline ? (baseline.result.failure ? 'is-alert' : 'is-complete') : 'is-pending' },
    { index: 'P1', title: '候选版本', text: candidate ? `${parameterDelta(baseline)}。${candidate.change_reason}` : '未得到可优化的参数候选。', state: candidate ? 'is-complete' : (baseline ? 'is-muted' : 'is-pending') },
    { index: '04', title: '独立验证', text: validation ? `${validation.result.experiment_id}: ${statusLabel(validation.result.status)}。当前仅 ${history.length - 1}/10 次候选验证。` : '候选生成后，运行 P1 进行第一轮验证。', state: validation ? (validation.result.status === 'succeeded' ? 'is-complete' : 'is-alert') : 'is-pending' },
  ];
  byId('loop-track').innerHTML = steps.map((step) => `<article class="loop-step ${step.state}"><span class="step-index">${escapeHtml(step.index)}</span><h3>${escapeHtml(step.title)}</h3><p>${escapeHtml(step.text)}</p></article>`).join('');
  byId('sample-note').textContent = history.length ? `已记录 ${history.length} 轮 / 晋升需每版至少 10 次` : '尚未开始';
  renderDecision(baseline, validation);
}

function renderDecision(baseline, validation) {
  const output = byId('decision-output');
  if (!baseline) {
    output.innerHTML = '<strong>版本决策待定</strong><span>先运行基线实验，系统才会产生可审计的证据链。</span>';
    return;
  }
  if (!baseline.candidate_skill) {
    output.innerHTML = '<strong>未生成候选版本</strong><span>当前失败类型没有对应的单参数优化策略，系统不会伪造迭代。</span>';
    return;
  }
  if (!validation) {
    output.innerHTML = '<strong>候选 P1 待验证</strong><span>归因已改变下一轮抓取偏移参数，但尚无候选版本实验结果。</span>';
    return;
  }
  if (validation.result.status === 'rejected') {
    output.innerHTML = `<strong>候选 P1 被安全门阻止</strong><span>${escapeHtml(evidenceText(validation))}</span>`;
    return;
  }
  output.innerHTML = `<strong>暂不可晋升</strong><span>P1 已完成首轮验证（${statusLabel(validation.result.status)}），但当前样本不足 10 次，不能据此宣称性能提升。</span>`;
}

function renderLineage() {
  byId('lineage-count').textContent = `${history.length} 轮`;
  if (!history.length) return;
  byId('lineage-list').innerHTML = history.map((record, index) => {
    const { result } = record;
    const label = index === 0 ? 'P0 基线' : `P1 候选验证 ${index}`;
    const change = index === 0 && record.candidate_skill ? `<p class="lineage-change">下一轮改动: ${escapeHtml(parameterDelta(record))}</p>` : '';
    return `<li class="lineage-item"><div class="lineage-marker">${index + 1}</div><div><div class="lineage-topline"><strong>${escapeHtml(label)}</strong><span class="status ${statusClass(result.status)}">${escapeHtml(statusLabel(result.status))}</span></div><p>${escapeHtml(result.experiment_id)} · ${escapeHtml(result.skill_version)} · ${escapeHtml(result.data_source)}</p><p>${escapeHtml(evidenceText(record))}</p>${change}</div></li>`;
  }).join('');
}

function renderComparison() {
  const baseline = history[0];
  const candidateRun = history[1];
  const output = byId('comparison-output');
  if (!baseline?.candidate_skill) {
    output.textContent = '候选版本生成后，此处比较 P0 与 P1 的状态、定位误差、执行时间和安全事件。';
    output.className = 'comparison-empty';
    return;
  }
  const baseMetrics = baseline.result.metrics;
  const candidateMetrics = candidateRun?.result.metrics;
  const rows = [
    ['实验状态', statusLabel(baseline.result.status), candidateRun ? statusLabel(candidateRun.result.status) : '待验证'],
    ['定位误差', metricWithUnit(baseMetrics.position_error_m ?? baseline.result.outcome.position_error_m, 'm'), candidateRun ? metricWithUnit(candidateMetrics.position_error_m ?? candidateRun.result.outcome.position_error_m, 'm') : '-'],
    ['TCP / 仿真路径', metricWithUnit(baseMetrics.path_length_m, 'm'), candidateRun ? metricWithUnit(candidateMetrics.path_length_m, 'm') : '-'],
    ['关节总行程', metricWithUnit(baseMetrics.joint_total_travel_rad, 'rad'), candidateRun ? metricWithUnit(candidateMetrics.joint_total_travel_rad, 'rad') : '-'],
    ['P95 关节速度', metricWithUnit(baseMetrics.p95_derived_joint_speed_rad_s, 'rad/s'), candidateRun ? metricWithUnit(candidateMetrics.p95_derived_joint_speed_rad_s, 'rad/s') : '-'],
    ['执行时间', metricWithUnit(baseMetrics.execution_time_s, 's'), candidateRun ? metricWithUnit(candidateMetrics.execution_time_s, 's') : '-'],
    ['安全事件', metric(baseMetrics.safety_events, 0), candidateRun ? metric(candidateMetrics.safety_events, 0) : '-'],
  ];
  output.className = 'comparison-table-wrap';
  output.innerHTML = `<table><thead><tr><th>指标</th><th>P0 基线</th><th>P1 候选</th></tr></thead><tbody>${rows.map((row) => `<tr><th>${row[0]}</th><td>${row[1]}</td><td>${row[2]}</td></tr>`).join('')}</tbody></table><p class="comparison-note">此表展示单次链路证据，不构成晋升结论。候选版本需要在独立固定验证场景中完成至少 10 次重复实验。</p>`;
}

function render(record) {
  updateFacts(record);
  renderLoop();
  renderLineage();
  renderComparison();
  iterateButton.disabled = !candidateUnderTest();
}

async function request(url, body) {
  const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || '请求失败');
  return payload;
}

async function refreshRuntime() {
  const mode = selectedMode();
  try {
    const response = await fetch(`/api/runtime?mode=${encodeURIComponent(mode)}`);
    const runtime = await response.json();
    if (!response.ok) throw new Error(runtime.error || '运行时状态不可用');
    const capabilities = mode === 'real_arm' ? [
      ['桥接', runtime.available && runtime._bridge_mode === 'real_motion'],
      ['停止', runtime.stop_available === true],
      ['结果评估', runtime.result_evaluator_configured === true],
      ['候选参数', runtime.parameterized_skill_configured === true],
    ] : [[runtime.hardware_status || runtime.motion_state || mode, mode === 'mock']];
    byId('runtime-capabilities').innerHTML = capabilities.map(([label, ready]) => (
      `<span class="${ready ? 'is-ready' : 'is-blocked'}">${escapeHtml(label)} · ${ready ? '就绪' : '未就绪'}</span>`
    )).join('');
  } catch (error) {
    byId('runtime-capabilities').innerHTML = `<span class="is-blocked">${escapeHtml(error.message)}</span>`;
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  history.length = 0;
  setBusy(true, '正在运行 P0...');
  try {
    const record = await request('/api/tasks', { task_text: byId('task-text').value, scenario: selectedScenario(), mode: selectedMode() });
    history.push(record);
    render(record);
  } catch (error) {
    byId('result-title').textContent = error.message;
  } finally {
    setBusy(false);
  }
});

campaignButton.addEventListener('click', async () => {
  setBusy(true, '正在运行受监督迭代...');
  try {
    const campaign = await request('/api/campaigns', {
      task_text: form.task_text.value,
      scenario: selectedScenario(),
      mode: selectedMode(),
      max_rounds: 2,
    });
    history.splice(0, history.length, ...campaign.records);
    render(history.at(-1));
  } catch (error) {
    byId('result-title').textContent = error.message;
  } finally {
    setBusy(false);
  }
});

iterateButton.addEventListener('click', async () => {
  const baselineExperimentId = history[0]?.result.experiment_id;
  if (!baselineExperimentId || !candidateUnderTest()) return;
  setBusy(true, '正在运行 P1...');
  try {
    const record = await request(`/api/experiments/${baselineExperimentId}/iterate`, { scenario: selectedScenario(), mode: selectedMode() });
    history.push(record);
    render(record);
  } catch (error) {
    byId('result-title').textContent = error.message;
  } finally {
    setBusy(false);
  }
});

document.querySelectorAll('input[name="mode"]').forEach((input) => {
  input.addEventListener('change', () => {
    const isMock = selectedMode() === 'mock';
    byId('scenario-fieldset').classList.toggle('is-disabled', !isMock);
    refreshRuntime();
  });
});

refreshRuntime();
