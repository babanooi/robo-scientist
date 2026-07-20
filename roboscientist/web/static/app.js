const form = document.querySelector('#task-form');
const runButton = document.querySelector('#run-button');
const iterateButton = document.querySelector('#iterate-button');
let currentExperimentId = null;

const pretty = (value) => JSON.stringify(value, null, 2);

function selectedScenario() {
  return document.querySelector('input[name="scenario"]:checked').value;
}

function selectedMode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

function setBusy(isBusy) {
  runButton.disabled = isBusy;
  runButton.textContent = isBusy ? '正在运行...' : '提交实验';
}

function render(record) {
  const { plan, result, analysis, candidate_skill: candidate } = record;
  currentExperimentId = result.experiment_id;
  document.querySelector('#result-title').textContent = result.status === 'succeeded' ? '实验已完成' : '实验结果需要处理';
  document.querySelector('#mode-label').textContent = result.data_source.toUpperCase();
  document.querySelector('#mode-notice').textContent = record.mode_notice;
  const status = document.querySelector('#run-status');
  status.textContent = result.status.toUpperCase();
  status.className = `status status-${result.status}`;
  const facts = document.querySelectorAll('#run-facts dd');
  facts[0].textContent = result.experiment_id;
  facts[1].textContent = result.data_source;
  facts[2].textContent = result.skill_version;
  facts[3].textContent = result.scene_id;
  facts[4].textContent = result.hardware_status;
  document.querySelector('#plan-output').textContent = pretty({ task: plan.task, target_pose: plan.target_pose, safety_constraints: plan.safety_constraints, safety_check: result.safety_check });
  document.querySelector('#result-output').textContent = pretty({ status: result.status, outcome: result.outcome, failure: result.failure, analysis, metrics: result.metrics });
  document.querySelector('#simulation-output').textContent = result.simulation ? pretty(result.simulation) : '当前适配器未产生 Gazebo/MoveIt2 仿真遥测。';
  document.querySelector('#skill-output').textContent = candidate ? pretty(candidate) : '此轮没有候选 Skill。';
  iterateButton.disabled = !candidate;
}

async function request(url, body) {
  const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || '请求失败');
  return payload;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  setBusy(true);
  try {
    render(await request('/api/tasks', { task_text: document.querySelector('#task-text').value, scenario: selectedScenario(), mode: selectedMode() }));
  } catch (error) {
    document.querySelector('#result-title').textContent = error.message;
  } finally {
    setBusy(false);
  }
});

iterateButton.addEventListener('click', async () => {
  if (!currentExperimentId) return;
  iterateButton.disabled = true;
  try {
    render(await request(`/api/experiments/${currentExperimentId}/iterate`, { scenario: selectedScenario(), mode: selectedMode() }));
  } catch (error) {
    document.querySelector('#result-title').textContent = error.message;
  }
});
