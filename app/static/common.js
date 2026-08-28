async function api(path, body) {
  const opts = body === undefined
    ? {}
    : {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify(body)};
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function mask(matric) {
  if (!matric || matric.length <= 5) return matric || '';
  return matric.slice(0, 4) + '•'.repeat(matric.length - 5) + matric.slice(-1);
}

function hhmm(iso) {
  return iso && iso.length >= 16 ? iso.slice(11, 16) : '';
}

function renderRelease(btn, counters) {
  if (!btn) return;
  const on = !!counters.release_mode;
  btn.textContent = on ? `Release: on · ${counters.released}` : 'Release: off';
  btn.className = 'pill' + (on ? ' alert' : '');
  btn.title = on
    ? 'Scans identify the student and stop — no table, nothing written to the '
      + 'sheet. Click to go back to checking people in.'
    : 'Click to stop seating: scans will say confirmed / not confirmed and stop.';
}

async function toggleRelease(btn, after) {
  const on = btn.classList.contains('alert');
  btn.disabled = true;
  try {
    const r = await api('/api/release-mode', {on: !on});
    renderRelease(btn, r.counters);
  } finally {
    btn.disabled = false;
  }
  if (after) await after();
}

function poll(fn, ms) {
  let busy = false;
  const tick = async () => {
    if (busy) return;
    busy = true;
    try { await fn(); } catch (e) { console.error(e); } finally { busy = false; }
  };
  tick();
  return setInterval(tick, ms);
}
