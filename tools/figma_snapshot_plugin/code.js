// rfc-agent Figma snapshot — exports screens as JSON_REST_V1 + PNG and POSTs each to the local receiver.
// Plugin API only: no REST, no MCP, no quota. Runs in the main thread; `fetch` is a sandbox global.
//
// Two receiver modes (the receiver says which on /health):
//  - plan mode (rfc-agent prep): send a cheap INVENTORY of the screens first (names, sections, sizes — no
//    images), keep running while the operator picks sections in the terminal, then poll /plan and export
//    only the picked frames. One keypress, and the export is proportional to what is actually needed.
//  - direct mode (`python -m rfc_agent.figma_snapshot serve`): export everything found, as before.
//
// What counts as a screen: FRAME / COMPONENT / INSTANCE / GROUP at least MIN_SIDE px on both sides.
// SECTIONs and COMPONENT_SETs are containers and are walked (nested too). Flow arrows, connector lines,
// text labels, icons and header strips are skipped — on a real handoff page they were ~1/3 of all nodes.

// Figma's manifest validator rejects raw-IP origins ("must be a valid URL") — localhost only,
// matching manifest.json allowedDomains. The receiver binds 127.0.0.1, which serves localhost.
const BASE = 'http://localhost:8790';
const MAX_PX = 4096; // longest PNG side; 2x for phone frames, downscaled for wide canvases
const MIN_SIDE = 200;
const SCREEN_TYPES = { FRAME: 1, COMPONENT: 1, INSTANCE: 1, GROUP: 1 };
const CONTAINER_TYPES = { SECTION: 1, COMPONENT_SET: 1, PAGE: 1 };
const PLAN_WAIT_MS = 30 * 60 * 1000;

figma.showUI(__html__, { width: 360, height: 220 });

function say(type, extra) {
  figma.ui.postMessage(Object.assign({ type }, extra || {}));
}

async function post(path, body) {
  const r = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(path + ' -> HTTP ' + r.status);
  return r.json();
}

function sectionOf(n) {
  for (let p = n.parent; p; p = p.parent) {
    if (p.type === 'SECTION') return p.name;
    if (p.type === 'PAGE') break;
  }
  return null;
}

function pageOf(n) {
  for (let p = n; p; p = p.parent) if (p.type === 'PAGE') return p;
  return figma.currentPage;
}

function isScreen(n) {
  return SCREEN_TYPES[n.type] && (n.width || 0) >= MIN_SIDE && (n.height || 0) >= MIN_SIDE;
}

function collect(nodes, out, section) {
  for (const n of nodes) {
    if (n.visible === false) continue;
    if (CONTAINER_TYPES[n.type]) {
      collect(Array.from(n.children || []), out, n.type === 'SECTION' ? n.name : section);
    } else if (isScreen(n) && typeof n.exportAsync === 'function') {
      out.push({ n, section: section || sectionOf(n) });
    }
  }
  return out;
}

async function roots(ids) {
  say('status', { text: 'Connected. Loading the linked node…' });
  // ids come from the receiver (the PRD links' node-ids): export exactly what a link points at, whatever
  // the operator has selected. A node not in this file belongs to another link's file — skip it.
  const found = [];
  for (const id of ids || []) {
    const n = await figma.getNodeByIdAsync(id);
    if (n) {
      await pageOf(n).loadAsync();
      found.push({ id, n });
    }
  }
  if (found.length) return found;
  await figma.currentPage.loadAsync();
  const sel = Array.from(figma.currentPage.selection);
  if ((ids || []).length) {
    // the PRD's linked node is not in the file this plugin runs in (another tab, a branch, a duplicate)
    if (!sel.length) {
      throw new Error('the linked node ' + ids.join(', ') + ' is not in this file ("' + figma.root.name +
        '"). Switch to the tab rfc-agent opened from the PRD link and run the plugin there — or select the ' +
        'frames you want in this file and run it again.');
    }
    say('status', { text: 'Linked node not in "' + figma.root.name + '" — using your selection (' + sel.length + ' nodes).' });
    return [{ id: null, n: null, sel, fallback: true }];
  }
  return [{ id: null, n: sel.length ? null : figma.currentPage, sel }];
}

async function exportItems(target, items) {
  let failures = 0;
  for (let i = 0; i < items.length; i++) {
    const n = items[i].n;
    const pg = pageOf(n);
    say('progress', { done: i, total: items.length, name: n.name });
    try {
      const json = await n.exportAsync({ format: 'JSON_REST_V1' });
      let png_b64 = '';
      let scale = 0;
      try {
        scale = Math.min(2, MAX_PX / Math.max(n.width || 1, n.height || 1));
        const png = await n.exportAsync({ format: 'PNG', constraint: { type: 'SCALE', value: scale } });
        png_b64 = figma.base64Encode(png);
      } catch (e) {
        png_b64 = ''; // JSON still ships; the digest marks the render as missing
      }
      await post('/frame', {
        target,
        page: { id: pg.id, name: pg.name },
        node: { id: n.id, name: n.name, type: n.type, w: Math.round(n.width || 0), h: Math.round(n.height || 0),
                section: items[i].section },
        scale,
        json,
        png_b64,
      });
    } catch (e) {
      failures++;
      say('warn', { text: 'Failed: ' + n.name + ' (' + n.id + '): ' + (e && e.message) });
    }
    // yield so the editor stays responsive on big pages (plugins run on the main thread)
    await new Promise((r) => setTimeout(r, 0));
  }
  return failures;
}

async function waitForPlan(target) {
  const t0 = Date.now();
  while (Date.now() - t0 < PLAN_WAIT_MS) {
    const r = await fetch(BASE + '/plan?target=' + encodeURIComponent(target || ''));
    const body = await r.json();
    if (body.ids || body.skip) return body;
    await new Promise((res) => setTimeout(res, 1000));
  }
  return { skip: true };
}

async function run() {
  let health;
  try {
    const r = await fetch(BASE + '/health');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    health = await r.json();
  } catch (e) {
    say('error', { text: 'Receiver not reachable at ' + BASE + '. Start it first (rfc-agent prep, or\n' +
      'python -m rfc_agent.figma_snapshot serve <out_dir> --url <figma url>)' });
    return;
  }
  const found = await roots(health.targets);
  for (const root of found) {
    const target = root.id || '';
    say('status', { text: 'Finding screens under ' + (root.n ? root.n.name + ' (' + root.n.type + ')' : 'the selection') + '…' });
    const items = root.n ? collect([root.n], [], null) : collect(root.sel, [], null);
    const page = items.length ? pageOf(items[0].n) : figma.currentPage;
    if (!items.length) {
      say('error', { text: 'No screens found under ' + (target || 'this page') + '.' });
      if (health.plan) await post('/inventory', { target, file: figma.root.name, frames: [] });
      continue;
    }
    let todo = items;
    if (health.plan) {
      say('status', { text: 'Sending the list of ' + items.length + ' screens…' });
      await post('/inventory', {
        target, fallback: !!root.fallback, file: figma.root.name, page: { id: page.id, name: page.name },
        frames: items.map((t) => ({ id: t.n.id, name: t.n.name, type: t.n.type, section: t.section,
                                    page: pageOf(t.n).name, w: Math.round(t.n.width || 0),
                                    h: Math.round(t.n.height || 0) })),
      });
      say('waiting', { total: items.length });
      const plan = await waitForPlan(target);
      if (plan.skip) {
        say('done', { total: 0, failures: 0, skipped: true });
        continue;
      }
      const want = new Set(plan.ids);
      todo = items.filter((t) => want.has(t.n.id));
    }
    const failures = await exportItems(target, todo);
    await post('/done', { target, file: figma.root.name, page: { id: page.id, name: page.name }, failures });
    say('done', { total: todo.length, failures });
    figma.notify('rfc-agent snapshot: ' + (todo.length - failures) + '/' + todo.length + ' screens exported');
  }
}

run().catch((e) => {
  // never leave the panel on "Connecting…" — show what broke, and tell the receiver (it logs to prep)
  const text = 'Plugin error: ' + ((e && e.message) || e);
  say('error', { text });
  fetch(BASE + '/log', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify({ text }) }).catch(() => {});
});
