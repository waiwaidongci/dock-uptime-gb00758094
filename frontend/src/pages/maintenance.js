import {api, download} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {planStates} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';

export async function maintenance() {
  const [datasets, plans] = await Promise.all([api('/datasets'), api('/maintenance/plans')]);
  const canEdit = state.account.role === 'analyst';
  const month = state.planMonth || '2026-10';
  const datasetOptions = datasets.length
    ? datasets.map(row => `<option value="${row.id}" ${row.id === state.datasetId ? 'selected' : ''}>${esc(row.name)} · #${row.id}</option>`).join('')
    : '';
  const builder = canEdit && datasets.length ? `
    <form id="plan-form" class="inline-form plan-builder">
      <label>数据版本<select name="dataset_id">${datasetOptions}</select></label>
      <label>排程月份<input name="month" value="${esc(month)}" pattern="[0-9]{4}-[0-9]{2}" required placeholder="YYYY-MM"></label>
      <div class="order-rows" id="order-rows">
        <div class="order-row">
          <input name="record_id" placeholder="保养单号，如 WO-001" required maxlength="60">
          <input name="dock" placeholder="月台，如 D01" required maxlength="40">
          <input name="minutes" type="number" min="1" max="44640" placeholder="时长（分钟）" required>
          <button type="button" class="secondary remove-row">删除</button>
        </div>
      </div>
      <div class="form-actions">
        <button type="button" id="add-row" class="secondary">再加一单</button>
        <button type="submit">整批排程</button>
      </div>
      ${errorSlot}
    </form>` : empty(datasets.length ? '分析员可提交整批保养排程。' : '先导入数据版本。', '#1', '查看数据台账');
  const history = table(plans.map(row => ({
    计划: {link: `#6/${row.id}`, text: `#${row.id}`},
    月份: row.month,
    数据版本: row.dataset_id,
    状态: planStates[row.state] ?? row.state,
    提交人: row.owner,
    提交时间: time(row.created),
    版本: row.checksum.slice(0, 10),
  })));
  return `<section>
    <h2>整批保养单排程</h2>
    <p class="hint">每张保养单连续完成、时长不变，只排在所选月份台班内并避开故障；同一时段全仓只修一座，维修期间至少保留一座在班且未停机的月台接货。整批排不下时仅返回冲突单号，不保存部分安排。</p>
    ${builder}
  </section>
  <section><h2>排程记录（按数据版本与批次去重）</h2>${history}</section>`;
}

export async function maintenanceDetail(planId) {
  const plan = await api(`/maintenance/plans/${planId}`);
  const result = plan.result;
  const conflictBlock = result.feasible ? '' : `
    <section class="conflict"><h2>冲突单号（${result.conflicts.length}）</h2>
      <p class="error" role="alert">${esc(result.headline)}</p>
      ${table(result.conflicts.map(row => ({
        保养单号: row['保养单号'], 月台: row['月台'], '时长（分钟）': row['时长（分钟）'], 冲突原因: row['冲突原因'],
      })))}
    </section>`;
  const scheduleBlock = result.feasible ? `
    <section><h2>逐单安排</h2>
      ${table(result.rows.map(row => ({
        保养单号: row['保养单号'], 月台: row['月台'], '时长（分钟）': row['时长（分钟）'],
        '开始（含）': row['开始（含）'], '结束（不含）': row['结束（不含）'],
      })))}
      <button id="plan-download" class="secondary" type="button" data-id="${plan.id}">下载排程 CSV</button>
      <p id="plan-error" class="error" role="alert"></p>
    </section>` : '';
  const metrics = Object.entries(result.metrics)
    .map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value ?? '—')}</dd></div>`).join('');
  const exactBadge = result.feasible
    ? (result.exact ? '<span class="badge badge-exact">已证明最早完工</span>'
      : '<span class="badge badge-heuristic">贪心可行解（未证明最优）</span>')
    : '';
  return `<section>
    <p><a href="#6">← 返回整批保养排程</a></p>
    <h2 class="${result.feasible ? '' : 'error'}">${esc(result.headline)} ${exactBadge}</h2>
    <p class="hint">计划 #${plan.id} · 月份 ${esc(plan.month)} · 数据版本 #${plan.dataset_id} · 状态 ${planStates[plan.state] ?? plan.state}</p>
    <dl class="metrics">${metrics}</dl>
    ${scheduleBlock}
    ${conflictBlock}
    <h3>口径与限制</h3>
    <ul>${result.notes.map(note => `<li>${esc(note)}</li>`).join('')}</ul>
  </section>`;
}

function rowTemplate() {
  return `<div class="order-row">
    <input name="record_id" placeholder="保养单号，如 WO-001" required maxlength="60">
    <input name="dock" placeholder="月台，如 D01" required maxlength="40">
    <input name="minutes" type="number" min="1" max="44640" placeholder="时长（分钟）" required>
    <button type="button" class="secondary remove-row">删除</button>
  </div>`;
}

export function bindMaintenance(root, refresh) {
  const addButton = root.querySelector('#add-row');
  const orderRows = root.querySelector('#order-rows');
  addButton?.addEventListener('click', () => {
    orderRows.insertAdjacentHTML('beforeend', rowTemplate());
  });
  orderRows?.addEventListener('click', event => {
    if (!event.target.classList.contains('remove-row')) return;
    if (orderRows.children.length > 1) event.target.closest('.order-row').remove();
  });
  bindForm(root, '#plan-form', async form => {
    const ids = [...form.querySelectorAll('[name=record_id]')].map(input => input.value.trim());
    const docks = [...form.querySelectorAll('[name=dock]')].map(input => input.value.trim());
    const minutes = [...form.querySelectorAll('[name=minutes]')].map(input => Number(input.value));
    const orders = ids.map((record_id, index) => ({record_id, dock: docks[index], minutes: minutes[index]}));
    const payload = {dataset_id: Number(form.elements.dataset_id.value), month: form.elements.month.value.trim(), orders};
    state.planMonth = payload.month;
    const saved = await api('/maintenance/plans', payload);
    state.status = saved.created ? '排程已生成。' : '相同数据版本与批次已有排程，已复用原结果，未新增记录。';
    location.hash = `6/${saved.id}`;
  }, refresh);
  root.querySelector('#plan-download')?.addEventListener('click', async event => {
    const key = event.currentTarget.dataset.id;
    try { await download(`/maintenance/plans/${key}/report.csv`, `保养排程-${key}.csv`); }
    catch (error) { root.querySelector('#plan-error').textContent = error.message; }
  });
}
