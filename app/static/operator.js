const $ = (id) => document.getElementById(id);
let state = null;
let lastCheckedIn = null;

function showResult(r, typed) {
  if (r.outcome === 'ambiguous' || r.outcome === 'refused' || !r.entry) {
    $('manualPanel').hidden = false;
  }
  const box = $('result');
  box.className = r.colour || '';
  clear(box);
  clear($('candidates'));

  const e = r.entry;
  if (r.outcome === 'ambiguous') {
    box.append(el('div', {class: 'who', text: `${r.candidates.length} students match "${typed}"`}),
               el('div', {class: 'sub', text: 'Tap the right one.'}));
    for (const c of r.candidates) {
      $('candidates').append(el('button', {
        class: 'small', onclick: () => submit(c.matric),
        text: `${c.name} · ${mask(c.matric)}${c.checked_in ? ' (already in)' : ''}`}));
    }
    return;
  }

  if (!e) {
    box.append(el('div', {class: 'who', text: typed ? `"${typed}" not recognised` : 'Refused'}),
               el('div', {class: 'sub', text: r.reason || ''}),
               el('div', {class: 'sub', text:
                 'They have to be in the Google Form. Ask them to fill it in, '
                 + 'then check them in again.'}));
    return;
  }

  box.append(
    el('div', {class: 'who', text: e.name || '(no name on file)'}),
    el('div', {class: 'sub', text:
      `${mask(e.matric)}  ·  ${e.confirmed ? 'confirmed' : 'not confirmed'}` +
      (e.allocated_session ? `  ·  allocated to session ${e.allocated_session}` : '') +
      (e.method ? `  ·  ${e.method}` : '')}),
    el('div', {class: 'verdict' + (r.display.length > 8 ? ' small' : ''), text: r.display}));

  if (r.reason && (r.colour !== 'green' || r.outcome === 'released')) {
    box.append(el('div', {class: 'sub', text: r.reason}));
  }
  if (r.outcome === 'already') box.append(el('div', {class: 'sub', text: 'already checked in'}));

  if (r.outcome !== 'refused') {
    lastCheckedIn = e.matric;
    $('candidates').append(el('button', {
      class: 'small danger', text: `Undo ${e.name || e.matric}`,
      onclick: () => undo(e.matric)}));
  }
}

function renderCounters(c) {
  const groups = c.group_size > 1 ? ` (${c.projected_groups} group${c.projected_groups === 1 ? '' : 's'} of ${c.group_size})` : '';
  $('projection').innerHTML =
    `next table <b>${c.next_table === null ? 'full' : c.next_table}</b>` +
    ` &middot; at finalise &rarr; <b>${c.projected_seated}</b> seated${groups}` +
    (c.projected_dismissed ? `, <b>${c.projected_dismissed}</b> dismissed` : '') +
    (c.finalised ? ' &middot; <b>finalised</b>' : '');
}

function rows(target, header, data, render) {
  const t = $(target);
  clear(t);
  if (!data.length) { t.append(el('tr', {}, el('td', {class: 'empty', text: 'nobody yet'}))); return; }
  t.append(el('tr', {}, header.map((h) => el('th', {text: h}))));
  for (const d of data) t.append(render(d));
}

const STUDENT_HOLD_MS = 15000;
let studentSeq = -1;
let studentHideAt = 0;
const STUDENT_IDLE = $('student').innerHTML;

function studentIdle() {
  studentHideAt = 0;
  $('student').className = 'student';
  $('student').innerHTML = STUDENT_IDLE;
  $('camNote').hidden = true;
  clear($('camNote'));
}

function studentMessage(d) {
  switch (d.outcome) {
    case 'seated':
      return {head: 'Checked in — thank you!', big: d.display,
              sub: 'Please take a seat at this table and wait there. '
                 + 'The experiment will start shortly.'};
    case 'already':
      return {head: 'You are already checked in', big: d.display,
              sub: 'This is your table — please go back to it.'};
    case 'waiting':
      return {head: 'Thank you — you are on the waiting list', big: d.display,
              sub: 'Please wait to one side. We will call you if a seat becomes free.'};
    case 'released':
      return {head: 'Thank you for coming', big: d.display,
              sub: 'There is no seat for you in this session. '
                 + 'Please speak to the experimenter.'};
    case 'refused':
      return {head: 'Sorry, we could not check you in', big: '',
              sub: 'Please speak to the experimenter. '
                 + 'They will sort it out with you.'};
    default:
      return {head: 'One moment, please', big: d.display || '',
              sub: 'The experimenter is checking your details.'};
  }
}

function messageNodes(m) {
  const nodes = [el('div', {class: 'head', text: m.head})];
  if (m.big) {
    nodes.push(el('div', {class: 'verdict' + (m.big.length > 8 ? ' small' : ''),
                          text: m.big}));
  }
  nodes.push(el('div', {class: 'sub', text: m.sub}));
  return nodes;
}

function renderStudent(d) {
  if (!d || d.seq === studentSeq) return;
  studentSeq = d.seq;
  const left = d.display ? STUDENT_HOLD_MS - (d.age || 0) * 1000 : 0;
  if (left <= 0) { studentIdle(); return; }
  studentHideAt = Date.now() + left;
  const m = studentMessage(d);
  const colour = d.colour || '';

  $('student').className = 'student ' + colour;
  clear($('student'));
  $('student').append(...messageNodes(m));

  const note = $('camNote');
  note.className = 'camnote ' + colour;
  clear(note);
  note.append(el('div', {class: 'card'}, ...messageNodes(m)));
  note.hidden = false;
}

setInterval(() => {
  if (studentHideAt && Date.now() > studentHideAt) studentIdle();
}, 300);

$('camNote').addEventListener('click', studentIdle);
$('camNote').title = 'click to clear';

function render(s) {
  state = s;
  renderStudent(s.display);
  const c = s.counters;
  $('sessionLabel').textContent = `Session ${s.session}` + (c.label ? ` · ${c.label}` : '');
  const sw = $('roomSwitch');
  const spans = s.desk_rooms || [];
  clear(sw);
  if (spans.length > 1) {
    sw.append(el('span', {class: 'label', text: 'seat in'}));
    for (const r of spans) {
      const on = s.desk_room === r.room;
      sw.append(el('button', {
        class: 'pill' + (on ? ' on' : ''),
        text: `room ${r.room} · ${r.start}–${r.end}`,
        title: on
          ? `Arrivals are being seated in room ${r.room}. Click to seat them anywhere again.`
          : `Seat the next arrivals at tables ${r.start}–${r.end}. `
            + 'Tables skipped in the other rooms stay free and are used later.',
        onclick: async (ev) => {
          ev.target.disabled = true;
          await api('/api/rooms/desk', {room: on ? null : r.room});
          await refresh();
        }}));
    }
    sw.append(el('span', {class: 'label', text:
      s.desk_room ? '' : 'any room — filling in order'}));
  }
  renderCounters(c);
  $('invariants').textContent = (s.invariants || []).join(' | ');

  const sheet = s.sheet;
  $('sheetPill').textContent = !sheet.enabled ? 'sheet off'
    : `sheet queue ${sheet.queue_depth}`;
  $('sheetPill').className = 'pill' + (sheet.enabled ? ' on' : '');

  renderRelease($('relToggle'), c);

  $('releasedPanel').hidden = !c.release_mode && !s.released.length;
  $('relCount').textContent = s.released.length ? `(${s.released.length})` : '';

  rows('released', ['at', 'name', 'matric', 'registration', ''], s.released, (e) =>
    el('tr', {}, el('td', {class: 'num', text: hhmm(e.arrival_time)}),
       el('td', {text: e.name || '(walk-in)'}), el('td', {text: mask(e.matric)}),
       el('td', {class: e.confirmed ? 'note' : 'warn', text: e.registration}),
       el('td', {}, el('button', {class: 'small danger', text: 'undo',
                                  onclick: () => undo(e.matric)}))));

  const allocated = s.waiting.filter((w) => !w.walk_in);
  const walkins = s.waiting.filter((w) => w.walk_in);

  $('waitCount').textContent = allocated.length ? `(${allocated.length})` : '';
  $('seatCount').textContent = `(${s.seated.length} of ${s.settings.total_tables})`;
  const next = allocated[0];
  const noSeat = c.next_table === null || c.next_table === undefined;
  const t = s.settings.total_tables;
  const g = s.settings.group_size;
  const lastRoom = !s.desk_room || s.desk_room === (s.desk_rooms || []).length;
  const growText = !lastRoom
    ? `room ${s.desk_room} is full — switch rooms above, or undo someone`
    : g > 1
      ? `every table is taken: this will add tables ${t + 1}–${t + g} (the next group) and seat them there`
      : `every table is taken: this will add table ${t + 1} and seat them there`;
  $('promoteHint').textContent = !next
    ? (walkins.length ? 'waiting list is empty — walk-ins are listed below'
                      : 'waiting list is empty')
    : (noSeat
       ? `next: ${next.name || '(walk-in)'} ${mask(next.matric)} — ${growText}`
       : `next: ${next.name || '(walk-in)'} ${mask(next.matric)} → table ${c.next_table}`)
      + (allocated.length > 1 ? `  ·  ${allocated.length - 1} more waiting` : '');
  $('promote').disabled = !next || (noSeat && !lastRoom);

  const waitRow = (w) =>
    el('tr', {}, el('td', {class: 'num', text: w.position}),
       el('td', {text: w.name || '(walk-in)'}), el('td', {text: mask(w.matric)}),
       el('td', {class: 'note', text: w.note}),
       el('td', {}, el('button', {class: 'small danger', text: 'undo', onclick: () => undo(w.matric)})));

  rows('waiting', ['#', 'name', 'matric', 'why', ''], allocated, waitRow);

  $('walkinPanel').hidden = !walkins.length;
  $('walkinCount').textContent = walkins.length ? `(${walkins.length})` : '';
  const nextW = walkins[0];
  $('admitHint').textContent = !nextW ? ''
    : (noSeat
       ? `next: ${nextW.name || '(walk-in)'} ${mask(nextW.matric)} — ${growText}`
       : `next: ${nextW.name || '(walk-in)'} ${mask(nextW.matric)} → table ${c.next_table}`)
      + (walkins.length > 1
         ? `  ·  ${walkins.length - 1} more walk-in${walkins.length === 2 ? '' : 's'}` : '')
      + (allocated.length ? `  ·  ${allocated.length} allocated still waiting above` : '');
  $('admitAnyway').disabled = !nextW || (noSeat && !lastRoom);

  rows('walkins', ['#', 'name', 'matric', 'why', ''], walkins, waitRow);

  rows('seated', ['table', 'name', 'matric', 'in', ''], s.seated, (e) =>
    el('tr', {class: e.confirmed ? 'confirmed' : ''},
       el('td', {class: 'num', text: e.table}), el('td', {text: e.name || '(walk-in)'}),
       el('td', {text: mask(e.matric)}), el('td', {class: 'num', text: hhmm(e.arrival_time)}),
       el('td', {}, el('button', {class: 'small danger', text: 'undo', onclick: () => undo(e.matric)}))));

}

async function refresh() { render(await api('/api/state')); }

async function submit(query) {
  const q = (query || '').trim();
  if (!q) return;
  try {
    const r = await api('/api/checkin', {query: q, method: 'manual'});
    showResult(r, q);
  } catch (err) {
    showResult({colour: 'red', outcome: 'refused', reason: err.message}, q);
  }
  $('query').value = '';
  await refresh();
  $('query').focus();
}

async function undo(matric) {
  await api('/api/undo', {matric});
  if (lastCheckedIn === matric) lastCheckedIn = null;
  const box = $('result');
  box.className = '';
  clear(box);
  clear($('candidates'));
  box.append(el('div', {class: 'who', text: 'Undone'}),
             el('div', {class: 'sub', text: `${mask(matric)} removed; the table is back in the pool.`}));
  await refresh();
  $('query').focus();
}

let stream = null;
let scanning = false;
const canvas = document.createElement('canvas');

function camAlert(kind, text) {
  const box = $('camAlert');
  box.className = 'camalert' + (kind ? ' ' + kind : '');
  box.textContent = text || '';
  box.hidden = !text;
}

function cameraFailure(err) {
  const named = {
    NotFoundError: 'No camera is connected. Plug one in and press Start camera '
                 + '— or just type the last 4 digits instead.',
    NotAllowedError: 'The browser is blocking the camera. Allow camera access '
                   + 'for this page, then press Start camera. Typing still works.',
    NotReadableError: 'Another app is using the camera (Teams, Zoom, the Camera '
                    + 'app). Close it and press Start camera. Typing still works.',
    OverconstrainedError: 'That camera cannot do 1280x720. Pick another one from '
                        + 'the list, or type the last 4 digits.',
    SecurityError: 'The camera only works on http://127.0.0.1 or http://localhost, '
                 + 'never on a 192.168.x.x address.',
  };
  return named[err && err.name] ||
    `The camera did not open (${(err && err.message) || 'unknown error'}). `
    + 'Manual entry below still works.';
}

async function listCameras(selectedId) {
  const devices = (await navigator.mediaDevices.enumerateDevices())
    .filter((d) => d.kind === 'videoinput');
  const sel = $('camDevice');
  clear(sel);
  devices.forEach((d, i) => sel.append(
    el('option', {value: d.deviceId, text: d.label || `Camera ${i + 1}`})));
  if (selectedId) sel.value = selectedId;
  $('camToggle').disabled = !devices.length && !scanning;
  if (!scanning) {
    if (!devices.length) {
      camAlert('bad', 'No camera found on this machine. Check-in still works — '
                    + 'type the last 4 digits of the matric instead.');
    } else {
      camAlert('', 'Camera is off. Press Start camera.');
    }
  }
  return devices;
}

async function startCamera() {
  const deviceId = $('camDevice').value;
  camAlert('', 'Opening the camera…');
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: deviceId ? {deviceId: {exact: deviceId}, width: 1280, height: 720}
                      : {width: 1280, height: 720}});
  } catch (err) {
    $('camStatus').textContent = 'unavailable';
    camAlert('bad', cameraFailure(err));
    $('camRead').textContent = '';
    return;
  }
  stream.getVideoTracks()[0].addEventListener('ended', () => {
    if (!scanning) return;
    stopCamera();
    camAlert('bad', 'The camera was disconnected. Plug it back in and press '
                  + 'Start camera — typing the last 4 digits still works.');
  });
  $('cam').srcObject = stream;
  $('cam').addEventListener('loadedmetadata', () => {
    const v = $('cam');
    if (v.videoWidth && v.videoHeight) {
      $('camwrap').style.aspectRatio = `${v.videoWidth} / ${v.videoHeight}`;
    }
  }, {once: true});
  $('camwrap').classList.add('live');
  $('camStatus').textContent = 'live';
  $('camToggle').textContent = 'Stop camera';
  scanning = true;
  await listCameras(stream.getVideoTracks()[0].getSettings().deviceId);
  camAlert('', '');
  await api('/api/scanner/reset', {});
  scanLoop();
}

function stopCamera() {
  scanning = false;
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  $('cam').srcObject = null;
  $('camwrap').classList.remove('live', 'reading');
  $('camStatus').textContent = 'off';
  $('camToggle').textContent = 'Start camera';
  $('camStats').textContent = '';
  $('camRead').textContent = '';
  camAlert('', 'Camera is off. Press Start camera.');
}

async function scanLoop() {
  const video = $('cam');
  let frames = 0, sumMs = 0, gated = 0;
  let lostServer = false;
  while (scanning) {
    if (!video.videoWidth) { await new Promise((r) => setTimeout(r, 120)); continue; }
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    const blob = await new Promise((r) => canvas.toBlob(r, 'image/jpeg', 0.8));

    const t0 = performance.now();
    let r;
    try {
      r = await (await fetch('/api/frame', {
        method: 'POST', headers: {'Content-Type': 'image/jpeg'}, body: blob})).json();
    } catch (err) {
      camAlert('bad', 'Lost the check-in server — is it still running? '
                    + 'Retrying every half second.');
      $('camRead').textContent = 'lost the server: ' + err.message;
      await new Promise((res) => setTimeout(res, 500));
      lostServer = true;
      continue;
    }
    if (lostServer) {
      lostServer = false;
      camAlert('', '');
    }
    const ms = performance.now() - t0;
    frames++; sumMs += ms;
    if (!r.passed_gate) gated++;

    $('camStats').className = 'note';
    $('camStats').textContent =
      `${(1000 / (sumMs / frames)).toFixed(1)} fps · ${Math.round(ms)} ms · ` +
      `${frames} frames · ${gated} skipped`;
    $('camwrap').classList.toggle('reading', Boolean(r.match && r.match.best));
    const idish = (r.texts || []).filter((t) => /[A-Za-z0-9]{4,}/.test(t));
    const read = $('camRead');
    read.className = r.elsewhere ? 'warn' : 'note';
    read.textContent = r.elsewhere
        ? `${r.elsewhere.name} (${mask(r.elsewhere.matric)}) is allocated to session ` +
          `${r.elsewhere.session}, not this one — going on the walk-in list`
      : !r.passed_gate ? r.gate_reason
      : r.match && r.match.accepted
        ? `read ${r.match.token} → ${r.match.best} (d ${r.match.distance})`
      : r.match ? `read ${r.match.token} — ${r.match.reason}`
      : idish.length ? `no ID-shaped text (${idish.slice(0, 3).join(', ')})`
      : 'nothing in frame';

    if (r.checkin) {
      showResult(r.checkin, r.checkin.entry ? r.checkin.entry.matric : '');
      await refresh();
    }
  }
}

$('camToggle').addEventListener('click', () => (scanning ? stopCamera() : startCamera()));
$('camDevice').addEventListener('change', async () => {
  if (scanning) { stopCamera(); await startCamera(); }
});
listCameras().catch(() => {
  $('camStatus').textContent = 'no camera API';
  $('camToggle').disabled = true;
  camAlert('bad', 'This browser will not give the page a camera. Check the '
                + 'address is 127.0.0.1 or localhost, not 192.168.x.x. '
                + 'Check-in by typing the last 4 digits still works.');
});
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange',
    () => listCameras($('camDevice').value).catch(() => {}));
}

$('query').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') { ev.preventDefault(); submit($('query').value); }
  if (ev.key === 'Escape') {
    if ($('query').value === '') { $('manualPanel').hidden = true; $('query').blur(); }
    $('query').value = '';
  }
});
$('relToggle').addEventListener('click', () => toggleRelease($('relToggle'), refresh));
$('undoLast').addEventListener('click', () => lastCheckedIn && undo(lastCheckedIn));
async function promoteFrom(pool, verb, listName) {
  const r = await api('/api/promote', {limit: 1, pool, grow: true});
  await refresh();
  const box = $('result');
  const p = r.promoted[0];
  box.className = p ? 'blue' : 'amber';
  clear(box);
  const nxt = r.next_in_queue;
  const grew = !r.grown ? ''
    : r.grown.added > 1
      ? `added tables ${r.grown.first}–${r.grown.last} — put the number cards out. `
      : `added table ${r.grown.first} — put the number card out. `;
  box.append(
    el('div', {class: 'who', text: p
      ? `${p.name || '(walk-in)'} ${mask(p.matric)} → TABLE ${p.table}`
      : `nobody was ${verb}`}),
    el('div', {class: 'sub', text: p
      ? grew + (nxt ? `${verb}. Next: ${nxt.name || '(walk-in)'} ${mask(nxt.matric)}`
                    + ` — ${r.still_waiting} still on the ${listName}`
                    : `${verb}. The ${listName} is now empty.`)
      : (r.still_waiting ? 'no seat is free — undo someone, or add tables on /admin'
                         : `the ${listName} is empty`)}));
}
$('promote').addEventListener('click', () => promoteFrom('allocated', 'promoted', 'waiting list'));
$('admitAnyway').addEventListener('click', () => promoteFrom('walk_in', 'admitted', 'walk-in list'));

document.addEventListener('keydown', (ev) => {
  if (ev.ctrlKey && ev.key === 'z' && lastCheckedIn) { ev.preventDefault(); undo(lastCheckedIn); }
  if (!ev.ctrlKey && !ev.altKey && ev.key.length === 1 && document.activeElement !== $('query')) {
    $('manualPanel').hidden = false;
    $('query').focus();
  }
});

poll(refresh, 3000);
