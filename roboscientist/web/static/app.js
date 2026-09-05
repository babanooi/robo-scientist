/* The console deliberately renders evidence defensively: the API can add fields
 * without making an old campaign impossible to inspect. */
const form = document.querySelector('#task-form');
const runButton = document.querySelector('#run-button');
const campaignButton = document.querySelector('#campaign-button');
const iterateButton = document.querySelector('#iterate-button');
const resetButton = document.querySelector('#reset-button');
const stopButton = document.querySelector('#stop-button');
const trajectoryReplayButton = document.querySelector('#trajectory-replay-button');
const validationButton = document.querySelector('#validation-button');
const state = {
  history: [], campaign: null, config: null,
  runtime: null, runtimeMode: null, runtimeError: null, lastStop: null,
  validation: null,
  replayTimer: null, replayIndex: -1, replayPlot: null,
  replayCatalog: [], replayRuns: {}, historicalReplay: null,
  selectedReplay: 'historical',
  hardwareBaselines: [], hardwareEvidence: null, selectedHardwareBaseline: null,
};

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
  return document.querySelector('input[name="mode"]:checked')?.value || 'simulation';
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
    simulation: ['SIMULATION · 虚拟工作台', 'source-simulation', '项目自带纯 Python 虚拟工作台；结果不代表真实机械臂或 Gazebo/MoveIt2 验证。'],
    real_arm: ['REAL ARM · 实机证据', 'source-real', '真实机械臂结果；自主闭环必须由视觉评价和完整日志证明。'],
    historical_real_arm: ['HISTORICAL REAL ARM · 历史实机', 'source-real', '已采集的历史实机证据；只读回放，不代表当前机械臂在线。'],
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
    simulation_software_virtual: '软件虚拟闭环（非实机）',
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

function monitorSourceLabel(source) {
  return {
    mock: ['Mock / 流程', '本地适配器，不驱动机械臂'],
    simulation: ['Simulation / 虚拟工作台', '纯 Python 可复验运行时；不连接实机'],
    real_arm: ['Real Arm / 实机', '仅允许通过安全 bridge 调用'],
    historical_real_arm: ['Historical Real Arm / 历史实机', '离线证据，只读回放'],
  }[source] || [String(source || 'unknown').toUpperCase(), '数据来源未分类'];
}

function safeHttpUrl(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const parsed = new URL(value.trim(), window.location.href);
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null;
  } catch (error) {
    return null;
  }
}

function urlSummary(value) {
  try {
    const parsed = new URL(value);
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
  } catch (error) {
    return String(value || '');
  }
}

function booleanValue(value) {
  return value === true || value === 1 || value === 'true';
}

function hardwareBaselineLabel(baseline) {
  const id = baseline?.baseline_id || baseline?.id || '';
  return {
    fixed_a_to_b_p0: '固定 A→B · P0 基线',
    multishape_factory: '多形状 · 工厂参数',
  }[id] || id || '未命名基线';
}

function formatHardwareParameter(value) {
  if (Array.isArray(value)) {
    return `[${value.map((item) => formatHardwareParameter(item)).join(', ')}]`;
  }
  if (value && typeof value === 'object') {
    return Object.entries(value)
      .map(([key, item]) => `${key}: ${formatHardwareParameter(item)}`)
      .join(' · ');
  }
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
  return String(value ?? '-');
}

function renderHardwareBaseline(baseline) {
  const summary = byId('hardware-baseline-summary');
  const list = byId('hardware-parameter-list');
  const badge = byId('hardware-reference-badge');
  if (!summary || !list || !badge) return;
  if (!baseline) {
    summary.innerHTML = '<p class="hardware-empty">暂无可用硬件基线。</p>';
    list.innerHTML = '<p class="hardware-empty">未加载参数。</p>';
    badge.textContent = 'REFERENCE · 未加载';
    badge.className = 'source-badge source-unknown';
    return;
  }
  const verified = baseline.current_hardware_verified === true;
  badge.textContent = `REFERENCE · ${hardwareBaselineLabel(baseline)}`;
  badge.className = `source-badge ${verified ? 'source-simulation' : 'source-unknown'}`;
  summary.innerHTML = `<strong>${escapeHtml(hardwareBaselineLabel(baseline))}</strong><p>${escapeHtml(baseline.documented_claim || '这是文档参数快照，不代表当前机械臂状态。')}</p><p class="hardware-baseline-meta">当前实机验证：${verified ? '是' : '否'} · 只读：是 · 不触发运动</p>`;
  const parameters = baseline.parameters && typeof baseline.parameters === 'object' ? baseline.parameters : {};
  const entries = Object.entries(parameters);
  list.innerHTML = entries.length
    ? entries.map(([key, value]) => `<dl class="hardware-parameter"><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(formatHardwareParameter(value))}</dd></dl>`).join('')
    : '<p class="hardware-empty">该基线没有可展示参数。</p>';
}

function renderHardwareEvidence(payload) {
  const statusNode = byId('hardware-evidence-status');
  const stateNode = byId('hardware-proof-state');
  const messageNode = byId('hardware-proof-message');
  const facts = document.querySelectorAll('#hardware-proof-facts dd');
  const details = byId('hardware-proof-missing');
  if (!statusNode || !stateNode || !messageNode || !details) return;
  const status = String(payload?.status || 'unavailable').toLowerCase();
  const labels = {
    passed: ['已通过', 'status-succeeded', '证据包满足当前校验规则。'],
    incomplete: ['不完整', 'status-warn', '证据包可读取，但缺少或无法验证一项或多项实机材料。'],
    invalid: ['无效', 'status-rejected', '证据包包含不可安全读取的成员或格式错误。'],
    unavailable: ['未配置', 'status-idle', '尚未通过启动参数配置硬件实验包。'],
  };
  const [label, className, fallback] = labels[status] || labels.unavailable;
  const stateContainer = stateNode.closest('.hardware-proof-state') || stateNode;
  const stateLabel = stateNode.tagName === 'STRONG' ? stateNode : stateNode.querySelector('strong');
  statusNode.textContent = label;
  statusNode.className = `status ${className}`;
  stateContainer.className = `hardware-proof-state ${status === 'passed' ? 'is-passed' : status === 'invalid' ? 'is-invalid' : ''}`;
  if (stateLabel) stateLabel.textContent = payload?.source_name ? `${label} · ${payload.source_name}` : label;
  messageNode.textContent = payload?.message || fallback;
  const validation = payload?.validation || payload || {};
  const missing = Array.isArray(payload?.missing) ? payload.missing : (Array.isArray(validation.missing) ? validation.missing : []);
  const warnings = Array.isArray(payload?.warnings) ? payload.warnings : (Array.isArray(validation.warnings) ? validation.warnings : []);
  const claims = payload?.claims_supported || validation.claims_supported || {};
  const supported = Object.entries(claims).filter(([, value]) => value === true).map(([key]) => key);
  const values = [
    payload?.package_type || validation.package_type || '-',
    String((payload?.files_checked || validation.files_checked || []).length),
    String(missing.length),
    supported.length ? supported.join(', ') : '无',
  ];
  values.forEach((value, index) => { if (facts[index]) facts[index].textContent = value; });
  const notices = [...missing.map((item) => `缺失 · ${item}`), ...warnings.map((item) => `警告 · ${item}`)];
  details.querySelector('p').textContent = notices.length ? notices.slice(0, 8).join('\n') : (status === 'passed' ? '未发现缺失项或警告。' : fallback);
  byId('hardware-proof-time').textContent = `最近检查 · ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
}

async function refreshHardwareEvidence() {
  const select = byId('hardware-baseline-select');
  try {
    const baselines = await request('/api/hardware/baselines');
    state.hardwareBaselines = Array.isArray(baselines?.baselines) ? baselines.baselines : [];
    if (select) {
      select.innerHTML = state.hardwareBaselines.length
        ? state.hardwareBaselines.map((baseline) => `<option value="${escapeHtml(baseline.baseline_id)}">${escapeHtml(hardwareBaselineLabel(baseline))}</option>`).join('')
        : '<option value="">暂无基线</option>';
      select.disabled = !state.hardwareBaselines.length;
    }
    state.selectedHardwareBaseline = state.hardwareBaselines.find((item) => item.baseline_id === state.selectedHardwareBaseline)?.baseline_id
      || state.hardwareBaselines[0]?.baseline_id || null;
    if (select && state.selectedHardwareBaseline) select.value = state.selectedHardwareBaseline;
    renderHardwareBaseline(state.hardwareBaselines.find((item) => item.baseline_id === state.selectedHardwareBaseline) || null);
  } catch (error) {
    state.hardwareBaselines = [];
    if (select) { select.innerHTML = '<option value="">基线接口不可用</option>'; select.disabled = true; }
    renderHardwareBaseline(null);
  }
  try {
    state.hardwareEvidence = await request('/api/hardware/evidence');
    renderHardwareEvidence(state.hardwareEvidence);
  } catch (error) {
    state.hardwareEvidence = { status: 'unavailable', message: `证据接口不可用：${error.message}` };
    renderHardwareEvidence(state.hardwareEvidence);
  }
}

function monitorStatus(title, tone = 'idle', detail = '') {
  return { title, tone, detail };
}

function setMonitorTile(id, value) {
  const title = byId(id);
  if (!title) return;
  const tile = title.closest('.motion-status-tile');
  if (tile) tile.className = `motion-status-tile is-${value.tone || 'idle'}`;
  title.textContent = value.title || '-';
  const detail = byId(`${id}-detail`);
  if (detail) detail.textContent = value.detail || '';
}

function runtimeIsBridgeReady(source, runtime) {
  if (!runtime) return false;
  if (source === 'mock') return false;
  if (source === 'simulation') {
    return (booleanValue(runtime.available)
      || runtime.hardware_status === 'simulation_runtime_verified'
      || runtime.runtime_status === 'simulation_runtime_verified')
      && runtime.hardware_status !== 'simulation_runtime_unverified'
      && runtime.runtime_status !== 'simulation_runtime_unverified';
  }
  const bridgeMode = runtime._bridge_mode || runtime.bridge_mode || runtime.mode;
  return booleanValue(runtime.available)
    && (bridgeMode === 'real_motion' || booleanValue(runtime.bridge_connected));
}

function monitorBridgeStatus(source, runtime) {
  if (source === 'mock') return monitorStatus('不连接', 'idle', 'Mock 仅验证软件流程');
  if (!runtime) return monitorStatus('未读取', 'idle', '等待 /api/runtime');
  if (runtimeIsBridgeReady(source, runtime)) {
    return monitorStatus(
      source === 'simulation' ? '已就绪' : '已连接',
      'ready',
      source === 'simulation'
        ? (runtime.runtime_name || '本地纯 Python 虚拟工作台')
        : (runtime.backend || '运行时已报告可用'),
    );
  }
  if (source === 'simulation') {
    return monitorStatus('已就绪', 'ready', runtime.runtime_name || '本地纯 Python 虚拟工作台');
  }
  return monitorStatus('不可用', 'blocked', runtime.message || '实机 bridge 未通过健康检查');
}

function monitorMotionStatus(source, runtime) {
  if (source === 'mock') return monitorStatus('仅流程', 'idle', '不会发送底层运动命令');
  if (source === 'simulation') {
    if (runtime?.motion_state === 'unavailable' || runtime?.hardware_status === 'simulation_runtime_unverified') {
      return monitorStatus('未接通', 'blocked', 'legacy 仿真适配器契约存在，运行时未核验');
    }
    return monitorStatus(runtime?.motion_state || '待机', 'ready', '本地虚拟执行；不发送机械臂命令');
  }
  if (runtime?.motion_busy === true) return monitorStatus('执行中', 'active', 'bridge 报告动作占用');
  if (runtimeIsBridgeReady(source, runtime) && booleanValue(runtime.motion_enabled)) {
    return monitorStatus(runtime.motion_state || '空闲', 'ready', '真实运动仍需人工安全监护');
  }
  return monitorStatus('不可用', 'blocked', '实机运动未获得双重放行');
}

function preflightStatus(record) {
  if (!record) return monitorStatus('未执行', 'idle', '尚未提交 P0/P1');
  const result = recordResult(record);
  const safety = result.safety_check || result.safety || {};
  const actions = Array.isArray(result.actions) ? result.actions : [];
  const action = actions.find((item) => /preflight|health|安全|预检/i.test(String(item?.action || '')));
  if (safety.allowed === false || action?.status === 'rejected' || action?.status === 'failed') {
    return monitorStatus('已拦截', 'blocked', action?.message || safety.messages?.[0] || '安全检查未通过');
  }
  if (safety.allowed === true && (!action || action.status === 'succeeded')) {
    return monitorStatus('已通过', 'ready', result.data_source === 'real_arm' ? '执行前 bridge 预检已记录' : '流程安全检查已记录');
  }
  return monitorStatus('待确认', 'warn', '结果中没有完整的预检回执');
}

function stopStatus(source, runtime) {
  const stop = state.lastStop;
  if (stop) {
    const stopped = booleanValue(stop.stopped) || stop.status === 'succeeded';
    return stopped
      ? monitorStatus('已回执', 'ready', '最近一次停止请求已返回')
      : monitorStatus('需人工接管', 'blocked', stop.message || '停止请求未确认');
  }
  if (source !== 'real_arm') return monitorStatus('仅实机', 'idle', '当前数据源不提供物理停止');
  if (runtime?.real_motion_stop_verified === true || runtime?.stop_verified === true) {
    return monitorStatus('已验证', 'ready', 'bridge 返回已验证停止能力');
  }
  if (runtime?.stop_available === true) {
    return monitorStatus('已配置·待实测', 'warn', '有停止入口，但尚无动作中实测证据');
  }
  return monitorStatus('不可用', 'blocked', '未发现可用的实机停止回执');
}

function runtimeStreamUrl(runtime) {
  return safeHttpUrl(pick(runtime, [
    'stream_url', 'video_url', 'camera_stream_url', 'live_view_url',
    'camera.stream_url', 'camera.video_url', 'stream.url',
  ], null));
}

function setBusy(isBusy, label = '正在运行...') {
  runButton.disabled = isBusy;
  campaignButton.disabled = isBusy;
  runButton.textContent = isBusy ? label : '仅运行 P0（诊断）';
  iterateButton.disabled = isBusy || !candidateUnderTest();
  resetButton.disabled = isBusy || !candidateUnderTest();
  if (validationButton) validationButton.disabled = isBusy;
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
    state.lastStop = { ...result, received_at: new Date().toISOString() };
    const stopped = result.status === 'succeeded' || result.stopped === true;
    byId('system-state').textContent = stopped ? '已请求停止，等待人工检查' : '停止请求已返回，请立即人工检查';
    byId('current-evidence').innerHTML = `<span class="field-label">人工接管 · ${escapeHtml(stopped ? '已确认' : '需检查')}</span><p>${escapeHtml(result.message || '停止结果未提供详细信息。')}</p>`;
    renderMotionMonitor(last(state.history));
  } catch (error) {
    state.lastStop = { status: 'failed', message: error.message, received_at: new Date().toISOString() };
    byId('system-state').textContent = '停止请求失败，立即执行物理急停';
    byId('current-evidence').innerHTML = `<span class="field-label">人工接管 · 失败</span><p>${escapeHtml(error.message)}。请立即执行现场物理急停。</p>`;
    renderMotionMonitor(last(state.history));
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

/*
 * Replay is deliberately read-only.  It accepts a future /api/replay-shaped
 * object ({available, data_source, trajectory, events, metrics}) when one is
 * included in a record, while falling back to the action evidence already
 * returned by /api/tasks or /api/campaigns.  No replay call can send a robot
 * command and no path is inferred when the backend did not return one.
 */
function replayData(record) {
  const result = recordResult(record);
  const supplied = record?.replay || record?.trajectory_replay
    || result.replay || result.trajectory_replay || {};
  const replay = supplied && typeof supplied === 'object' && !Array.isArray(supplied)
    ? supplied : {};
  const events = Array.isArray(replay.events)
    ? replay.events
    : Array.isArray(replay.actions)
      ? replay.actions
      : Array.isArray(result.actions) ? result.actions : [];
  const trajectory = pick(replay, ['trajectory', 'trajectory_url', 'path', 'file'], null)
    || pick(result, ['artifacts.trajectory', 'artifacts.trajectory_url', 'simulation.trajectory'], null);
  const metrics = replay.metrics && typeof replay.metrics === 'object'
    ? replay.metrics : (result.metrics || {});
  return {
    kind: 'experiment',
    available: replay.available === true || Boolean(trajectory) || events.length > 0,
    dataSource: replay.data_source || result.data_source || sourceOf(record),
    trajectory,
    events,
    metrics,
    reason: replay.reason || replay.message || '',
  };
}

function historicalReplayData(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const summary = payload.summary || {};
  const trajectory = payload.trajectory && typeof payload.trajectory === 'object'
    ? payload.trajectory : null;
  return {
    kind: 'historical',
    available: payload.status === 'available' && payload.read_only === true && payload.motion_requested === false,
    dataSource: payload.data_source || 'historical_real_arm',
    runId: payload.run_id || '-',
    trajectory,
    events: Array.isArray(payload.events) ? payload.events : [],
    visionTargets: Array.isArray(payload.vision_targets) ? payload.vision_targets : [],
    metrics: payload.metrics && typeof payload.metrics === 'object' ? payload.metrics : {},
    summary,
    calibration: payload.calibration || null,
    evidence: payload.evidence || {},
    limitations: Array.isArray(payload.limitations) ? payload.limitations : [],
    reason: payload.message || payload.reason_code || '历史运行包不可用',
  };
}

function selectedReplayPayload() {
  const select = byId('trajectory-source-select');
  const value = select?.value || state.selectedReplay;
  if (value === '__current__') return null;
  if (value && state.replayRuns[value]) return state.replayRuns[value];
  return state.historicalReplay;
}

function activeReplayData(record) {
  const select = byId('trajectory-source-select');
  const value = select?.value || state.selectedReplay;
  if (value !== '__current__') {
    const payload = selectedReplayPayload();
    const historical = historicalReplayData(payload);
    if (historical) return historical;
    if (!record) return { kind: 'historical', available: false, dataSource: 'historical_real_arm', events: [], metrics: {}, reason: '尚未加载历史实机运行包' };
  }
  return replayData(record);
}

function updateReplaySourceOptions() {
  const select = byId('trajectory-source-select');
  if (!select) return;
  const previous = state.selectedReplay || select.value || 'historical';
  const available = state.replayCatalog.filter((run) => run.status === 'available');
  select.innerHTML = available.map((run) => `<option value="${escapeHtml(run.run_id)}">历史实机 · ${escapeHtml(run.run_id)}</option>`).join('');
  select.insertAdjacentHTML('beforeend', '<option value="__current__">当前实验记录</option>');
  if (!available.length) {
    select.insertAdjacentHTML('afterbegin', '<option value="historical" disabled>历史实机 · 未配置</option>');
  }
  const preferred = previous === '__current__' ? '__current__' : (available.some((run) => run.run_id === previous) ? previous : available[0]?.run_id || '__current__');
  select.value = preferred;
  state.selectedReplay = preferred;
  select.disabled = !available.length && !last(state.history);
}

function renderReplaySourceDetail(replay) {
  const node = byId('trajectory-source-detail');
  if (!node) return;
  if (replay?.kind === 'historical') {
    const hash = replay.calibration?.sha256 ? ` · 标定 ${replay.calibration.sha256.slice(0, 12)}…` : '';
    node.textContent = `历史实机只读 · ${replay.runId || '-'}${hash}`;
  } else {
    node.textContent = '当前实验记录 · 只读展示，不发送运动命令';
  }
}

function trajectoryReference(value) {
  if (Array.isArray(value)) return `${value.length} 个采样点`;
  if (value && typeof value === 'object') {
    const count = value.returned_points ?? value.samples ?? value.points?.length ?? value.length;
    const original = value.original_points;
    if (finite(Number(count))) {
      return finite(Number(original)) && Number(original) > Number(count)
        ? `${Number(count)} / ${Number(original)} 个采样点`
        : `${Number(count)} 个采样点`;
    }
    return '结构化轨迹已返回';
  }
  if (typeof value === 'string' && value.trim()) {
    const parts = value.trim().split(/[\\/]/);
    return parts[parts.length - 1] || value.trim();
  }
  return '';
}

function trajectoryActionTone(status) {
  const normalized = String(status || '').toLowerCase();
  if (normalized === 'succeeded' || normalized === 'success' || normalized === 'completed') return 'complete';
  if (normalized === 'failed' || normalized === 'rejected' || normalized === 'timed_out' || normalized === 'stopped') return 'failed';
  return 'pending';
}

function trajectoryActionLabel(action, index) {
  const value = action?.action || action?.name || action?.stage || `动作 ${index + 1}`;
  const labels = {
    approach: '接近目标', move_to_pregrasp: '移动至预抓取位',
    close_gripper: '夹爪闭合', grasp: '抓取', lift: '提起',
    grasp_evaluation: '抓取评价', path_check: '路径安全检查',
    transit: '中转移动', place: '放置', stop: '停止 / 接管',
    adapter_preflight: '适配器预检', hardware_bridge_health: 'Bridge 健康检查',
    simulation_contract_check: '仿真契约检查',
  };
  return labels[value] || String(value);
}

function stopTrajectoryReplay() {
  if (state.replayTimer !== null) {
    clearTimeout(state.replayTimer);
    state.replayTimer = null;
  }
  state.replayIndex = -1;
  document.querySelectorAll('.trajectory-step').forEach((step) => step.classList.remove('is-active'));
  if (state.replayPlot?.cursor) state.replayPlot.cursor.setAttribute('visibility', 'hidden');
  if (trajectoryReplayButton) {
    trajectoryReplayButton.classList.remove('is-playing');
    trajectoryReplayButton.textContent = '播放动作阶段';
  }
}

function replayDuration(action) {
  const seconds = Number(action?.duration_s);
  if (!Number.isFinite(seconds) || seconds <= 0) return 650;
  return Math.max(350, Math.min(1800, seconds * 500));
}

function optionalNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function renderTrajectoryMetrics(replay = {}) {
  const metrics = replay?.metrics || {};
  const historical = replay?.kind === 'historical';
  const path = optionalNumber(historical
    ? pick(metrics, ['joint_total_travel_rad'], null)
    : pick(metrics, ['path_length_m', 'trajectory_length_m', 'path_length'], null));
  const execution = optionalNumber(historical
    ? pick(metrics, ['joint_trajectory_duration_s', 'duration_s'], null)
    : pick(metrics, ['execution_time_s', 'duration_s', 'execution_time'], null));
  const safety = optionalNumber(historical
    ? null
    : pick(metrics, ['safety_events', 'safety_event_count'], null));
  const pathNode = byId('trajectory-path-metric');
  const timeNode = byId('trajectory-time-metric');
  const safetyNode = byId('trajectory-safety-metric');
  const pathLabel = byId('trajectory-path-label');
  const timeLabel = byId('trajectory-time-label');
  const safetyLabel = byId('trajectory-safety-label');
  if (pathLabel) pathLabel.textContent = historical ? '关节总行程' : '路径';
  if (timeLabel) timeLabel.textContent = historical ? '记录时长' : '执行';
  if (safetyLabel) safetyLabel.textContent = historical ? '原始轨迹点' : '安全事件';
  if (pathNode) pathNode.textContent = path === null ? '-' : historical ? `${path.toFixed(3)} rad` : `${path.toFixed(3)} m`;
  if (timeNode) timeNode.textContent = execution === null ? '-' : `${execution.toFixed(2)} s`;
  if (safetyNode) {
    const points = historical ? optionalNumber(replay?.trajectory?.original_points) : safety;
    safetyNode.textContent = points === null ? '-' : `${points.toFixed(0)}`;
  }
}

function clearTrajectoryChart(message = '没有可绘制的连续关节轨迹。') {
  const chart = byId('trajectory-chart');
  if (chart) chart.innerHTML = `<p class="trajectory-chart-empty">${escapeHtml(message)}</p>`;
  state.replayPlot = null;
  const detail = byId('trajectory-point-detail');
  if (detail) detail.textContent = '尚未选择轨迹点';
}

function updateTrajectoryCursor(index, visible = true) {
  const plot = state.replayPlot;
  if (!plot?.cursor || !plot.xs?.length) return;
  const bounded = Math.max(0, Math.min(plot.xs.length - 1, Number(index) || 0));
  plot.cursor.setAttribute('x1', plot.xs[bounded].toFixed(2));
  plot.cursor.setAttribute('x2', plot.xs[bounded].toFixed(2));
  plot.cursor.setAttribute('visibility', visible ? 'visible' : 'hidden');
  const point = plot.points[bounded] || {};
  const joints = Object.entries(point.joints || {}).map(([name, value]) => `${name} ${Number(value).toFixed(2)}`).join(' · ');
  const detail = byId('trajectory-point-detail');
  if (detail) detail.textContent = `t = ${Number(point.time_from_start_s || 0).toFixed(2)} s · ${joints || '关节值未提供'}`;
}

function renderTrajectoryChart(replay) {
  const chart = byId('trajectory-chart');
  if (!chart) return;
  const points = replay?.trajectory?.points;
  if (!Array.isArray(points) || points.length < 2) {
    clearTrajectoryChart(replay?.kind === 'historical' ? '历史包没有足够的有效关节采样点。' : '本轮没有可绘制的连续关节轨迹。');
    return;
  }
  const jointNames = replay.trajectory.joint_names?.length
    ? replay.trajectory.joint_names
    : [...new Set(points.flatMap((point) => Object.keys(point.joints || {})))];
  const values = jointNames.flatMap((name) => points.map((point) => Number(point.joints?.[name])).filter(Number.isFinite));
  if (!values.length) {
    clearTrajectoryChart('关节采样存在，但没有可用的数值位置。');
    return;
  }
  const width = 720;
  const height = 188;
  const left = 39;
  const right = 10;
  const top = 10;
  const bottom = 23;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (Math.abs(max - min) < 1e-9) { min -= 0.5; max += 0.5; }
  const pad = (max - min) * 0.08;
  min -= pad; max += pad;
  const xs = points.map((point, index) => left + (index / (points.length - 1)) * plotWidth);
  const y = (value) => top + ((max - value) / (max - min)) * plotHeight;
  const colors = ['#ba402d', '#2f6e64', '#c18b38', '#4f5e8a', '#8b5f73', '#5c6c53', '#9a6d42', '#65716b'];
  const horizontal = [0, 0.5, 1].map((ratio) => {
    const yy = top + ratio * plotHeight;
    const label = (max - ratio * (max - min)).toFixed(2);
    return `<line class="chart-grid" x1="${left}" x2="${width - right}" y1="${yy.toFixed(2)}" y2="${yy.toFixed(2)}"></line><text class="chart-axis-label" x="2" y="${(yy + 3).toFixed(2)}">${label}</text>`;
  }).join('');
  const vertical = [0, 0.5, 1].map((ratio) => {
    const xx = left + ratio * plotWidth;
    return `<line class="chart-grid" x1="${xx.toFixed(2)}" x2="${xx.toFixed(2)}" y1="${top}" y2="${height - bottom}"></line>`;
  }).join('');
  const lines = jointNames.map((name, jointIndex) => {
    const pointsString = points.map((point, index) => {
      const value = Number(point.joints?.[name]);
      return Number.isFinite(value) ? `${xs[index].toFixed(2)},${y(value).toFixed(2)}` : '';
    }).filter(Boolean).join(' ');
    return `<polyline class="chart-line" stroke="${colors[jointIndex % colors.length]}" points="${pointsString}"></polyline>`;
  }).join('');
  const legend = jointNames.map((name, index) => `<span><i style="background:${colors[index % colors.length]}"></i>${escapeHtml(name)}</span>`).join('');
  const lastPoint = points[points.length - 1] || {};
  chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="关节位置随时间变化"><g>${horizontal}${vertical}${lines}<line id="trajectory-chart-cursor" class="chart-cursor" x1="${xs[0].toFixed(2)}" x2="${xs[0].toFixed(2)}" y1="${top}" y2="${height - bottom}" visibility="hidden"></line></g><text class="chart-axis-label" x="${left}" y="${height - 5}">0 s</text><text class="chart-axis-label" x="${width - right - 35}" y="${height - 5}">${Number(lastPoint.time_from_start_s || 0).toFixed(0)} s</text></svg><div class="trajectory-chart-legend">${legend}</div>`;
  state.replayPlot = { cursor: byId('trajectory-chart-cursor'), xs, points, jointNames };
  updateTrajectoryCursor(0, false);
}

function playJointTrajectoryReplay(replay) {
  const points = replay?.trajectory?.points || [];
  if (points.length < 2) return;
  if (state.replayTimer !== null) {
    stopTrajectoryReplay();
    byId('trajectory-progress').textContent = '回放已停止；不会发送运动命令';
    return;
  }
  let index = 0;
  const stride = Math.max(1, Math.ceil(points.length / 100));
  trajectoryReplayButton.classList.add('is-playing');
  trajectoryReplayButton.textContent = '停止回放';
  const tick = () => {
    updateTrajectoryCursor(index, true);
    state.replayIndex = index;
    byId('trajectory-progress').textContent = `历史轨迹 ${index + 1}/${points.length} · 只读回放`;
    state.replayTimer = window.setTimeout(() => {
      index += stride;
      if (index >= points.length) {
        updateTrajectoryCursor(points.length - 1, true);
        state.replayTimer = null;
        state.replayIndex = -1;
        trajectoryReplayButton.classList.remove('is-playing');
        trajectoryReplayButton.textContent = '重新播放';
        byId('trajectory-progress').textContent = `已完成 ${points.length} 个关节采样点 · 只读回放`;
        return;
      }
      tick();
    }, 70);
  };
  tick();
}

function playTrajectoryReplay() {
  const record = last(state.history);
  const replay = activeReplayData(record);
  if (replay?.kind === 'historical') {
    playJointTrajectoryReplay(replay);
    return;
  }
  const steps = [...document.querySelectorAll('.trajectory-step')];
  if (!steps.length) return;
  if (state.replayTimer !== null) {
    stopTrajectoryReplay();
    byId('trajectory-progress').textContent = '回放已停止；不会发送运动命令';
    return;
  }
  const events = replay.events;
  let index = 0;
  trajectoryReplayButton.classList.add('is-playing');
  trajectoryReplayButton.textContent = '停止回放';
  const tick = () => {
    steps.forEach((step, stepIndex) => step.classList.toggle('is-active', stepIndex === index));
    state.replayIndex = index;
    const name = trajectoryActionLabel(events[index], index);
    byId('trajectory-progress').textContent = `回放 ${index + 1}/${steps.length} · ${name}`;
    state.replayTimer = window.setTimeout(() => {
      index += 1;
      if (index >= steps.length) {
        state.replayTimer = null;
        state.replayIndex = -1;
        steps.forEach((step) => step.classList.remove('is-active'));
        trajectoryReplayButton.classList.remove('is-playing');
        trajectoryReplayButton.textContent = '重新播放';
        byId('trajectory-progress').textContent = `已完成 ${steps.length} 个动作阶段 · 只读回放`;
        return;
      }
      tick();
    }, replayDuration(events[index]));
  };
  tick();
}

function renderTrajectory(record) {
  const status = byId('trajectory-status');
  const track = byId('trajectory-track');
  const progress = byId('trajectory-progress');
  const note = byId('trajectory-note');
  if (!status || !track || !progress || !note) return;
  stopTrajectoryReplay();
  const replay = activeReplayData(record);
  renderReplaySourceDetail(replay);
  if (replay?.kind === 'historical') {
    renderTrajectoryChart(replay);
    renderTrajectoryMetrics(replay);
    trajectoryReplayButton.textContent = '播放关节轨迹';
    if (!replay.available) {
      status.textContent = '历史数据不可用';
      status.className = 'status status-rejected';
      track.innerHTML = `<p class="trajectory-empty">${escapeHtml(replay.reason || '尚未加载历史实机运行包。')}</p>`;
      trajectoryReplayButton.disabled = true;
      progress.textContent = '暂无可回放数据';
      note.textContent = '请使用 --replay-source 指定历史运行包；读取失败时不会显示虚构轨迹。';
      return;
    }
    const points = replay.trajectory?.points || [];
    const targets = replay.visionTargets || [];
    status.textContent = '历史实机 · 只读';
    status.className = 'status status-warn';
    trajectoryReplayButton.textContent = '播放关节轨迹';
    if (targets.length) {
      track.innerHTML = targets.map((target, index) => {
        const position = [target.x, target.y, target.z].every(Number.isFinite)
          ? `[${target.x.toFixed(3)}, ${target.y.toFixed(3)}, ${target.z.toFixed(3)}] m` : '坐标未提供';
        const shape = target.shape || 'unknown';
        const yaw = Number.isFinite(Number(target.yaw)) ? `yaw ${Number(target.yaw).toFixed(1)}` : 'yaw 未提供';
        return `<article class="trajectory-observation"><span class="trajectory-dot">${index + 1}</span><div><strong>视觉目标 · ${escapeHtml(shape)}</strong><small>${escapeHtml(position)} · ${escapeHtml(yaw)}</small></div><span class="trajectory-step-status">已记录</span></article>`;
      }).join('');
    } else if (replay.events.length) {
      track.innerHTML = replay.events.map((event, index) => `<article class="trajectory-observation"><span class="trajectory-dot">${index + 1}</span><div><strong>原始事件</strong><small>${escapeHtml(event.result || event.message || '未提供结果文本')}</small></div><span class="trajectory-step-status">未评价</span></article>`).join('');
    } else {
      track.innerHTML = '<p class="trajectory-empty">轨迹已读取，但 events.csv 没有结构化结果；此处不推断抓取成功或失败。</p>';
    }
    trajectoryReplayButton.disabled = points.length < 2;
    const originalPoints = Number(replay.trajectory?.original_points);
    const pointLabel = points.length
      ? `${points.length}${Number.isFinite(originalPoints) && originalPoints > points.length ? ` / ${originalPoints}` : ''} 个采样点`
      : '没有可回放采样点';
    progress.textContent = points.length ? `${pointLabel} · ${targets.length} 条视觉目标` : pointLabel;
    const limitation = replay.limitations?.join(' ') || '历史数据只用于离线展示。';
    note.textContent = `来源：Historical Real Arm。${limitation} 页面不会重新执行，也不把关节空间总行程解释为 TCP 最优路径。`;
    return;
  }
  renderTrajectoryChart(replay);
  renderTrajectoryMetrics(replay);
  if (!record) {
    status.textContent = '暂无证据';
    status.className = 'status status-idle';
    track.innerHTML = '<p class="trajectory-empty">运行一轮实验后，这里显示已记录的动作阶段；没有轨迹文件时不会绘制虚构路径。</p>';
    trajectoryReplayButton.disabled = true;
    progress.textContent = '尚未载入回放';
    note.textContent = '回放只高亮历史事件，不会重新发送机械臂运动命令。';
    return;
  }
  const events = replay.events;
  trajectoryReplayButton.textContent = '播放动作阶段';
  const reference = trajectoryReference(replay.trajectory);
  if (!replay.available) {
    status.textContent = '暂无证据';
    status.className = 'status status-idle';
    track.innerHTML = `<p class="trajectory-empty">${escapeHtml(replay.reason || '本轮没有返回轨迹或动作事件，页面不会猜测路径。')}</p>`;
    trajectoryReplayButton.disabled = true;
    progress.textContent = '暂无可回放数据';
    note.textContent = '需要后端返回 /api/replay 的 events 或 trajectory 后才能回放。';
    return;
  }
  const syntheticReference = typeof replay.trajectory === 'string' && /^(mock|simulation):\/\//i.test(replay.trajectory.trim());
  status.textContent = reference ? (syntheticReference ? '流程引用·非实机' : '轨迹引用已记录') : '动作事件可回放';
  status.className = `status ${reference && !syntheticReference ? 'status-succeeded' : 'status-rejected'}`;
  if (events.length) {
    track.innerHTML = events.map((action, index) => {
      const tone = trajectoryActionTone(action?.status);
      const duration = Number(action?.duration_s);
      const durationText = Number.isFinite(duration) ? `${duration.toFixed(2)} s` : '时长未提供';
      const detail = action?.message || action?.error_code || durationText;
      return `<article class="trajectory-step ${tone === 'complete' ? 'is-complete' : tone === 'failed' ? 'is-failed' : ''}" data-replay-index="${index}"><span class="trajectory-dot">${index + 1}</span><div><strong>${escapeHtml(trajectoryActionLabel(action, index))}</strong><small>${escapeHtml(detail)}</small></div><span class="trajectory-step-status">${escapeHtml(statusLabel(action?.status || 'pending'))}</span></article>`;
    }).join('');
  } else {
    track.innerHTML = '<p class="trajectory-empty">已收到轨迹引用，但没有可展开的动作事件。</p>';
  }
  trajectoryReplayButton.disabled = !events.length;
  progress.textContent = events.length ? `${events.length} 个动作事件 · ${reference ? '含轨迹引用' : '无轨迹文件'}` : '仅有轨迹引用';
  const replaySource = monitorSourceLabel(replay.dataSource)[0];
  note.textContent = reference
    ? `来源：${replaySource}。轨迹引用：${reference}${syntheticReference ? '（流程引用，非实机轨迹）' : ''}。页面只展示后端已返回的记录，不会重新执行。`
    : `来源：${replaySource}。未收到可绘制的连续轨迹；当前仅回放动作事件顺序，不代表 TCP 路径最优。`;
}

/*
 * The public Simulation mode is a small, deterministic Python workcell.  It
 * returns scene objects and TCP samples with every experiment, so the browser
 * can show what was actually evaluated instead of drawing a generic robot
 * animation.  This is intentionally a 2-D projection (x/y with z encoded by
 * vertical lift); the labels make clear that it is not a URDF or camera view.
 */
function virtualNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function virtualPoint(value) {
  if (Array.isArray(value) && value.length >= 3) {
    const point = value.slice(0, 3).map((item) => Number(item));
    return point.every(Number.isFinite) ? point : null;
  }
  if (value && typeof value === 'object') {
    const point = ['x', 'y', 'z'].map((key) => Number(value[key]));
    return point.every(Number.isFinite) ? point : null;
  }
  return null;
}

function virtualTrajectoryPoints(record) {
  const result = recordResult(record);
  const simulation = result.simulation || {};
  const direct = Array.isArray(simulation.tcp_trajectory)
    ? simulation.tcp_trajectory.map(virtualPoint).filter(Boolean)
    : [];
  if (direct.length >= 2) return direct;
  const replay = replayData(record);
  return (replay.trajectory?.points || [])
    .map((point) => virtualPoint(point.tcp_position_m))
    .filter(Boolean);
}

function virtualSceneObjects(record) {
  const result = recordResult(record);
  const simulation = result.simulation || {};
  if (Array.isArray(simulation.scene_objects) && simulation.scene_objects.length) {
    return simulation.scene_objects;
  }
  return [];
}

function virtualObjectPose(object) {
  return virtualPoint(object?.pose_m || object?.pose || object);
}

function renderVirtualWorkcell(record) {
  const node = byId('virtual-workcell');
  if (!node) return false;
  const source = sourceOf(record);
  if (source !== 'simulation' || !record) {
    node.hidden = true;
    node.innerHTML = '';
    return false;
  }
  const points = virtualTrajectoryPoints(record);
  const objects = virtualSceneObjects(record);
  if (points.length < 2 || !objects.length) {
    node.hidden = true;
    node.innerHTML = '';
    return false;
  }

  const result = recordResult(record);
  const simulation = result.simulation || {};
  const replay = replayData(record);
  const target = objects.find((item) => item.kind === 'target');
  const destination = objects.find((item) => item.kind === 'destination');
  const obstacle = objects.find((item) => item.kind === 'obstacle');
  const xmin = 0.10; const xmax = 0.40;
  const ymin = -0.20; const ymax = 0.20;
  const plot = { left: 42, top: 35, width: 520, height: 250 };
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const sx = (value) => plot.left + ((clamp(value, xmin, xmax) - xmin) / (xmax - xmin)) * plot.width;
  /* z is projected upward so a raised transit path is visually distinct. */
  const sy = (value, z = 0) => plot.top + plot.height - ((clamp(value, ymin, ymax) - ymin) / (ymax - ymin)) * plot.height - clamp(z, 0, 0.30) * 100;
  const pointString = points.map(([x, y, z]) => `${sx(x).toFixed(1)},${sy(y, z).toFixed(1)}`).join(' ');
  const status = String(result.status || 'unknown');
  const pathClass = status === 'succeeded' ? 'virtual-path-success' : 'virtual-path-failed';
  const scenario = simulation.evaluation?.scenario || replay.scenario || 'unknown';
  const zValues = points.map((point) => point[2]);
  const zMin = Math.min(...zValues); const zMax = Math.max(...zValues);
  const zLabel = `z ${zMin.toFixed(2)}–${zMax.toFixed(2)} m`;

  const rectFor = (item, className, fallbackSize) => {
    const pose = virtualObjectPose(item);
    if (!pose) return '';
    const size = Array.isArray(item?.size_m) ? item.size_m : fallbackSize;
    const width = Math.max(8, Number(size?.[0] || fallbackSize[0]) * (plot.width / (xmax - xmin)));
    const height = Math.max(6, Number(size?.[1] || fallbackSize[1]) * (plot.height / (ymax - ymin)));
    const x = sx(pose[0]) - width / 2;
    const y = sy(pose[1], pose[2]) - height / 2;
    const label = item.kind === 'target' ? '红色方块' : '目标区域';
    return `<g class="${className}"><rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${width.toFixed(1)}" height="${height.toFixed(1)}" rx="3"></rect><text x="${(x + width / 2).toFixed(1)}" y="${(y - 6).toFixed(1)}" text-anchor="middle">${escapeHtml(label)}</text></g>`;
  };

  let obstacleMarkup = '';
  if (obstacle && Array.isArray(obstacle.min_m) && Array.isArray(obstacle.max_m)) {
    const min = obstacle.min_m.map(Number); const max = obstacle.max_m.map(Number);
    if (min.length >= 3 && max.length >= 3 && min.every(Number.isFinite) && max.every(Number.isFinite)) {
      const x = sx(min[0]); const y = sy(max[1], 0);
      const width = Math.max(8, sx(max[0]) - sx(min[0]));
      const height = Math.max(8, sy(min[1], 0) - sy(max[1], 0));
      obstacleMarkup = `<g class="virtual-obstacle"><rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${width.toFixed(1)}" height="${height.toFixed(1)}" rx="2"></rect><text x="${(x + width / 2).toFixed(1)}" y="${(y + height / 2 + 3).toFixed(1)}" text-anchor="middle">障碍物</text></g>`;
    }
  }
  const sampleStride = Math.max(1, Math.ceil(points.length / 18));
  const samples = points.filter((_, index) => index % sampleStride === 0 || index === points.length - 1)
    .map(([x, y, z]) => `<circle class="virtual-trace-point" cx="${sx(x).toFixed(1)}" cy="${sy(y, z).toFixed(1)}" r="2.8"><title>x ${x.toFixed(3)} · y ${y.toFixed(3)} · z ${z.toFixed(3)} m</title></circle>`).join('');
  const current = points[points.length - 1];
  const start = points[0];
  const grid = [0.10, 0.20, 0.30, 0.40].map((value) => `<line class="virtual-grid-line" x1="${sx(value).toFixed(1)}" x2="${sx(value).toFixed(1)}" y1="${plot.top}" y2="${plot.top + plot.height}"></line><text class="virtual-axis-label" x="${sx(value).toFixed(1)}" y="${plot.top + plot.height + 18}" text-anchor="middle">${value.toFixed(2)}</text>`).join('');
  const horizontal = [-0.20, 0, 0.20].map((value) => `<line class="virtual-grid-line" x1="${plot.left}" x2="${plot.left + plot.width}" y1="${sy(value, 0).toFixed(1)}" y2="${sy(value, 0).toFixed(1)}"></line><text class="virtual-axis-label" x="${plot.left - 8}" y="${(sy(value, 0) + 3).toFixed(1)}" text-anchor="end">${value.toFixed(2)}</text>`).join('');
  const outcomeLabel = status === 'succeeded' ? 'P1 成功' : status === 'failed' ? 'P0 失败 / 待反馈' : statusLabel(status);
  node.innerHTML = `<div class="virtual-workcell-head"><span>VIRTUAL WORKCELL · ${escapeHtml(String(simulation.runtime_name || 'roboscientist.virtual_workcell'))}</span><strong>非实机 · ${escapeHtml(outcomeLabel)}</strong></div><svg viewBox="0 0 640 360" role="img" aria-label="纯 Python 虚拟工作台二维轨迹投影"><defs><marker id="virtual-arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="currentColor"></path></marker></defs><rect class="virtual-table" x="${plot.left}" y="${plot.top}" width="${plot.width}" height="${plot.height}" rx="4"></rect>${grid}${horizontal}${obstacleMarkup}${rectFor(target, 'virtual-target', [0.04, 0.04])}${rectFor(destination, 'virtual-destination', [0.08, 0.08])}<polyline class="${pathClass}" points="${pointString}" marker-end="url(#virtual-arrow)"></polyline>${samples}<circle class="virtual-start" cx="${sx(start[0]).toFixed(1)}" cy="${sy(start[1], start[2]).toFixed(1)}" r="5"></circle><circle class="virtual-current" cx="${sx(current[0]).toFixed(1)}" cy="${sy(current[1], current[2]).toFixed(1)}" r="6"></circle><text class="virtual-label" x="${(sx(start[0]) + 8).toFixed(1)}" y="${(sy(start[1], start[2]) - 8).toFixed(1)}">起点</text><text class="virtual-label" x="${(sx(current[0]) + 8).toFixed(1)}" y="${(sy(current[1], current[2]) - 8).toFixed(1)}">末端</text><text class="virtual-axis-title" x="${plot.left + plot.width / 2}" y="${plot.top + plot.height + 37}" text-anchor="middle">x / m</text><text class="virtual-axis-title" x="12" y="${plot.top + plot.height / 2}" text-anchor="middle" transform="rotate(-90 12 ${plot.top + plot.height / 2})">y / m</text><g class="virtual-meta"><text x="584" y="54">场景：${escapeHtml(String(scenario))}</text><text x="584" y="72">${escapeHtml(zLabel)}</text><text x="584" y="90">采样：${points.length} 点</text><text x="584" y="108">状态：${escapeHtml(statusLabel(status))}</text><line x1="584" y1="132" x2="604" y2="132" class="virtual-legend-success"></line><text x="610" y="136">轨迹</text><rect x="584" y="148" width="20" height="10" class="virtual-legend-obstacle"></rect><text x="610" y="157">障碍</text></g></svg><div class="virtual-workcell-foot"><span>二维投影：x / y；高度 z 通过抬升显示</span><span>data_source=simulation · physical_robot=false</span></div>`;
  node.hidden = false;
  return true;
}

function renderMotionMonitor(record = null) {
  const source = record ? sourceOf(record) : selectedMode();
  const runtime = state.runtimeMode === source ? state.runtime : null;
  const bridge = monitorBridgeStatus(source, runtime);
  const motion = monitorMotionStatus(source, runtime);
  const preflight = preflightStatus(record);
  const stop = stopStatus(source, runtime);
  const sourceMeta = monitorSourceLabel(source);
  const sourceBadge = byId('motion-data-source-badge');
  const topStatus = byId('motion-view-status');
  const viewport = byId('motion-viewport');
  const viewportMessage = byId('motion-viewport-message');
  const liveFrame = byId('motion-live-frame');
  const viewKind = byId('motion-view-kind');
  const viewConnection = byId('motion-view-connection');
  const viewOverline = byId('motion-view-overline');
  const viewTitle = byId('motion-view-title');
  const viewDescription = byId('motion-view-description');
  const frameSource = byId('motion-frame-source');
  const frameNote = byId('motion-frame-note');
  if (!viewport || !sourceBadge || !topStatus) return;

  sourceBadge.textContent = `DATA SOURCE · ${sourceMeta[0]}`;
  sourceBadge.className = `source-badge ${modeLabel(source)[1]}`;
  setMonitorTile('motion-data-source', monitorStatus(sourceMeta[0], source === 'real_arm' ? 'warn' : 'idle', sourceMeta[1]));
  setMonitorTile('motion-bridge-status', bridge);
  setMonitorTile('motion-motion-status', motion);
  setMonitorTile('motion-preflight-status', preflight);
  setMonitorTile('motion-stop-status', stop);
  byId('motion-runtime-age').textContent = state.runtimeError
    ? '读取失败' : state.runtime ? '刚刚刷新' : '尚未刷新';
  byId('motion-disclosure').querySelector('p').textContent = state.runtimeError
    ? `运行时读取失败：${state.runtimeError}。当前仅显示保守状态，不会推断设备可用。`
    : source === 'real_arm'
      ? '实机画面只有在 runtime 明确返回安全的视频地址且 bridge 健康时才显示；停止能力“已配置”不等于“动作中已实测”。'
      : source === 'simulation'
        ? '画面是项目自带纯 Python 虚拟工作台的二维轨迹投影；它展示本轮实际返回的场景与评价，不连接实机。'
        : 'Mock 只展示软件流程和动作事件；它不会连接机械臂，也不能作为物理抓取证据。';

  const streamUrl = runtimeStreamUrl(runtime);
  const canShowVirtual = source === 'simulation' && renderVirtualWorkcell(record);
  const canShowLive = Boolean(streamUrl)
    && runtimeIsBridgeReady(source, runtime)
    && source !== 'mock';
  if (canShowLive) {
    const virtualWorkcell = byId('virtual-workcell');
    if (virtualWorkcell) virtualWorkcell.hidden = true;
    viewport.className = 'motion-viewport is-live has-live-frame';
    liveFrame.src = streamUrl;
    liveFrame.hidden = false;
    viewKind.textContent = 'LIVE VIEW';
    viewConnection.textContent = '实时流已提供';
    viewConnection.className = 'status status-succeeded';
    topStatus.textContent = '实时画面已提供';
    topStatus.className = 'status status-succeeded';
    viewOverline.textContent = 'LIVE / CONNECTED';
    viewTitle.textContent = '实时相机画面';
    viewDescription.textContent = '画面地址由运行时明确返回；页面仅观察，不发送运动命令。';
    frameSource.textContent = urlSummary(streamUrl);
    frameNote.textContent = '仅显示运行时返回的视频流';
  } else if (canShowVirtual) {
    liveFrame.hidden = true;
    liveFrame.removeAttribute('src');
    viewport.className = 'motion-viewport is-virtual';
    viewKind.textContent = 'VIRTUAL VIEW';
    viewConnection.textContent = '本地运行时已就绪';
    viewConnection.className = 'status status-succeeded';
    topStatus.textContent = '虚拟工作台已就绪';
    topStatus.className = 'status status-succeeded';
    viewOverline.textContent = 'SIMULATION / VERIFIED';
    viewTitle.textContent = '纯 Python 虚拟工作台';
    viewDescription.textContent = '显示本轮返回的目标、障碍物、TCP 轨迹和评价结果；不连接实机。';
    frameSource.textContent = 'roboscientist.virtual_workcell';
    frameNote.textContent = 'data_source=simulation · 非实机';
  } else {
    viewport.className = 'motion-viewport is-unavailable';
    liveFrame.hidden = true;
    liveFrame.removeAttribute('src');
    viewKind.textContent = source === 'real_arm' ? 'LIVE VIEW' : 'READ-ONLY VIEW';
    viewConnection.textContent = record ? '历史记录模式' : '未连接';
    viewConnection.className = `status ${record ? 'status-rejected' : 'status-idle'}`;
    if (source === 'real_arm') {
      viewOverline.textContent = 'LIVE / UNAVAILABLE';
      viewTitle.textContent = '实时实机画面不可用';
      viewDescription.textContent = bridge.tone === 'blocked'
        ? '实机 bridge 尚未通过健康检查。页面不会因为选择了 Real Arm 就假定机械臂在线。'
        : '运行时没有返回可验证的视频地址。请由硬件侧提供 stream_url 后再显示画面。';
      topStatus.textContent = '实时实机不可用';
      topStatus.className = 'status status-rejected';
      frameSource.textContent = streamUrl ? '视频地址已返回，但运行时未就绪' : '未提供视频地址';
      frameNote.textContent = '不播放占位视频';
    } else if (source === 'simulation') {
      viewOverline.textContent = 'SIMULATION / READY';
      viewTitle.textContent = '等待虚拟实验';
      viewDescription.textContent = '运行 P0 或 P0→P1 闭环后，页面会显示实际返回的虚拟场景与轨迹。';
      topStatus.textContent = '等待虚拟实验';
      topStatus.className = 'status status-idle';
      frameSource.textContent = '尚未生成虚拟轨迹';
      frameNote.textContent = '纯 Python · 非实机';
    } else {
      viewOverline.textContent = 'MOCK / READ-ONLY';
      viewTitle.textContent = '无实时画面 · Mock 仅显示动作阶段';
      viewDescription.textContent = '流程 Mock 用于验证计划、归因和版本演进；它不生成视频，也不代表真实机械臂动作。';
      topStatus.textContent = '只读流程视图';
      topStatus.className = 'status status-idle';
      frameSource.textContent = 'Mock 没有视频源';
      frameNote.textContent = '不播放占位视频';
    }
  }
  viewport.setAttribute('aria-label', viewTitle.textContent);
  if (viewportMessage) viewportMessage.hidden = canShowLive || canShowVirtual;
  renderTrajectory(record);
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
    : '本轮没有仿真执行证据（或不是 Simulation 模式）。';
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

function validationMetric(value, unit = '', digits = 3) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '-';
  return `${number.toFixed(digits)}${unit ? ` ${unit}` : ''}`;
}

function renderValidation(payload) {
  const validation = payload?.validation || payload || {};
  state.validation = validation;
  const statusNode = byId('validation-status');
  const output = byId('validation-output');
  if (!statusNode || !output) return;
  const comparison = validation.comparison || {};
  const summary = validation.summary || {};
  const p0 = summary.p0 || {};
  const p1 = summary.p1 || {};
  if (validation.status === 'blocked' || !summary.p0 || !summary.p1) {
    statusNode.textContent = validation.reason || '重复验证未完成，未生成足够样本。';
    statusNode.className = 'validation-status is-blocked';
    output.innerHTML = `<p class="validation-empty">${escapeHtml(validation.reason || '需要同时得到 P0 与 P1 的结构化记录。')}</p>`;
    return;
  }
  const decision = comparison.decision || 'candidate_only_insufficient_samples';
  const decisionText = {
    promote_candidate_for_review: '达到内部重复验证门槛：建议人工复核后再晋升',
    candidate_only_insufficient_samples: '候选仅供观察：样本数不足，不能宣称稳定提升',
    reject_candidate: '候选未通过：未形成满足约束的可重复改善',
  }[decision] || decision;
  const tone = decision === 'promote_candidate_for_review' ? 'is-good' : decision === 'reject_candidate' ? 'is-bad' : 'is-warn';
  statusNode.textContent = `${decisionText} · ${validation.repeats_per_version || p0.sample_count || 0} 次/版本 · data_source=${validation.data_source || 'simulation'}`;
  statusNode.className = `validation-status ${tone}`;
  const rows = [
    ['任务完成率', validationMetric(p0.task_success_rate, '', 1), validationMetric(p1.task_success_rate, '', 1)],
    ['位置误差均值', validationMetric(p0.position_error_mean_m, 'm'), validationMetric(p1.position_error_mean_m, 'm')],
    ['路径长度均值', validationMetric(p0.path_length_mean_m, 'm'), validationMetric(p1.path_length_mean_m, 'm')],
    ['执行时间均值', validationMetric(p0.execution_time_mean_s, 's'), validationMetric(p1.execution_time_mean_s, 's')],
    ['碰撞率', validationMetric(p0.collision_rate, '', 1), validationMetric(p1.collision_rate, '', 1)],
  ];
  output.innerHTML = `<div class="validation-decision ${tone}"><strong>${escapeHtml(decisionText)}</strong><span>${escapeHtml(comparison.reason || '')}</span></div><table><thead><tr><th>指标</th><th>P0 · ${Number(p0.sample_count || 0)} 次</th><th>P1 · ${Number(p1.sample_count || 0)} 次</th></tr></thead><tbody>${rows.map((row) => `<tr><th>${escapeHtml(row[0])}</th><td>${escapeHtml(row[1])}</td><td>${escapeHtml(row[2])}</td></tr>`).join('')}</tbody></table><p class="validation-footnote">同一虚拟适配器、场景、任务和评价器；该统计是软件虚拟实验，不是实机成功率。</p>`;
}

function render(record) {
  if (!record) return;
  updateSource(record);
  updateFacts(record);
  renderMotionMonitor(record);
  renderLoop();
  renderLineage();
  renderComparison();
  if (state.validation) renderValidation(state.validation);
  iterateButton.disabled = !candidateUnderTest();
  syncConditionControls();
}

function clearExperimentView() {
  const firstHistorical = state.replayCatalog.find((run) => run.status === 'available');
  if (firstHistorical && byId('trajectory-source-select')) {
    state.selectedReplay = firstHistorical.run_id;
    byId('trajectory-source-select').value = firstHistorical.run_id;
  }
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
  state.validation = null;
  const validationStatus = byId('validation-status');
  const validationOutput = byId('validation-output');
  if (validationStatus) {
    validationStatus.textContent = '运行后生成可复算的成功率、误差、路径和安全指标。';
    validationStatus.className = 'validation-status';
  }
  if (validationOutput) validationOutput.innerHTML = '';
  renderMotionMonitor(null);
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
  state.validation = campaign.validation || null;
  return campaign;
}

function monitorRecordForSelectedMode() {
  const record = last(state.history);
  return record && sourceOf(record) === selectedMode() ? record : null;
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
  state.runtimeMode = mode;
  state.runtime = null;
  state.runtimeError = null;
  renderMotionMonitor(monitorRecordForSelectedMode());
  try {
    const runtime = await request(`/api/runtime?mode=${encodeURIComponent(mode)}`);
    state.runtime = runtime;
    state.runtimeMode = mode;
    const capabilities = mode === 'real_arm' ? [
      ['桥接', runtime.available && runtime._bridge_mode === 'real_motion'],
      ['停止', runtime.stop_available === true],
      ['结果评估', runtime.result_evaluator_configured === true],
      ['候选参数', runtime.parameterized_skill_configured === true],
    ] : mode === 'simulation' ? [
      ['虚拟运行时', runtime.runtime_status === 'simulation_runtime_verified'],
      ['结构化轨迹', runtime.virtual === true],
      ['物理机械臂', runtime.physical_robot_connected === true],
    ] : [['流程适配器', mode === 'mock']];
    byId('runtime-capabilities').innerHTML = capabilities.map(([label, ready]) => `<span class="${ready ? 'is-ready' : 'is-blocked'}">${escapeHtml(label)} · ${ready ? '就绪' : '未就绪'}</span>`).join('');
    renderMotionMonitor(monitorRecordForSelectedMode());
  } catch (error) {
    state.runtime = null;
    state.runtimeError = error.message;
    byId('runtime-capabilities').innerHTML = `<span class="is-blocked">运行时不可用 · ${escapeHtml(error.message)}</span>`;
    renderMotionMonitor(monitorRecordForSelectedMode());
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
    // Keep the first-load preview reproducible and offline-safe.  A configured
    // provider means the integration is available; it does not mean a live
    // network call should happen without an explicit user choice.
    toggle.checked = false;
    byId('qwen-control').classList.toggle('is-unavailable', !configured);
    byId('qwen-control-help').textContent = configured
      ? `${qwen.provider || '百炼'} · ${model} · 默认关闭，勾选后才调用`
      : '未配置 DASHSCOPE_API_KEY，闭环不会静默降级';
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

async function refreshReplay() {
  const select = byId('trajectory-source-select');
  if (!select) return;
  try {
    const catalog = await request('/api/replay');
    state.replayCatalog = Array.isArray(catalog?.runs) ? catalog.runs : [];
    updateReplaySourceOptions();
    const first = state.replayCatalog.find((run) => run.status === 'available');
    if (!first) {
      select.disabled = !last(state.history);
      if (!state.history.length) byId('system-state').textContent = '等待实验或历史回放';
      renderTrajectory(last(state.history));
      return;
    }
    const runId = select.value === '__current__' ? first.run_id : select.value;
    if (!state.replayRuns[runId]) {
      state.replayRuns[runId] = await request(`/api/replay/${encodeURIComponent(runId)}`);
    }
    state.historicalReplay = state.replayRuns[runId];
    if (state.selectedReplay !== '__current__') {
      state.selectedReplay = runId;
      select.value = runId;
    }
    if (!state.history.length) byId('system-state').textContent = `历史实机已载入 · ${runId}`;
    renderTrajectory(last(state.history));
  } catch (error) {
    state.replayCatalog = [];
    state.historicalReplay = null;
    updateReplaySourceOptions();
    const detail = byId('trajectory-source-detail');
    if (detail) detail.textContent = `历史回放接口不可用 · ${error.message}`;
    if (!state.history.length) byId('system-state').textContent = '历史回放不可用，等待实验';
    renderTrajectory(last(state.history));
  }
}

async function selectReplaySource() {
  const select = byId('trajectory-source-select');
  if (!select) return;
  state.selectedReplay = select.value;
  stopTrajectoryReplay();
  if (select.value !== '__current__' && !state.replayRuns[select.value]) {
    select.disabled = true;
    try {
      state.replayRuns[select.value] = await request(`/api/replay/${encodeURIComponent(select.value)}`);
      state.historicalReplay = state.replayRuns[select.value];
    } catch (error) {
      state.historicalReplay = null;
      const detail = byId('trajectory-source-detail');
      if (detail) detail.textContent = `历史运行包读取失败 · ${error.message}`;
    } finally {
      select.disabled = false;
    }
  }
  renderTrajectory(last(state.history));
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  state.history = [];
  state.campaign = null;
  state.lastStop = null;
  clearExperimentView();
  setBusy(true, '正在运行 P0...');
  try {
    const record = await request('/api/tasks', { task_text: byId('task-text').value, scenario: selectedScenario(), mode: selectedMode() });
    state.history = [record];
    state.selectedReplay = '__current__';
    if (byId('trajectory-source-select')) byId('trajectory-source-select').value = '__current__';
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
  state.lastStop = null;
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
    state.selectedReplay = '__current__';
    if (byId('trajectory-source-select')) byId('trajectory-source-select').value = '__current__';
    render(last(state.history));
    renderValidation(state.validation);
  } catch (error) {
    byId('current-evidence').innerHTML = `<span class="field-label">闭环未完成</span><p>${escapeHtml(visibleError(error))}</p>`;
    byId('system-state').textContent = '闭环请求失败，未静默降级';
  } finally { setBusy(false); }
});

validationButton?.addEventListener('click', async () => {
  if (selectedMode() === 'real_arm') {
    const statusNode = byId('validation-status');
    if (statusNode) {
      statusNode.textContent = '无实机路线不执行 Real Arm 重复验证；请切换到 Simulation。';
      statusNode.className = 'validation-status is-blocked';
    }
    return;
  }
  validationButton.disabled = true;
  const statusNode = byId('validation-status');
  if (statusNode) {
    statusNode.textContent = '正在运行 10 次 P0 + 10 次 P1；每次均写入独立证据。';
    statusNode.className = 'validation-status is-running';
  }
  try {
    const payload = await request('/api/validation', {
      task_text: byId('task-text').value,
      scenario: selectedScenario(),
      mode: 'simulation',
      repeats: 10,
      use_qwen: false,
    });
    await loadCampaignIfNeeded(payload);
    state.selectedReplay = '__current__';
    if (byId('trajectory-source-select')) byId('trajectory-source-select').value = '__current__';
    render(last(state.history));
    renderValidation(state.validation || payload.validation);
  } catch (error) {
    if (statusNode) {
      statusNode.textContent = `重复验证失败：${visibleError(error)}`;
      statusNode.className = 'validation-status is-blocked';
    }
  } finally {
    validationButton.disabled = false;
  }
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
  byId('scenario-fieldset').classList.toggle('is-disabled', false);
  state.lastStop = null;
  renderMotionMonitor(monitorRecordForSelectedMode());
  refreshRuntime();
}));

byId('use-qwen').addEventListener('change', () => {
  byId('qwen-control-help').textContent = byId('use-qwen').checked ? '两次结构化调用：规划 + 反馈；证据会写入 campaign' : '确定性流程模式；不会伪造 Qwen 证据';
});

trajectoryReplayButton?.addEventListener('click', playTrajectoryReplay);
byId('trajectory-source-select')?.addEventListener('change', selectReplaySource);
byId('hardware-baseline-select')?.addEventListener('change', (event) => {
  state.selectedHardwareBaseline = event.target.value || null;
  renderHardwareBaseline(state.hardwareBaselines.find((item) => item.baseline_id === state.selectedHardwareBaseline) || null);
});

renderMotionMonitor();
refreshConfig();
refreshRuntime();
refreshReplay();
refreshHardwareEvidence();
