'use strict';

const $ = (id) => document.getElementById(id);
let entities = [];
let selected = new Set();
let poll = null;

/* -- helpers ---------------------------------------------------------- */
async function api(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

function flash(message, isError) {
  const el = $('flash');
  el.textContent = message;
  el.className = isError ? 'flash error' : 'flash';
  el.hidden = false;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => { el.hidden = true; }, 6000);
}

/* -- watchlist -------------------------------------------------------- */
async function load() {
  const data = await api('/api/entities');
  entities = data.entities;
  // Everything active starts selected; a run with nothing ticked is never
  // what someone means by pressing the button.
  selected = new Set(entities.filter((e) => e.active).map((e) => e.id));
  render();
}

function visible() {
  const term = $('filter').value.trim().toLowerCase();
  return entities.filter((e) => !term || e.name.toLowerCase().includes(term));
}

function render() {
  const term = $('filter').value.trim().toLowerCase();
  const list = $('entities');
  const shown = visible();

  list.innerHTML = '';
  for (const e of shown) {
    const li = document.createElement('li');
    if (!e.active) li.classList.add('off');

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = selected.has(e.id);
    box.disabled = !e.active;
    box.addEventListener('change', () => {
      box.checked ? selected.add(e.id) : selected.delete(e.id);
      updateCount();
    });

    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = e.name;

    const badge = document.createElement('span');
    badge.className = 'badge' + (e.type === 'Other' ? ' other' : '');
    badge.textContent = e.type;

    const pins = document.createElement('span');
    pins.className = 'pins';
    const n = e.agencies_confirmed.length;
    pins.textContent = n ? `${n} confirmed` : '';
    if (n) pins.title = e.agencies_confirmed.join(', ');

    const del = document.createElement('button');
    del.className = 'remove';
    del.type = 'button';
    del.textContent = '×';
    del.title = `Remove ${e.name}`;
    del.addEventListener('click', () => remove(e));

    li.append(box, name, badge, pins, del);
    list.append(li);
  }

  $('empty').hidden = shown.length > 0;
  $('list-title').textContent =
    term ? `Entities (${shown.length} of ${entities.length})` : `Entities (${entities.length})`;
  updateCount();
}

function updateCount() {
  const n = selected.size;
  $('run-count').textContent = n || '';
  $('run').disabled = n === 0;
  // The box reflects the rows on screen, because that is what ticking it acts
  // on. Judging it against the whole list would leave it unticked while every
  // visible row is ticked, under a filter.
  const shown = visible().filter((e) => e.active);
  $('select-all').checked =
    shown.length > 0 && shown.every((e) => selected.has(e.id));
}

async function remove(e) {
  if (!confirm(`Remove ${e.name} from the watchlist?`)) return;
  await api(`/api/entities/${e.id}`, { method: 'DELETE' });
  flash(`Removed ${e.name}`);
  await load();
}

/* -- adding ----------------------------------------------------------- */
$('add-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const input = $('add-name');
  const name = input.value.trim();
  if (!name) return;
  try {
    const { entity, status } = await api('/api/entities', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const said = {
      added: `Added ${entity.name}`,
      exists: `${entity.name} is already on the list`,
      reactivated: `${entity.name} is back on the list`,
    }[status];
    flash(said, status === 'exists');
    input.value = '';
    await load();
  } catch (err) {
    flash(err.message, true);
  }
});

$('file').addEventListener('change', async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    const r = await api('/api/entities/upload', { method: 'POST', body: form });
    const bits = [`${r.added} added`];
    if (r.reactivated) bits.push(`${r.reactivated} reactivated`);
    if (r.already_present) bits.push(`${r.already_present} already present`);
    if (r.invalid) bits.push(`${r.invalid} unusable`);
    flash(bits.join(' · '));
    await load();
  } catch (err) {
    flash(err.message, true);
  } finally {
    ev.target.value = '';
  }
});

document.querySelector('label[for="file"]').addEventListener('click', () => $('file').click());

$('filter').addEventListener('input', render);

$('select-all').addEventListener('change', (ev) => {
  // Acts on the filtered view only: with 'municipal' typed, ticking the box
  // should take the three municipal bodies and leave the rest of the
  // selection alone, not quietly re-select all thirty-six.
  for (const e of visible()) {
    if (!e.active) continue;
    ev.target.checked ? selected.add(e.id) : selected.delete(e.id);
  }
  render();
});

/* -- running ---------------------------------------------------------- */
$('run').addEventListener('click', async () => {
  if (!selected.size) return;
  $('run').disabled = true;
  $('result').hidden = true;
  $('progress').hidden = false;
  setProgress(0, 'Starting…');
  try {
    const job = await api('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_ids: [...selected] }),
    });
    watch(job.id);
  } catch (err) {
    $('progress').hidden = true;
    $('run').disabled = false;
    flash(err.message, true);
  }
});

function setProgress(pct, label) {
  $('bar-fill').style.width = `${pct}%`;
  $('progress-pct').textContent = `${pct}%`;
  $('progress-label').textContent = label || '';
}

function watch(id) {
  clearInterval(poll);
  poll = setInterval(async () => {
    let job;
    try {
      job = await api(`/api/jobs/${id}`);
    } catch (err) {
      clearInterval(poll);
      $('progress').hidden = true;
      $('run').disabled = false;
      flash(err.message, true);
      return;
    }
    setProgress(job.percent, job.label);
    if (job.status === 'done' || job.status === 'failed') {
      clearInterval(poll);
      $('progress').hidden = true;
      $('run').disabled = false;
      job.status === 'done' ? showResult(job) : flash(job.error || 'The run failed', true);
      load();
    }
  }, 1000);
}

function showResult(job) {
  const s = job.summary || {};
  $('result').hidden = false;
  $('download').href = `/api/jobs/${job.id}/result`;
  $('result-title').textContent = 'Ratings collected';
  $('result-sub').textContent =
    `${s.companies_with_ratings ?? 0} of ${job.companies} entities · ratings within the last 15 months`;

  const stats = $('result-stats');
  stats.innerHTML = '';
  const add = (label, value) => {
    const el = document.createElement('span');
    el.className = 'stat';
    el.innerHTML = `${label} <b>${value}</b>`;
    stats.append(el);
  };
  add('Rows', s.records ?? 0);
  for (const [agency, n] of Object.entries(s.by_agency || {})) add(agency, n);
  const blank = job.companies - (s.companies_with_ratings ?? 0);
  if (blank > 0) add('No current rating', blank);
}

load().catch((err) => flash(err.message, true));
