const $ = (id) => document.getElementById(id);
let dirty = false;

let roomEls = {};
let roomsSig = '';

const roomKey = (r) => `${r.session}:${r.room}`;

function roomProblem(key, text) {
  const refs = roomEls[key];
  if (refs) refs.problem.textContent = text || '';
}

async function applyRoom(refs) {
  const r = refs.current;
  const wanted = Number(refs.session.value) || r.session;
  const sizes = {total_tables: Number(refs.tables.value) || null,
                 group_size: Number(refs.group.value) || null};
  try {
    if (wanted !== r.session) {
      if (r.active && r.room === 1) {
        await api('/api/session', {number: wanted});
        const res = await api('/api/settings', {session: wanted, room: 1,
          total_tables: Number(refs.tables.value) !== r.settings.total_tables
            ? sizes.total_tables : null,
          group_size: Number(refs.group.value) !== r.settings.group_size
            ? sizes.group_size : null});
        dirty = false;
        render(res.state);
        roomProblem(`${wanted}:1`, (res.problems || []).join(' '));
      } else {
        const res = await api('/api/rooms/rekey',
                              {session: r.session, room: r.room, to: wanted, ...sizes});
        dirty = false;
        render(res.state);
      }
      return;
    }
    const res = await api('/api/settings', {session: r.session, room: r.room, ...sizes});
    dirty = false;
    render(res.state);
    roomProblem(roomKey(r), (res.problems || []).join(' '));
  } catch (err) {
    roomProblem(roomKey(r), err.message);
  }
}

function buildRoom(r) {
  const refs = {current: r};
  refs.session = el('input', {type: 'number', min: '1', style: 'width:5rem'});
  refs.tables = el('input', {type: 'number', min: '1', max: '200', style: 'width:5rem'});
  refs.group = el('input', {type: 'number', min: '1', style: 'width:5rem'});
  for (const inp of [refs.session, refs.tables, refs.group]) {
    inp.addEventListener('input', () => { dirty = true; });
  }
  refs.problem = el('span', {class: 'warn'});
  refs.map = el('div', {class: 'map'});
  refs.note = el('div', {class: 'note', style: 'margin-top:.6rem'});

  const several = r.rooms_in_session > 1;
  const removable = !(r.active && r.room === 1);
  const head = el('div', {class: 'head-row'},
    el('h3', {text: `Room — session ${r.session}`
                    + (several ? ` · tables ${r.start}–${r.end}` : '')}),
    el('span', {class: 'grow'}),
    removable
      ? el('button', {class: 'small danger', text: 'remove',
           title: several
             ? "Take this room's tables out of the session"
             : 'Hide this block. Its check-ins and settings stay in the database.',
           onclick: async () => {
             try { render((await api('/api/rooms/remove',
                                     {session: refs.current.session,
                                      room: refs.current.room})).state); }
             catch (err) { roomProblem(roomKey(refs.current), err.message); }
           }})
      : null);

  const chips = el('div', {class: 'chips', style: 'margin-bottom:.6rem'},
    el('label', {class: 'note'}, 'session ', refs.session),
    el('label', {class: 'note'}, 'tables ', refs.tables),
    el('label', {class: 'note'}, 'group size ', refs.group),
    el('button', {text: 'Apply', onclick: () => applyRoom(refs)}),
    refs.problem);

  refs.panel = el('div', {class: 'panel'}, head, chips, refs.map, refs.note);
  return refs;
}

function renderRooms(s) {
  const sig = s.rooms.map((r) =>
    `${roomKey(r)}=${r.start}-${r.end}${r.active ? '*' : ''}`).join(',');
  if (sig !== roomsSig) {
    roomsSig = sig;
    roomEls = {};
    clear($('rooms'));
    for (const r of s.rooms) {
      roomEls[roomKey(r)] = buildRoom(r);
      $('rooms').append(roomEls[roomKey(r)].panel);
    }
  }
  const c = s.counters;
  for (const r of s.rooms) {
    const refs = roomEls[roomKey(r)];
    refs.current = r;
    clear(refs.map);
    for (const cell of r.table_map) {
      refs.map.append(el('div', {class: `cell ${cell.status}`},
        el('div', {class: 't', text: `table ${cell.table}${cell.group ? ` · g${cell.group}` : ''}`}),
        el('div', {class: 'n', text: cell.name || '—'})));
    }
    refs.note.textContent = (r.active && r.room === 1)
      ? `${c.seated} seated · ${c.waiting} waiting` +
        (c.waiting_walk_ins ? ` (${c.waiting_walk_ins} walk-in — Admit anyway on /operator)` : '') +
        ` · ${c.expected_not_arrived} expected but not arrived` +
        ` · at finalise ${c.projected_seated} seated, ${c.projected_dismissed} dismissed`
      : `${r.seated} seated · ${r.label}` +
        (r.starts_mid_group
          ? ` · starts mid-group — a group of ${r.settings.group_size} spans both rooms`
          : '');
    refs.note.className = r.starts_mid_group ? 'warn' : 'note';
    if (!dirty) {
      refs.session.value = r.session;
      refs.tables.value = r.settings.total_tables;
      refs.group.value = r.settings.group_size;
    }
  }
}

function render(s) {
  const c = s.counters;
  $('sessionLabel').textContent = `${c.label} · ${s.settings.total_tables} tables · group size ${s.settings.group_size}`;

  renderRooms(s);

  const r = s.roster;
  $('rosterNote').textContent = r.source;
  $('rosterCounts').textContent =
    `session ${s.session}: ${r.total} allocated, ` +
    `${r.confirmed} confirmed, ${r.unconfirmed} unconfirmed`;
  const stamp = hhmm(r.checked_at || r.at) || '—';
  $('rosterLive').textContent = r.auto
    ? `live · re-read every ${Math.round(r.every)}s · last ${stamp}`
    : 'not re-reading — press Reload roster after a correction';
  $('rosterLive').className = r.error ? 'warn' : (r.auto && r.live ? 'note' : 'warn');

  if (!dirty) $('sheetUrl').value = r.sheet.pasted || '';
  $('sheetLink').href = r.sheet.link || '#';
  $('sheetLink').hidden = !r.sheet.link;

  const issues = $('rosterIssues');
  clear(issues);
  if (r.error) issues.append(el('div', {class: 'warn', text: r.error}));
  for (const e of s.roster.errors) issues.append(el('div', {class: 'error', text: e}));
  for (const w of s.roster.confusable) {
    issues.append(el('div', {class: 'warn', text:
      `${w.a} and ${w.b} look alike (distance ${w.distance.toFixed(2)}) — expect to confirm these two by hand`}));
  }

  renderRelease($('relToggle'), c);

  const sh = s.sheet;
  const wb = s.writeback || {};
  $('sheetPill').textContent = !sh.enabled ? 'off'
    : (sh.queue_depth ? `${sh.queue_depth} queued` : 'live');
  $('sheetPill').className = 'pill' + (sh.enabled && !sh.last_error ? ' on' : '');
  $('flush').disabled = !sh.enabled;

  if (!dirty) {
    $('wbUrl').value = wb.url || '';
    $('wbMode').value = wb.mode || 'off';
    $('wbSecret').placeholder = wb.has_secret ? 'unchanged' : 'not set';
  }
  $('wbTarget').textContent = !wb.url
    ? 'no link set for this sheet — check-ins are written nowhere'
    : (wb.has_secret ? '' : 'no secret set — the script will refuse the batch');
  $('wbTarget').className = 'warn';
  const checkedIn = c.seated + c.waiting;
  const neverQueued = sh.enabled && checkedIn > 0 && !sh.queue_depth && !sh.sent;
  $('sheetNote').textContent = !sh.enabled
    ? `off — nothing is written. ${sh.queue_depth} row(s) are held and will be `
      + 'sent when you set mode to live above; nothing is lost meanwhile.'
    : `${sh.queue_depth} queued · ${sh.sent} sent` +
      (sh.parked ? ` · ${sh.parked} parked` : '') +
      (sh.retry_in ? ` · retrying in ${sh.retry_in}s` : '') +
      (sh.configured ? '' : ' · no link set') +
      (neverQueued
        ? ` — ${checkedIn} checked in but never queued: they arrived before the `
          + 'write-back was on, so the sheet will not hear about them. '
          + 'POST /api/sheet/resend re-queues every one of them.'
        : '');
  $('sheetNote').className = neverQueued ? 'warn' : 'note';
  const log = $('sheetLog');
  clear(log);
  if (sh.last_error) log.append(el('div', {class: 'error', text: sh.last_error}));
  for (const line of sh.history || []) log.append(el('div', {class: 'note', text: line}));

  const wait = $('waiting');
  clear(wait);
  wait.append(el('tr', {}, ['#', 'queue', 'name', 'matric', 'why', 'arrived'].map((h) => el('th', {text: h}))));
  for (const w of s.waiting) {
    wait.append(el('tr', {}, el('td', {class: 'num', text: w.position}),
      el('td', {class: w.walk_in ? 'warn' : 'note', text: w.tier}),
      el('td', {text: w.name || '(walk-in)'}), el('td', {text: mask(w.matric)}),
      el('td', {class: 'note', text: w.note}), el('td', {class: 'num', text: hhmm(w.arrival_time)})));
  }
  if (!s.waiting.length) wait.append(el('tr', {}, el('td', {class: 'empty', text: 'empty'})));

  const seat = $('seated');
  $('seatCount').textContent = `(${s.seated.length} of ${s.settings.total_tables})`;
  clear(seat);
  seat.append(el('tr', {}, ['table', 'group', 'subject', 'name', 'matric', 'how'].map((h) => el('th', {text: h}))));
  for (const e of s.seated) {
    seat.append(el('tr', {class: e.confirmed ? 'confirmed' : ''},
      el('td', {class: 'num', text: e.table}), el('td', {class: 'num', text: e.group || '—'}),
      el('td', {class: 'num', text: e.subject_id || '—'}), el('td', {text: e.name || '(walk-in)'}),
      el('td', {text: mask(e.matric)}), el('td', {class: 'note', text: `${e.status} · ${e.method}`})));
  }
  if (!s.seated.length) seat.append(el('tr', {}, el('td', {class: 'empty', text: 'nobody seated yet'})));
}

async function refresh() { render(await api('/api/state')); }

$('addRoom').addEventListener('click', async () => {
  const r = await api('/api/rooms/add', {});
  dirty = false;
  render(r.state);
});

$('relToggle').addEventListener('click', () => toggleRelease($('relToggle'), refresh));

$('flush').addEventListener('click', async () => {
  const r = await api('/api/sheet/flush', {});
  $('sheetNote').textContent = JSON.stringify(r.result);
  await refresh();
});

function showAdminResult(r, typed) {
  const box = $('adminResult');
  box.className = r.colour || '';
  clear(box);
  clear($('adminCandidates'));

  if (r.outcome === 'ambiguous') {
    box.append(el('div', {class: 'who', text: `${r.candidates.length} students match "${typed}"`}),
               el('div', {class: 'sub', text: 'Tap the right one.'}));
    for (const cand of r.candidates) {
      $('adminCandidates').append(el('button', {
        class: 'small', onclick: () => adminSubmit(cand.matric),
        text: `${cand.name} · ${mask(cand.matric)}${cand.checked_in ? ' (already in)' : ''}`}));
    }
    return;
  }

  const e = r.entry;
  if (!e) {
    box.append(el('div', {class: 'who', text: typed ? `"${typed}" not recognised` : 'Refused'}),
               el('div', {class: 'sub', text: r.reason || ''}),
               el('div', {class: 'sub', text:
                 'They have to be in the Google Form. Ask them to fill it in, '
                 + 'then check them in again.'}));
    return;
  }

  box.append(
    el('div', {class: 'who', text: r.display}),
    el('div', {class: 'sub', text:
      `${e.name || '(no name)'} · ${mask(e.matric)} · ` +
      `${e.confirmed ? 'confirmed' : 'not confirmed'}` +
      (e.allocated_session ? ` · allocated to session ${e.allocated_session}` : '') +
      (r.reason ? ` — ${r.reason}` : '')}));
  $('adminCandidates').append(el('button', {
    class: 'small danger', text: `Undo ${mask(e.matric)}`,
    onclick: async () => { await api('/api/undo', {matric: e.matric});
                           clear($('adminCandidates'));
                           $('adminResult').textContent = 'Undone — the table is back in the pool.';
                           await refresh(); }}));
}

async function adminSubmit(q) {
  const query = (q || '').trim();
  if (!query) return;
  try {
    showAdminResult(await api('/api/checkin', {query, method: 'manual'}), query);
  } catch (err) {
    showAdminResult({colour: 'red', outcome: 'refused', reason: err.message}, query);
  }
  $('adminQuery').value = '';
  await refresh();
  $('adminQuery').focus();
}

$('adminGo').addEventListener('click', () => adminSubmit($('adminQuery').value));
$('adminQuery').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') { ev.preventDefault(); adminSubmit($('adminQuery').value); }
  if (ev.key === 'Escape') $('adminQuery').value = '';
});

$('reload').addEventListener('click', async () => {
  const r = await api('/api/roster/reload', {});
  $('rosterNote').textContent = `${r.source} · ${r.rows} rows`;
  await refresh();
});

$('sheetUrl').addEventListener('input', () => { dirty = true; });
$('sheetUrl').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') { ev.preventDefault(); $('connect').click(); }
});

function sheetMsg(cls, text) {
  const box = $('sheetMsg');
  clear(box);
  if (text) box.append(el('div', {class: cls, text}));
}

$('connect').addEventListener('click', async () => {
  sheetMsg('note', 'reading that sheet…');
  try {
    const r = await api('/api/sheet/connect', {url: $('sheetUrl').value});
    dirty = false;
    render(r.state);
    const wb = r.connected.writeback;
    sheetMsg('note', `connected — ${r.connected.rows} rows, ` +
                     `sessions ${r.connected.sessions.join(', ')}.` +
                     (wb && wb.url
                       ? ' Its own write-back link came back with it.'
                       : ' It has no write-back link yet — set one below, or'
                         + ' check-ins are written nowhere.'));
  } catch (err) {
    sheetMsg('error', err.message);
  }
});

function wbMsg(cls, text) {
  const box = $('wbMsg');
  clear(box);
  if (text) box.append(el('div', {class: cls, text}));
}

for (const id of ['wbUrl', 'wbSecret', 'wbMode']) {
  $(id).addEventListener('input', () => { dirty = true; });
}

$('wbSave').addEventListener('click', async () => {
  wbMsg('note', 'saving…');
  try {
    const typed = $('wbSecret').value;
    const r = await api('/api/sheet/writeback', {
      url: $('wbUrl').value,
      mode: $('wbMode').value,
      secret: typed === '' ? null : typed});
    $('wbSecret').value = '';
    dirty = false;
    wbMsg('note', r.writeback.mode === 'off'
      ? 'saved — off. Nothing is written, and check-ins stay queued until you '
        + 'set this to live.'
      : 'saved — writing live to the sheet this roster comes from.');
    await refresh();
  } catch (err) {
    wbMsg('error', err.message);
  }
});

poll(refresh, 3000);
