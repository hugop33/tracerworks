// The dashboard page updates in place (#820): `morph`, taken from the page
// itself between its markers, run on a minimal DOM. The claim: a card that did
// not change keeps its node — so its scroll, focus and place — across a refresh.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// The page to test: `test_dashboard_page.py` says which (the package's own).
assert.ok(process.env.DASHBOARD_PAGE, 'DASHBOARD_PAGE: the page to test');
const page = readFileSync(process.env.DASHBOARD_PAGE, 'utf8');
const source = page.slice(page.indexOf('/* morph:begin'), page.indexOf('/* morph:end */'));
assert.ok(source.length > 100, 'the morph markers are in the page');
const morph = new Function(`${source}; return morph;`)();

/* Just the DOM surface `morph` touches, and `childNodes` as a plain array. */
class Node {
  constructor(nodeType, nodeName, nodeValue = null) {
    Object.assign(this, { nodeType, nodeName, nodeValue, childNodes: [], attributes: [], parentNode: null });
  }
  getAttribute(name) { const a = this.attributes.find(a => a.name === name); return a ? a.value : null; }
  setAttribute(name, value) {
    const a = this.attributes.find(a => a.name === name);
    if (a) a.value = value; else this.attributes.push({ name, value });
  }
  removeAttribute(name) { this.attributes = this.attributes.filter(a => a.name !== name); }
  insertBefore(node, ref) {
    if (node.parentNode) node.parentNode.removeChild(node);
    const at = ref ? this.childNodes.indexOf(ref) : this.childNodes.length;
    assert.ok(at >= 0, 'the reference is a child');
    this.childNodes.splice(at, 0, node); node.parentNode = this; return node;
  }
  removeChild(node) {
    const at = this.childNodes.indexOf(node);
    assert.ok(at >= 0, 'the removed node is a child');
    this.childNodes.splice(at, 1); node.parentNode = null; return node;
  }
}
const text = value => new Node(3, '#text', value);
const el = (tag, attrs = {}, ...kids) => {
  const node = new Node(1, tag.toUpperCase());
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  for (const kid of kids) node.insertBefore(typeof kid === 'string' ? text(kid) : kid, null);
  return node;
};
const card = (key, title, attrs = {}) => el('div', { class: 'wcard', 'data-key': key, ...attrs }, el('b', {}, title));
const html = n => n.nodeType === 3 ? n.nodeValue
  : `<${n.nodeName.toLowerCase()}${n.attributes.map(a => ` ${a.name}="${a.value}"`).join('')}>${n.childNodes.map(html).join('')}</${n.nodeName.toLowerCase()}>`;
const fresh = (...kids) => el('template', {}, ...kids);

test('an unchanged card keeps its node when a new one arrives before it', () => {
  const app = el('div', {}, card('w1', '#1'), card('w2', '#2'));
  const [one, two] = app.childNodes;
  morph(app, fresh(card('w9', '#9'), card('w1', '#1'), card('w2', '#2')));
  assert.equal(html(app), html(el('div', {}, card('w9', '#9'), card('w1', '#1'), card('w2', '#2'))));
  assert.equal(app.childNodes[1], one);
  assert.equal(app.childNodes[2], two);
});

test('a changed card is patched in place, not replaced', () => {
  const app = el('div', {}, card('w1', '#1', { class: 'wcard new' }));
  const [one] = app.childNodes, title = one.childNodes[0].childNodes[0];
  morph(app, fresh(card('w1', '#1 bis')));
  assert.equal(app.childNodes[0], one);
  assert.equal(one.childNodes[0].childNodes[0], title);
  assert.equal(title.nodeValue, '#1 bis');
  assert.equal(one.getAttribute('class'), 'wcard');
});

test('cards follow the new order, and a card gone is removed', () => {
  const app = el('div', {}, card('w1', '#1'), card('w2', '#2'), card('w3', '#3'));
  const [one, , three] = app.childNodes;
  morph(app, fresh(card('w3', '#3'), card('w1', '#1')));
  assert.deepEqual(app.childNodes, [three, one]);
});

test('an attribute gone from the new page is removed, a new one set', () => {
  const app = el('div', {}, el('button', { hidden: '', 'data-open': 'i1' }, '+2 autres'));
  const [more] = app.childNodes;
  morph(app, fresh(el('button', { 'data-open': 'i7' }, '+2 autres')));
  assert.equal(app.childNodes[0], more);
  assert.equal(more.getAttribute('hidden'), null);
  assert.equal(more.getAttribute('data-open'), 'i7');
});

test('children without a key match in order, and a different tag is replaced', () => {
  const app = el('div', {}, el('span', {}, 'a'), el('p', {}, 'b'));
  const [span] = app.childNodes;
  morph(app, fresh(el('span', {}, 'a2'), el('div', {}, 'b'), text('tail')));
  assert.equal(app.childNodes[0], span);
  assert.equal(html(app), '<div><span>a2</span><div>b</div>tail</div>');
});

// ---- the quota gauges (#822, #823, #848), taken from the page between their markers ----
const between = (from, to) => page.slice(page.indexOf(from), page.indexOf(to));
const textSource = between('/* text:begin', '/* text:end */');
const quotaSource = between('/* quota:begin', '/* quota:end */');
assert.ok(textSource.length > 100 && quotaSource.length > 100, 'the text and quota markers are in the page');
const Q = new Function(`${textSource}; ${quotaSource}; return { untilReset, duration, claudeGauge, actionsGauge, cpuGauge, tipHtml };`)();
const T = 1_790_000_000, H = 3600;
const row = (g, key) => (g.rows.find(r => r[0] === key) || [])[1];

test('the time to a reset, compact on the gauge, spelled out in the detail', () => {
  assert.equal(Q.untilReset(2 * H + 14 * 60), '2h14');
  assert.equal(Q.untilReset(3 * 24 * H + 5 * H + 59 * 60), '3d5h');
  assert.equal(Q.untilReset(9 * 60 + 30), '9min');
  assert.equal(Q.duration(2 * H + 14 * 60), '2 h 14 min');
  assert.equal(Q.duration(3 * 24 * H + 5 * H + 59 * 60), '3 d 5 h');
  assert.equal(Q.duration(42), '0 min');
});

test('a Claude window read: its percent, its threshold, its reset, its age', () => {
  const g = Q.claudeGauge('claude5h', 'Claude 5h', { used_percent: 62.4, resets_at: T + 2 * H + 14 * 60, seen_at: T - 60 }, T, null);
  assert.equal(g.text, '62% · 2h14');
  assert.equal(g.pct, 62.4);
  assert.equal(row(g, 'Used'), '62%');
  assert.equal(row(g, 'Threshold'), '90% (turns yellow)');
  assert.equal(row(g, 'Resets'), 'in 2 h 14 min');
  assert.equal(row(g, 'Read'), '1 min ago');
  assert.equal(row(g, 'Projected'), undefined);  // the 5-hour window carries none
});

test('the week: its admission cap as threshold, and its projection when it has one', () => {
  const g = Q.claudeGauge('claudeweek', 'Claude week', { used_percent: 88, resets_at: T + 80 * H, seen_at: T - 2 * H, projected_percent: 131.2 }, T, null, { cap: 80 });
  assert.equal(g.text, '88% · 3d8h');
  assert.equal(g.cls, 'r');  // over the admission cap
  assert.equal(row(g, 'Threshold'), '80% (no new worker above)');
  assert.equal(row(g, 'Projected'), '≈ 131% at the reset');
  assert.equal(row(g, 'Read'), '2 h 0 min ago');
  assert.equal(row(g, 'Slope'), undefined);  // none known: none said
});

test('the detail says the slope a projection follows, over which days, and its half-life (#864)', () => {
  const week = { used_percent: 88, resets_at: T + 80 * H, seen_at: T - 60, projected_percent: 131.2,
    slope: { per_day: 12.96, days: 7, window_days: 7, half_life_days: 2 } };
  assert.equal(row(Q.claudeGauge('claudeweek', 'Claude week', week, T, null), 'Slope'),
    'at the pace of the last 7 days, exponentially weighted (half-life 2 days): 13.0%/day');
  const minutes = { used_minutes: 6, included_minutes: 3000, used_percent: 0.2, resets_at: T + 26 * H, read_at: T - 600, error: null,
    projected_minutes: 3606, slope: { per_day: 119.6, days: 1.5, window_days: 7 } };
  // less history than the window: the span it has, said
  assert.equal(row(Q.actionsGauge(minutes, T), 'Slope'),
    'at the pace of the last 1.5 days (all the history there is): 120 min/day');
  // under a day of history: in hours, never « the last 0 days »
  const young = { ...minutes, slope: { per_day: 40, days: 0.02, window_days: 7 } };
  assert.equal(row(Q.actionsGauge(young, T), 'Slope'),
    'at the pace of the last 1 h (all the history there is): 40 min/day');
});

test('an unknown window is « unknown », its reason in the detail, no number', () => {
  const g = Q.claudeGauge('claudeweek', 'Claude week', null, T, 'no Claude status line received — declared: 78%');
  assert.equal(g.text, 'unknown');
  assert.equal(g.pct, null);
  assert.deepEqual(g.rows, [['Unknown', 'no Claude status line received — declared: 78%']]);
});

// #862: past a reset, without a reading since, the gauge says when the new window opened — never a percent.
const hm = t => new Date(t * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
const previous = { used_percent: 50.2, seen_at: T - 40 * 60, ended_at: T - 2 * 60, next_resets_at: T + 5 * H - 2 * 60 };

test('a new window without a reading: when it opened, why nothing is read, the previous one, its end', () => {
  const g = Q.claudeGauge('claude5h', 'Claude 5h', null, T, null, { previous, running: 0 });
  assert.equal(g.text, `new window since ${hm(T - 2 * 60)} · no reading yet`);
  assert.equal(g.pct, null);
  assert.equal(g.cls, 'dim');
  assert.equal(row(g, 'Unknown'), "no worker running — the quota is read by the workers' status line");
  assert.equal(row(g, 'Previous window'), `50% at ${hm(T - 40 * 60)}`);
  assert.equal(row(g, 'Resets'), `around ${hm(T + 5 * H - 2 * 60)}`);
  assert.ok(!g.rows.some(r => r[0] === 'Used'), 'no percent made up for the new window');
});

test('a new window while a worker runs: the reason says the reading is still to come', () => {
  const g = Q.claudeGauge('claude5h', 'Claude 5h', null, T, null, { previous, running: 2 });
  assert.equal(row(g, 'Unknown'), "no worker has answered since — the quota is read by the workers' status line");
});

test('past the window after the reset: neither when the current one opened nor its end', () => {
  const g = Q.claudeGauge('claude5h', 'Claude 5h', null, T, null, { previous: { ...previous, next_resets_at: null } });
  assert.equal(g.text, `no reading since the reset at ${hm(T - 2 * 60)}`);
  assert.equal(row(g, 'Resets'), undefined);
});

test('a previous window older than today carries its day', () => {
  const old = { ...previous, seen_at: T - 3 * 24 * H, ended_at: T - 2 * 24 * H, next_resets_at: T + 5 * 24 * H };
  const g = Q.claudeGauge('claudeweek', 'Claude week', null, T, null, { previous: old, note: ' — declared: 78%' });
  const day = t => new Date(t * 1000).toLocaleDateString('en-GB', { weekday: 'short' });
  assert.equal(g.text, `new window since ${day(T - 2 * 24 * H)} ${hm(T - 2 * 24 * H)} · no reading yet`);
  assert.equal(row(g, 'Previous window'), `50% at ${day(T - 3 * 24 * H)} ${hm(T - 3 * 24 * H)}`);
  assert.match(row(g, 'Unknown'), / — declared: 78%$/);
});

test('a fresh reading after a reset is the normal gauge, the previous window gone from it', () => {
  const g = Q.claudeGauge('claude5h', 'Claude 5h', { used_percent: 4, resets_at: T + 4 * H, seen_at: T - 60 }, T, null, { previous });
  assert.equal(g.text, '4% · 4h00');
  assert.equal(row(g, 'Previous window'), undefined);
});

test('GitHub Actions: minutes, percent, reset and the month\'s projection — or « unknown » and why', () => {
  const read = { used_minutes: 1520, included_minutes: 2000, used_percent: 76, resets_at: T + 26 * H, read_at: T - 600, error: null, projected_minutes: 2310.4 };
  const g = Q.actionsGauge(read, T);
  assert.equal(g.text, '1,520/2,000min · 76% · 1d2h');
  assert.equal(row(g, 'Used'), '1,520 of 2,000 min (76%)');
  assert.equal(row(g, 'Threshold'), '2,000 min included in the plan');
  assert.equal(row(g, 'Resets'), 'in 1 d 2 h');
  assert.equal(row(g, 'Projected'), '≈ 2,310 min at the end of the month — over the plan');
  assert.equal(row(g, 'Read'), '10 min ago');
  assert.equal(row(Q.actionsGauge({ ...read, projected_minutes: null }, T), 'Projected'), undefined);
  const refused = Q.actionsGauge({ ...read, used_minutes: null, used_percent: null, error: 'GitHub refused (HTTP 403)' }, T);
  assert.equal(refused.text, 'unknown');
  assert.deepEqual(refused.rows, [['Unknown', 'GitHub refused (HTTP 403)']]);
});

test('the detail is one block for every gauge: its name, then its rows, escaped', () => {
  const html = Q.tipHtml({ label: 'CPU', rows: [['Busy', '<34%>']] });
  assert.match(html, /^<b>CPU<\/b><dl><dt>Busy<\/dt><dd>&lt;34%&gt;<\/dd><\/dl>$/);
  assert.equal(row(Q.cpuGauge(null, null, T), 'Unknown') !== undefined, true);
});

// ---- the hover detail (#848): opens on hover, closes on leaving; a touch shows it a few seconds ----
const tipSource = between('/* tip:begin', '/* tip:end */');
assert.ok(tipSource.length > 100, 'the tip markers are in the page');
const { detailTip, TOUCH_TIP_MS } = new Function(`${tipSource}; return { detailTip, TOUCH_TIP_MS };`)();
function tipWorld() {
  const seen = [], timers = new Map(); let n = 0;
  const tip = detailTip({
    show: key => seen.push(`show ${key}`), hide: () => seen.push('hide'),
    later: (f, ms) => { timers.set(++n, { f, ms }); return n; }, cancel: id => timers.delete(id) });
  const elapse = () => { for (const [id, t] of [...timers]) { timers.delete(id); t.f(); } };
  return { tip, seen, timers, elapse };
}

test('hovering a gauge opens its detail, leaving it closes it', () => {
  const { tip, seen, timers } = tipWorld();
  tip.enter('ram');
  assert.equal(tip.open, 'ram');
  tip.leave();
  assert.equal(tip.open, null);
  assert.deepEqual(seen, ['show ram', 'hide']);
  assert.equal(timers.size, 0, 'the mouse never closes it by a timer');
});

test('a touch shows the detail, and it closes by itself a few seconds later', () => {
  const { tip, seen, timers, elapse } = tipWorld();
  tip.enter('actions', true);
  assert.equal(tip.open, 'actions');
  assert.equal([...timers.values()][0].ms, TOUCH_TIP_MS);
  assert.ok(TOUCH_TIP_MS >= 3000 && TOUCH_TIP_MS <= 6000, 'a few seconds');
  tip.leave(true);  // the finger lifted: still shown
  assert.equal(tip.open, 'actions');
  elapse();
  assert.equal(tip.open, null);
  assert.deepEqual(seen, ['show actions', 'hide']);
});

test('a second touch restarts the clock, on the gauge touched last', () => {
  const { tip, seen, timers, elapse } = tipWorld();
  tip.enter('ram', true); tip.enter('cpu', true);
  assert.equal(timers.size, 1, 'one clock, not two');
  elapse();
  assert.deepEqual(seen, ['show ram', 'show cpu', 'hide']);
});

test('a detail open across a refresh is shown again with the fresh figures', () => {
  const { tip, seen } = tipWorld();
  tip.refresh();
  tip.enter('disk'); tip.refresh();
  assert.deepEqual(seen, ['show disk', 'show disk']);
});

// ---- the 60-second cycle (the operator, 30/09), taken from the page between its markers ----
const cycleSource = page.slice(page.indexOf('/* cycle:begin'), page.indexOf('/* cycle:end */'));
assert.ok(cycleSource.length > 100, 'the cycle markers are in the page');
const C = new Function(`${textSource}; ${cycleSource}; return { snapshotAge, nextLoadIn, ageText };`)();

test('the age is counted on the server clock, then second by second', () => {
  // the phone's clock is 5 minutes ahead: it must not age the snapshot
  const d = { built_at: 1000, served_at: 1012, received_at: 1300 + 1000 };
  assert.equal(C.snapshotAge(d, 2300), 12);
  assert.equal(C.snapshotAge(d, 2301), 13);
  assert.equal(C.ageText(C.snapshotAge(d, 2305.9)), '17 s ago');
});

test('the page asks again just after the next build, or soon while building', () => {
  assert.equal(C.nextLoadIn(12, false), 49);   // 61 s after the snapshot was built
  assert.equal(C.nextLoadIn(75, false), 1);    // late: at once, never negative
  assert.equal(C.nextLoadIn(12, true), 2);     // the server is building a newer one
});

test('the age reads in seconds, then minutes, then hours', () => {
  assert.equal(C.ageText(59.9), '59 s ago');
  assert.equal(C.ageText(600), '10 min ago');
  assert.equal(C.ageText(3 * 3600), '3 h ago');
});

/* The tabs and the curves (#839), taken from the page between their markers. */
const G = new Function(`${textSource}
  ${between('/* tabs:begin', '/* tabs:end */')}
  ${between('/* charts:begin', '/* charts:end */')}
  return { tabOf, TABS, niceMax, axisTicks, sx, sy, L, R, get W() { return W; }, lineChart, barChart, TIPS, MODELS, readAt, readIndex, stepIndex };`)();

test('the tab shown is the address’s, Factory by default', () => {
  assert.equal(G.tabOf('#measures'), 'measures');
  assert.equal(G.tabOf('#github'), 'github');
  assert.equal(G.tabOf(''), 'factory');
  assert.equal(G.tabOf('#whatever'), 'factory');  // an unknown tab is the default one
  for (const old of ['#trends', '#Quality', '#tendances', '#qualite']) assert.equal(G.tabOf(old), 'measures', old);  // #1821
  assert.equal(G.tabOf('#mesures'), 'measures');
  assert.deepEqual(G.TABS.map(t => t[0]), ['factory', 'measures', 'github']);
});

// ---- Measures (#1821): the tiles, over the span shown ----
const MT = new Function(`${textSource}; ${between('/* measures:begin', '/* measures:end */')}; return { measureTiles };`)();
const measured = (series, days = 4) => ({ days: Array.from({ length: days }, (_, i) => `2026-10-2${i}`), series: {
  merged: [], deploys: [], tickets: [], ci_p50: [], ci_p90: [], runs_green: [], runs_red: [], runs_other: [], lead_h: [],
  night_ok: [], mem_min_gb: [], mem_low: [], ...series } });
const tileOf = (m, name) => MT.measureTiles(m).find(t => t[0] === name);

test('a tile counts the days with data: a gap is left out, never a zero', () => {
  const m = measured({ merged: [null, null, 3, 1], deploys: [null, 0, 2, 0], tickets: [null, 4, 0, 2] });
  assert.deepEqual(tileOf(m, 'PULL REQUESTS MERGED'), ['PULL REQUESTS MERGED', '2/day', '4 in 4 d']);
  assert.deepEqual(tileOf(m, 'DEPLOYMENTS').slice(1), ['2', 'in 4 d']);
  assert.equal(tileOf(m, 'TICKETS CLOSED')[1], '2/day');
});

test('a tile without any data says so', () => {
  const m = measured({ merged: [null, null, null, null] });
  assert.deepEqual(MT.measureTiles(m).slice(0, 8).map(t => t[1]), Array(8).fill('no data'));
});

test('the CI tiles: median duration and its p90, the red share of all runs, the night suites green', () => {
  const m = measured({ ci_p50: [20, null, 30, 26], ci_p90: [40, null, 60, 50], runs_green: [3, 0, 4, 1], runs_red: [1, 0, 1, 0], runs_other: [0, 0, 0, 0],
    night_ok: [1, null, 0, 1], lead_h: [2, null, 6, 4], mem_min_gb: [2.5, null, 1.2, 3], mem_low: [0, 0, 3, 0] });
  assert.deepEqual(tileOf({ ...m, period: { ci_p50: 1, ci_p90: 4 } }, 'CI DURATION (MEDIAN)').slice(1), ['1 min', 'p90 4 min · pull requests'],
    'the span\'s own median, never the median of the daily ones (26 here)');
  assert.equal(tileOf(m, 'CI DURATION (MEDIAN)')[1], 'no data', 'no period figure: said');
  assert.deepEqual(tileOf(m, 'RED CI').slice(1), ['20%', '2 of 10 runs']);
  assert.equal(tileOf(m, 'NIGHT SUITES')[1], '2/3');
  assert.equal(tileOf(m, 'PR OPENED → MERGED')[1], '4 h');
  assert.deepEqual(tileOf(m, 'LOWEST FREE MEMORY').slice(1), ['1.2 GB', '3 readings under 1.5 GB']);
});

test('the tiles #1822 adds: the waits per day, the cost, the first green, the runners killed', () => {
  const m = measured({ wait_ci: [null, 60, 120, 0], wait_memory: [null, 30, 0, 0], wait_permission: [null, 0, 15, 0], wait_question: [null, 0, 15, 120],
    cost_m: [null, 2, 3, 10], first_green: [null, 50, 100, null], oom: [null, 2, 0, 1], oom_runner: [null, 1, 0, 0] });
  assert.deepEqual(tileOf(m, 'WORKERS WAITING').slice(1, 2), ['2 h/day'], '360 min over the 3 days with data');
  assert.equal(tileOf(m, 'COST PER PR')[1], '≈ 3 M');
  assert.equal(tileOf({ ...m, period: { first_green: 60 } }, 'GREEN AT FIRST TRY')[1], '60%', 'weighted by its PRs, not a mean of days');
  assert.equal(tileOf(m, 'GREEN AT FIRST TRY')[1], 'no data');
  assert.deepEqual(tileOf(m, 'RUNNERS KILLED (OOM)').slice(1), ['1', 'runners, of 3 units killed']);
  assert.deepEqual(MT.measureTiles(measured({})).slice(8).map(t => t[1]), Array(4).fill('no data'), 'nothing recorded: said, never a zero');
});


test('an axis tops out at a round number at least the highest value', () => {
  assert.equal(G.niceMax(73), 80);
  assert.equal(G.niceMax(119.5), 150);  // a quota projected past 100 %
  assert.equal(G.niceMax(3605), 4000);
  assert.equal(G.niceMax(100), 100);
  assert.equal(G.niceMax(0), 1);
});

test('an axis is graduated by a round step, each line at its own value', () => {
  assert.deepEqual(G.axisTicks(3000), [0, 1000, 2000, 3000]);  // not 0, 1500, 3000 read « 2 k » in the middle
  assert.deepEqual(G.axisTicks(100), [0, 20, 40, 60, 80, 100]);
  assert.deepEqual(G.axisTicks(3), [0, 1, 2, 3]);  // a count: no 1.5
  assert.deepEqual(G.axisTicks(1.5), [0, 1, 1.5]);
  assert.deepEqual(G.axisTicks(0.5), [0, 0.1, 0.2, 0.3, 0.4, 0.5]);
  assert.deepEqual(G.axisTicks(3570), [0, 1000, 2000, 3000, 3570]);  // an exact top keeps its label
  assert.deepEqual(G.axisTicks(3100), [0, 1000, 2000, 3100]);  // …and drops a line too close to it
  const svg = G.lineChart({ key: 'gh', x0: 0, x1: 100, y1: 3000, exact: true, fmt: v => `${v / 1000} k`, tip: String,
    series: [{ cls: 's1', name: 'min', points: [[0, 100], [50, 900]], proj: null }] });
  for (const [v, label] of [[1000, '1 k'], [2000, '2 k'], [3000, '3 k']]) {
    const y = svg.match(new RegExp(`<text class="axis" x="[\\d.]+" y="([\\d.]+)" text-anchor="end">${label}</text>`));
    assert.ok(y, `${label} is on the axis`);
    assert.ok(Math.abs(Number(y[1]) - 3 - G.sy(v, 0, 3000)) < 0.1, `${label} sits at its value`);
  }
});

test('a higher value is drawn higher', () => {
  assert.ok(G.sy(80, 0, 100) < G.sy(20, 0, 100));
});

test('a projection is the same series, dashed, from its last point to the end', () => {
  const svg = G.lineChart({ key: 'w', x0: 0, x1: 100, y1: 100, fmt: String, tip: String,
    series: [{ cls: 's1', name: 'semaine', points: [[0, 0], [30, 30]], proj: [100, 70] }] });
  const proj = svg.match(/<line class="proj s1" x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"/);
  assert.ok(proj, 'the projection is drawn');
  const [, x1, , x2] = proj.map(Number);
  assert.ok(x1 < x2 && x2 === 590, 'it runs to the end of the period');
  assert.deepEqual(G.MODELS.w.xs, [0, 30], 'the cursor reads the readings, none invented');
  const without = G.lineChart({ key: 'w', x0: 0, x1: 100, y1: 100, fmt: String, tip: String,
    series: [{ cls: 's1', name: 'semaine', points: [[0, 0], [30, 30]], proj: null }] });
  assert.ok(!without.includes('class="proj'), 'no projection, no dashed line');
});

test('a single reading is a dot, not an invisible line', () => {
  const svg = G.lineChart({ key: 'c', x0: 0, x1: 100, y1: 100, fmt: String, tip: String, series: [{ cls: 's1', name: 'cpu', points: [[50, 20]] }] });
  assert.match(svg, /<circle class="ln s1" r="3"/);
  assert.ok(!svg.includes('<polyline'));
});

test('no data draws no chart, and says so', () => {
  const empty = G.lineChart({ key: 'e', x0: 0, x1: 1, y1: 100, fmt: String, tip: String, series: [{ cls: 's1', name: 'x', points: [] }] });
  assert.match(empty, /no data yet/);
  assert.match(G.barChart({ key: 'e', fmt: String, bars: [{ label: 'a', parts: [{ v: 0, cls: 'ci-ok' }], rows: [] }] }), /no data yet/);
});

test('green and red CI stack, each with its label, and a zero draws no bar', () => {
  const svg = G.barChart({ key: 'ci', fmt: String, bars: [
    { label: '01/09', parts: [{ v: 3, cls: 'ci-ok' }, { v: 1, cls: 'ci-ko' }], rows: [['✓', '3'], ['✗', '1']] },
    { label: '02/09', parts: [{ v: 2, cls: 'ci-ok' }, { v: 0, cls: 'ci-ko' }], rows: [['✓', '2'], ['✗', '0']] }] });
  assert.equal((svg.match(/<rect class="ci-ok"/g) || []).length, 2);
  assert.equal((svg.match(/<rect class="ci-ko"/g) || []).length, 1);
  assert.deepEqual(G.readIndex(G.MODELS.ci, 0), { i: 0, n: 2, x: G.L + G.MODELS.ci.step / 2, title: '01/09', rows: [['✓', '3'], ['✗', '1']],
    band: [G.L, G.MODELS.ci.step], points: [] });
  const [green, red] = [...svg.matchAll(/<rect class="(ci-ok|ci-ko)" x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"/g)];
  assert.ok(Number(red[2]) + Number(red[3]) <= Number(green[2]), 'red sits above green, with a gap');
});

test('a share runs over the bars on its own 0–100 % axis, and breaks where there is none', () => {
  const bar = v => ({ label: 'd', parts: [{ v, cls: 'ci-ok' }], rows: [['✓', String(v)]] });
  const svg = G.barChart({ key: 'cs', fmt: String, bars: [bar(4), bar(0), bar(2), bar(2)],
    line: { cls: 's1', values: [1, null, 0.5, 0.5] } });
  assert.equal((svg.match(/<polyline class="ln s1"/g) || []).length, 1, 'the two days with a share: one line');
  assert.equal((svg.match(/<circle class="ln s1"/g) || []).length, 1, 'a lone share: a dot, cut from the next');
  const dot = svg.match(/<circle class="ln s1" r="[\d.]+" cx="[\d.]+" cy="([\d.]+)"/);
  assert.equal(Number(dot[1]), G.sy(1, 0, 1), '100 % at the top');
  const [, a, b] = svg.match(/<polyline class="ln s1" points="[\d.]+,([\d.]+) [\d.]+,([\d.]+)"/);
  assert.ok(a === b && Math.abs(Number(a) - G.sy(0.5, 0, 1)) < 0.1, '50 % halfway');
  for (const label of ['0%', '50%', '100%']) assert.match(svg, new RegExp(`class="axis pct"[^>]*>${label}<`));
  assert.ok(svg.lastIndexOf('<rect class="ci-ok"') < svg.indexOf('<polyline'), 'over the bars');
  assert.ok(svg.indexOf('<polyline') < svg.indexOf('class="cursor"'), 'under the cursor\'s layer');
  const without = G.barChart({ key: 'cs', fmt: String, bars: [bar(4)] });
  assert.ok(!without.includes('pct') && !without.includes('class="ln'), 'no share, no second axis');
});

// ---- the vertical cursor (#2050): one reading of a chart at a pointer's x ----
const vxOf = t => G.sx(t, 0, 100);  // a chart drawn over x0 = 0 … x1 = 100
const twoSeries = () => G.lineChart({ key: 'dur', x0: 0, x1: 100, y1: 0, fmt: String, tip: (v, t) => [`day ${t}`, `${v} min`],
  series: [{ cls: 's1', name: 'median', points: [[10, 20], [30, 25]], proj: null }, { cls: 's1', name: 'median', points: [[70, 22]], proj: null },
    { cls: 's2', name: 'p90', points: [[10, 40], [30, 45], [50, 41], [70, 44]], proj: null }] });

test('the cursor snaps to the nearest day, wherever the pointer is over the chart', () => {
  twoSeries();
  const r = G.readAt(G.MODELS.dur, vxOf(37));
  assert.equal(r.title, 'day 30');
  assert.equal(r.x, vxOf(30), 'the line stands on the day, not under the pointer');
  assert.equal(r.i, 1); assert.equal(r.n, 4, 'every x a series has, merged');
  assert.equal(G.readAt(G.MODELS.dur, 0).title, 'day 10', 'left of the first: the first');
  assert.equal(G.readAt(G.MODELS.dur, 10_000).title, 'day 70', 'right of the last: the last');
});

test('the cursor gives one row per series, in the legend\'s order, a missing day said "no data"', () => {
  twoSeries();
  assert.deepEqual(G.readIndex(G.MODELS.dur, 1).rows, [['s1', 'median', '25 min'], ['s2', 'p90', '45 min']]);
  assert.deepEqual(G.readIndex(G.MODELS.dur, 2).rows, [['s1', 'median', 'no data'], ['s2', 'p90', '41 min']], 'a gap, never a zero');
  assert.deepEqual(G.readIndex(G.MODELS.dur, 3).rows[0], ['s1', 'median', '22 min'], 'two runs of one series: one row');
  assert.equal(G.readIndex(G.MODELS.dur, 2).points.length, 1, 'a dot on each curve that has the day');
});

test('along a projection\'s dashes the cursor follows the pointer and reads the projection there', () => {
  G.lineChart({ key: 'wk', x0: 0, x1: 100, y1: 100, exact: true, fmt: String, tip: (v, t) => [`t${t}`, `${v.toFixed(1)}%`],
    series: [{ cls: 's1', name: 'week', points: [[0, 10], [40, 40]], proj: [100, 130], projRows: [['slope', '5%/day']] }] });
  const r = G.readAt(G.MODELS.wk, vxOf(70));
  assert.equal(r.pj, true);
  assert.ok(Math.abs(r.x - vxOf(70)) < 1e-9, 'no snapping on the dashes');
  assert.equal(r.title, 't70 · projection');
  assert.deepEqual(r.rows, [['s1', 'week at this pace', '85.0%'], ['', 'slope', '5%/day']], 'halfway from 40 % to 130 %');
  assert.ok(r.points[0][1] >= G.sy(100, 0, 100) - 1e-9, 'its dot stays inside the chart, the label saying where it goes');
  assert.equal(G.readAt(G.MODELS.wk, 10_000).title, 't100 · projection', 'no further than the period\'s end');
  assert.equal(G.readAt(G.MODELS.wk, vxOf(30)).title, 't40', 'before the dashes: the readings');
});

test('a projection that starts where it ends reads the readings, never NaN', () => {
  G.lineChart({ key: 'z', x0: 0, x1: 100, y1: 100, fmt: String, tip: (v, t) => [`t${t}`, `${v}%`],
    series: [{ cls: 's1', name: 'week', points: [[0, 10], [100, 40]], proj: [100, 40] }] });
  const r = G.readAt(G.MODELS.z, 10_000);
  assert.ok(!r.pj && r.rows[0][2] === '40%', JSON.stringify(r));
});

test('over bars the cursor lights the column under the pointer, its rows and the green share', () => {
  G.barChart({ key: 'runs', fmt: String, line: { cls: 's1', values: [0.5, null] }, bars: [
    { label: 'Oct 1', parts: [{ v: 3, cls: 'ci-ok' }, { v: 1, cls: 'ci-ko' }], rows: [['ci-ok', 'green', '3'], ['ci-ko', 'red', '1'], ['s1', 'green share, 3 d', '50%']] },
    { label: 'Oct 2', parts: [{ v: 2, cls: 'ci-ok' }], rows: [['ci-ok', 'green', '2'], ['ci-ko', 'red', '0'], ['s1', 'green share, 3 d', 'no data']] }] });
  const m = G.MODELS.runs, step = m.step;
  const r = G.readAt(m, G.L + step * 1.2);
  assert.equal(r.i, 1); assert.equal(r.title, 'Oct 2');
  assert.deepEqual(r.band, [G.L + step, step], 'the whole column lit');
  assert.deepEqual(r.rows.at(-1), ['s1', 'green share, 3 d', 'no data']);
  assert.equal(r.points.length, 0, 'no share that day: no dot');
  assert.equal(G.readAt(m, G.L + step * 0.5).points.length, 1, 'the share\'s dot');
  assert.equal(G.readAt(m, -50).i, 0);
});

test('the keys walk the days from 0 to n − 1, Escape closes', () => {
  assert.equal(G.stepIndex(null, 4, 'ArrowRight'), 0);
  assert.equal(G.stepIndex(null, 4, 'ArrowLeft'), 3, 'from the right end: the latest day');
  assert.equal(G.stepIndex(0, 4, 'ArrowLeft'), 0);
  assert.equal(G.stepIndex(2, 4, 'ArrowRight'), 3);
  assert.equal(G.stepIndex(3, 4, 'ArrowRight'), 3);
  assert.equal(G.stepIndex(2, 4, 'Escape'), null);
  assert.equal(G.stepIndex(2, 4, 'a'), 2, 'any other key: unchanged');
});

// ---- every chart is read by the cursor; the gauges keep their detail (#859, #2050) ----
/* a chart the cursor reads: focusable, its model registered, its cursor's layer and label there */
const readable = html => { const key = (html.match(/<svg [^>]*data-chart="([^"]+)"/) || [])[1];
  return Boolean(key && G.MODELS[key] && /tabindex="0"/.test(html) && html.includes('<g class="cursor"></g>') && html.includes('class="xtag"')); };
test('every chart, curves and bars, is read by the cursor, and no mark carries a detail of its own', () => {
  const line = G.lineChart({ key: 'q', x0: 0, x1: 100, y1: 100, fmt: String, tip: (v, t) => [`t${t}`, `${v}%`],
    series: [{ cls: 's1', name: 'week', points: [[0, 10], [30, 40]], proj: [100, 90] }] });
  const bars = G.barChart({ key: 'tk', fmt: String, bars: [
    { label: 'Sep 1', parts: [{ v: 1, cls: 'k1' }, { v: 2, cls: 'k2' }, { v: 3, cls: 'k3' }, { v: 4, cls: 'k4' }],
      rows: [['k1', 'input', '1'], ['k2', 'cache written', '2'], ['k3', 'cache read', '3'], ['k4', 'output', '4'], ['', 'total', '10']] }] });
  for (const html of [line, bars]) {
    assert.ok(readable(html), html.slice(0, 200));
    assert.ok(!/data-tip=|class="hit/.test(html), 'the 6 px targets and the per-mark details are gone');
  }
  assert.deepEqual(G.readIndex(G.MODELS.q, 1).rows, [['s1', 'week', '40%']]);
  assert.equal((bars.match(/<rect class="k\d"/g) || []).length, 4, 'four categories, stacked');
  assert.equal(G.readIndex(G.MODELS.tk, 0).rows.length, 5);
});

test('the counter-proof: a chart drawn without its model is caught', () => {
  assert.ok(!readable('<div class="plot"><svg viewBox="0 0 1 1" tabindex="0" data-chart="nobody"><g class="cursor"></g></svg><div class="xtag" hidden></div></div>'));
});

test('the page draws no chart outside lineChart and barChart, and no native tooltip', () => {
  const charts = between('/* charts:begin', '/* charts:end */');
  const outside = page.replace(charts, '');
  assert.ok(!/<svg viewBox/.test(outside), 'an SVG chart drawn outside the shared charts');
  assert.equal((visibleSource(page).match(/<title>/g) || []).length, 1, 'a <title> tooltip besides the page\'s own: the detail is the one component');
  const gauge = page.match(/<span class="gauge"[^\n]*/);
  assert.ok(gauge && gauge[0].includes('tipAttr('), 'a gauge keeps its detail');
});

test('a quota axis ends at 100 % and a projection past it stops at the top', () => {
  const svg = G.lineChart({ key: 'cw', x0: 0, x1: 100, y1: 100, exact: true, fmt: v => `${v}%`, tip: String,
    limits: [{ y: 80, label: '80%' }], series: [{ cls: 's1', name: 'week', points: [[0, 10], [50, 60]], proj: [100, 130] }] });
  assert.match(svg, />100%<\/text>/);
  assert.ok(!/>150%</.test(svg), 'no rounded-up axis past the limit');
  const [, , y2] = svg.match(/<line class="proj s1" x1="[\d.]+" y1="[\d.]+" x2="([\d.]+)" y2="([\d.]+)"/).map(Number);
  assert.ok(y2 >= G.sy(100, 0, 100) - 0.05, 'the projection stays inside the chart');
  assert.match(G.readAt(G.MODELS.cw, 10_000).rows[0][2], /130/);  // the cursor still says where it goes
});

test('a projection\'s reading carries the rows its series gives — the slope (#864)', () => {
  G.lineChart({ key: 'sl', x0: 0, x1: 100, y1: 100, fmt: String, tip: String,
    series: [{ cls: 's1', name: 'week', points: [[0, 10], [50, 60]], proj: [100, 90], projRows: [['Slope', '5%/day']] }] });
  assert.deepEqual(G.readAt(G.MODELS.sl, 10_000).rows[1], ['', 'Slope', '5%/day']);
});

test('Actions may pass its plan: the axis goes to the highest value, the plan line stays, named', () => {
  const svg = G.lineChart({ key: 'am', x0: 0, x1: 100, y1: 3000, exact: true, fmt: v => String(Math.round(v)), tip: String,
    limits: [{ y: 3000, label: '3,000 min included' }], series: [{ cls: 's1', name: 'min', points: [[0, 100], [50, 3570]], proj: null }] });
  assert.match(svg, />3570<\/text>/);
  assert.match(svg, /<line class="limit"[^>]*\/><text[^>]*>3,000 min included<\/text>/);
  const under = G.lineChart({ key: 'am2', x0: 0, x1: 100, y1: 3000, exact: true, fmt: v => String(Math.round(v)), tip: String,
    limits: [{ y: 3000, label: 'plan' }], series: [{ cls: 's1', name: 'min', points: [[0, 100], [50, 900]], proj: null }] });
  assert.match(under, />3000<\/text>/);
});

// ---- CI RUNS PER DAY (#2049): the green share over 3 days, its % in a day's detail ----
const DB = new Function(`${textSource}
  ${between('/* charts:begin', '/* charts:end */')}
  const short = d => d.slice(5), DAY_S = 86400, dm = t => 'd' + new Date(t * 1000).getUTCDate();
  ${between('/* daily:begin', '/* daily:end */')}
  return { dailyBars, dailyLine, loadParts, MODELS, readIndex, readAt };`)();

test('a day of CI RUNS PER DAY gives its green share under its counts, or no data', () => {
  const m = { days: ['2026-10-27', '2026-10-28'], series: { runs_green: [5, 0], runs_red: [1, 0], runs_other: [0, 1], green_share: [0.8333, null] } };
  const svg = DB.dailyBars(m, 'gs', [{ k: 'runs_green', cls: 'ci-ok', name: 'green' }, { k: 'runs_red', cls: 'ci-ko', name: 'red' }, { k: 'runs_other', cls: 'k3', name: 'cancelled, skipped' }],
    undefined, { k: 'green_share', cls: 's1', name: 'green share, 3 d' });
  assert.deepEqual(DB.readIndex(DB.MODELS.gs, 0).rows, [['ci-ok', 'green', '5'], ['ci-ko', 'red', '1'], ['k3', 'cancelled, skipped', '0'], ['s1', 'green share, 3 d', '83%']]);
  assert.deepEqual(DB.readIndex(DB.MODELS.gs, 1).rows.at(-1), ['s1', 'green share, 3 d', 'no data']);
  const quiet = { days: ['2026-10-27', '2026-10-28'], series: { runs_green: [2, 0], runs_red: [0, 0], runs_other: [0, 0], green_share: [1, 1] } };
  const svg2 = DB.dailyBars(quiet, 'gq', [{ k: 'runs_green', cls: 'ci-ok', name: 'green' }], undefined, { k: 'green_share', cls: 's1', name: 'green share, 3 d' });
  assert.ok(svg2.includes('data-chart="gq"'));
  assert.deepEqual(DB.readIndex(DB.MODELS.gq, 1).rows.at(-1), ['s1', 'green share, 3 d', '100%'], 'a day without a run still gives its share');
  assert.match(svg, /<(polyline|circle) class="ln s1"/, 'the share drawn on its own axis');
});

test('on a daily curve the cursor stops on every day: a gap, or a series empty all along, says "no data"', () => {
  const m = { days: ['2026-10-26', '2026-10-27', '2026-10-28'], series: { p50: [20, null, 22], p90: [null, null, null] } };
  DB.dailyLine(m, 'dl', [{ k: 'p50', cls: 's1', name: 'median' }, { k: 'p90', cls: 's2', name: 'p90' }], v => `${v} min`);
  const model = DB.MODELS.dl;
  assert.equal(model.xs.length, 3, 'the empty day is a stop');
  assert.deepEqual(DB.readIndex(model, 1).rows, [['s1', 'median', 'no data'], ['s2', 'p90', 'no data']]);
  assert.equal(DB.readIndex(model, 1).title, 'd27');
  assert.deepEqual(DB.readIndex(model, 2).rows, [['s1', 'median', '22 min'], ['s2', 'p90', 'no data']], 'every legend\'s series, even one never measured');
});

test('the load (#2280): each consumer’s share of a day, a consumer never measured that day and a day without reading say "no data"', () => {
  const m = { days: ['2026-11-02', '2026-11-03'], series: { cpu_workers: [24.7, null], cpu_ci: [40.6, null], cpu_factory: [null, null], cpu_containers: [8.6, null], cpu_rest: [6.1, null] } };
  DB.dailyBars(m, 'cpu', DB.loadParts(), v => `${Math.round(v)}%`);
  assert.deepEqual(DB.readIndex(DB.MODELS.cpu, 0).rows, [['k1', 'workers', '25%'], ['k2', 'CI', '41%'], ['k4', 'factory', 'no data'], ['ci-ko', 'containers', '9%'], ['k3', 'rest', '6%']]);
  assert.ok(DB.readIndex(DB.MODELS.cpu, 1).rows.every(r => r[2] === 'no data'), 'a day without reading is a gap, never 0%');
  const l = { days: m.days, series: { load_p50: [8, null], load_max: [16.5, null] } };
  DB.dailyLine(l, 'load', [{ k: 'load_p50', cls: 's1', name: 'median' }, { k: 'load_max', cls: 's2', name: 'peak' }], String);
  assert.deepEqual(DB.readIndex(DB.MODELS.load, 1).rows, [['s1', 'median', 'no data'], ['s2', 'peak', 'no data']]);
  assert.match(page, /dailyLine\(m, 'm-load', \[\{ k: 'load_p50'[^\n]*k: 'load_max'/);
  assert.match(page, /dailyBars\(m, 'm-cpu', loadParts\(\)/);
  assert.match(page, /k: 'cpu_share', cls: 's1', name: M.cpuUse/, 'the day\'s CPU drawn on the bars');
  assert.match(page, /S.cpu_share = \(S.cpu_mean \|\| \[\]\).map\(v => v == null \? null : v \/ 100\)/, 'a gap stays a gap');
});

test('the page computes no red share any more: the server gives the green one', () => {
  assert.ok(!/redShare|pctOf\(/.test(page));
  assert.match(page, /greenShare: 'green share, 3 d'/);
  assert.match(page, /k: 'green_share'/);
});

// ---- the QUOTAS section of Measures (#2048): Trends' two projections ----
const QC = new Function(`${textSource}
  ${between('/* charts:begin', '/* charts:end */')}
  const DAY_S = 86400, esc = s => String(s ?? '');
  const int = n => Math.round(n).toLocaleString('en-US');
  const dayOf = t => new Date(t * 1000), dm = t => TXT.months[dayOf(t).getUTCMonth()] + ' ' + dayOf(t).getUTCDate();
  const chartCard = (title, body) => '<section><h3>' + title + '</h3>' + body + '</section>';
  ${between('/* quotacharts:begin', '/* quotacharts:end */')}
  return { claudeChart, actionsChart, vpsCharts, lineChart, MODELS, readIndex, readAt };`)();
const WEEK_END = 1_790_000_000, WEEK_START = WEEK_END - 7 * 86400;
const slope = { per_day: 12.6, days: 7, window_days: 7, half_life_days: 2 };

test('the Claude week: its sentence, its readings, dashed to the reset, the 80 % line and the present instant', () => {
  const at = WEEK_START + 3 * 86400;
  const html = QC.claudeChart({ start: WEEK_START, end: WEEK_END, seven_day: [[WEEK_START + 3600, 10], [at, 40]], projected: 90.4, slope }, at);
  assert.match(html, /CLAUDE QUOTA · THE WEEK/);
  assert.match(html, /at the pace of the last 7 days[^<]*, end of week ≈ <b>90%<\/b>/);
  assert.ok(!html.includes('used up'), 'under 100 %: no warning');
  assert.match(html, /<polyline class="ln s1"/);
  assert.match(html, /<line class="proj s1"/);
  assert.match(html, />alert 80%<\/text>/);
  assert.equal((html.match(/<line class="nowline"/g) || []).length, 1, 'the present instant, dotted');
  const r = QC.readIndex(QC.MODELS.claude, 1);
  assert.deepEqual(r.rows, [['s1', 'week', '40.0%']]);
  assert.deepEqual(QC.readAt(QC.MODELS.claude, 10_000).rows, [['s1', 'week at this pace', '90.4%'], ['', 'Slope', '12.6%/day']],
    'along the dashes: the projection, and the rate it follows — the sentence above says the rest');
});

test('a week that will run out says so in red; an Actions month over its plan in yellow', () => {
  const claude = QC.claudeChart({ start: WEEK_START, end: WEEK_END, seven_day: [[WEEK_START + 3600, 50]], projected: 130, slope }, WEEK_START + 3600);
  assert.match(claude, /<span class="r">✗ quota used up before the reset<\/span>/);
  const actions = QC.actionsChart({ start: WEEK_START, end: WEEK_END, points: [[WEEK_START + 3600, 2000]], included: 3000, projected: 3570, slope }, WEEK_START + 3600);
  assert.match(actions, /ACTIONS MINUTES · THE MONTH/);
  assert.match(actions, /end of month ≈ <b>3,570 min<\/b> for 3,000 included <span class="y">▲ over the plan<\/span>/);
  assert.match(actions, />included 3,000<\/text>/);
});

test('a quota without a reading, or without a slope, says so', () => {
  assert.match(QC.claudeChart(null, 0), /unknown: no status line reading for the current week/);
  assert.match(QC.actionsChart(null, 0), /unknown: no billing reading this month/);
  const flat = QC.claudeChart({ start: WEEK_START, end: WEEK_END, seven_day: [[WEEK_START + 3600, 5]], projected: null, slope: null }, WEEK_START + 3600);
  assert.match(flat, /projection: no rise read over the last days yet/);
  assert.ok(!flat.includes('class="proj'), 'no projection drawn');
});

test('the machine\'s week, hour by hour: memory to its total, CPU and disk to 100 %, the disk\'s 90 % line', () => {
  const H = 3600, at = WEEK_END;
  const hours = [[at - 5 * H, 4096, 10, 70], [at - 4 * H, 6144, 30, 72], [at - 2 * H, 2048, null, 91]];  // at − 3 h: no reading
  const html = QC.vpsCharts(hours, 16384, at);
  for (const title of ['AVAILABLE MEMORY · 7 DAYS', 'CPU · 7 DAYS', 'DISK · 7 DAYS']) assert.match(html, new RegExp(title));
  const [mem, cpu, disk] = html.split('<section>').slice(1);
  assert.match(mem, />16\.0 GB<\/text>/, 'the memory axis ends at the total');
  assert.match(cpu, />100%<\/text>/);
  assert.match(disk, />alert 90%<\/text>/);
  assert.equal((mem.match(/<polyline class="ln s1"/g) || []).length, 1, 'a missing hour cuts the line…');
  assert.equal((mem.match(/<circle class="ln s1"/g) || []).length, 1, '…the hour after it stands alone, never joined by a zero');
  assert.equal((cpu.match(/<polyline class="ln s1"/g) || []).length, 1, 'a null CPU reading is a gap too');
  const first = QC.readIndex(QC.MODELS.vps1, 0);
  assert.match(first.title, /\(hourly mean\)$/);
  assert.deepEqual(first.rows, [['s1', 'available memory · 7 days', '4,096 MB']], 'the runs of one curve: one row');
});

test('the machine\'s week without a reading says no data', () => {
  assert.equal((QC.vpsCharts([], null, WEEK_END).match(/no data yet/g) || []).length, 3);
});

test('the present instant is drawn only inside the chart', () => {
  const chart = now => QC.lineChart({ key: 'nw', x0: 0, x1: 100, y1: 100, fmt: String, tip: String, now,
    series: [{ cls: 's1', name: 'w', points: [[0, 1], [50, 2]], proj: null }] });
  assert.match(chart(50), /<line class="nowline"/);
  assert.ok(!chart(150).includes('nowline') && !chart(null).includes('nowline'));
});

// ---- a PR's jobs and their chrono (#859) ----
test('a job\'s chrono: 0 while waiting, ticking while running, frozen once finished', () => {
  let t = 1000;
  const J = new Function('now', `${between('/* jobs:begin', '/* jobs:end */')}; return { chrono, clock };`)(() => t);
  assert.equal(J.chrono({ state: 'waiting' }), '0s');
  const running = { state: 'running', started_at: 910 };
  assert.match(J.chrono(running), /data-since="910">1m 30s</);
  t = 1001; assert.match(J.chrono(running), />1m 31s</);  // the page ticks it each second
  assert.equal(J.chrono({ state: 'success', started_at: 0, completed_at: 3725 }), '1h 2m 5s');
  assert.equal(J.chrono({ state: 'failure', started_at: 0, completed_at: 42 }), '42s');
  assert.equal(J.chrono({ state: 'skipped', started_at: 5, completed_at: 5 }), '0s');
});

// ---- a delivered PR's panel (#860): its figures, « unknown » never a zero ----
const D = new Function(`${textSource}
  const esc = s => String(s ?? '');
  const gh = (url, label) => '<a href="' + url + '">' + label + '</a>';
  const int = n => Math.round(n).toLocaleString('en-US');
  const duration = s => Math.round(s / 3600) + ' h';
  const testName = t => t;
  const issueUrl = n => 'https://github.com/o/r/issues/' + n;
  ${between('/* tokens:begin', '/* tokens:end */')}
  ${between('/* charts:begin', '/* charts:end */')}
  const DELIVERY = {};
  ${between('/* deliver:begin', '/* deliver:end */')}
  return { deliveryOf, DELIVERY, TIPS };`)();
const plain = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
const HOUR_S = 3600;

test('a chantier PR\'s panel: tokens stacked and exact, times, CI, size, links', () => {
  D.DELIVERY[832] = { data: { error: null, pull: { number: 832, url: 'pr832',
    spec: { number: 830, title: 'Spec — x' },
    tokens: { total: 305174449, input: 4062, cache_write: 5995106, cache_read: 297897060, output: 1278221 },
    times: { published: 0, opened: 25 * HOUR_S, merged: 30 * HOUR_S, prod: 32 * HOUR_S },
    ci: { runs: 2, first_green: false, minutes: { github: 2, vps: 19.5 }, failed_tests: ['t.py::test_a'], last_run_url: 'run9' },
    size: { changed_files: 9, additions: 412, deletions: 37, closed_issues: 2 } } } };
  const html = D.deliveryOf({ number: 832, url: 'pr832' }), shown = plain(html);
  for (const part of ['input 4 k tokens', 'cache read 297,897 k tokens', 'Total 305,174 k tokens', 'spec → PR 25 h', 'merged → prod 2 h',
    'CI runs 2 · ✗ not green at first try', 'GitHub 2.0 min · VPS 19.5 min', 't.py::test_a', 'files 9', '+412 −37', 'issues closed 2'])
    assert.ok(shown.includes(part), `missing « ${part} » in ${shown}`);
  assert.equal((html.match(/<span class="k\d" style="width:/g) || []).length, 4, 'one stacked bar, four categories');
  assert.match(html, /data-tip="delivered:832"/);
  assert.equal(D.TIPS['delivered:832'].rows.length, 5);
  // in thousands, the figures in a right-aligned column — the panel and its detail alike (#865)
  assert.equal((html.match(/<dd class="n">[\d,]+ k tokens<\/dd>/g) || []).length, 5);
  assert.ok(D.TIPS['delivered:832'].rows.every(([, v, n]) => / k tokens$/.test(v) && n === 'n'));
  for (const link of ["pr832", "issues/830", "run9"]) assert.match(html, new RegExp(`href="[^"]*${link}"`), link);
  assert.ok(!/unknown/.test(shown));
});

test('a PR outside any chantier: no tokens, no run — « unknown », never a zero', () => {
  D.DELIVERY[900] = { data: { error: null, pull: { number: 900, url: 'pr900', spec: null, tokens: null,
    times: { published: null, opened: 0, merged: HOUR_S, prod: null },
    ci: { runs: null, first_green: null, minutes: null, failed_tests: [], last_run_url: null },
    size: { changed_files: null, additions: null, deletions: null, closed_issues: null } } } };
  const html = D.deliveryOf({ number: 900, url: 'pr900' }), shown = plain(html);
  assert.ok(/Total unknown/.test(shown) && /CI runs unknown/.test(shown) && /files unknown/.test(shown), shown);
  assert.ok(/spec → PR unknown/.test(shown) && /merged → prod not in prod/.test(shown), shown);
  assert.ok(!/\b0 tokens|\bfiles 0\b|CI runs 0/.test(shown), 'a zero invented');
  assert.ok(!/kbar/.test(html), 'no bar without tokens');
  assert.match(plain(D.deliveryOf({ number: 7, url: 'x' })), /loading/);
  const unread = structuredClone(D.DELIVERY[832]); unread.data.pull.ci.failed_tests = null;
  D.DELIVERY[833] = unread;
  assert.match(plain(D.deliveryOf({ number: 833, url: 'x' })), /failed tests unknown/);  // a log not read: not « none »
});

// ---- « Livré » (#849): its dated rows, no « N since your visit » counter in its title ----
test('the Delivered column counts what it shows, never what is new since a visit', () => {
  const header = page.match(/post\('livre', [^,]+, ([^\n]+?), h\(K\.livreHint\)/);
  assert.ok(header, 'the Delivered column is drawn');
  assert.match(header[1], /deliveredRows\(DATA\.delivered, now\(\)\)\.count/);
  assert.ok(!/isNew|visit|visite/i.test(header[1]), `a visit counter in the title: ${header[1]}`);
  assert.ok(!/depuis ta visite|since your visit/i.test(page), 'no visit counter text left in the page');
});

// ---- everything the page shows is English (#850), and lives in TXT ----
/* French a reader would see: an accent, a « », or a common French word.
   English never needs them. */
const FRENCH = /[àâçéèêëîïôûùüÿœ«»]|\b(le|la|les|des|du|une|est|sont|pas|aucun|aucune|depuis|dans|pour|avec|sans|sur|en cours|il y a|inconnue?|illisibles?|libre|occupé|fusionnée?|chantiers?|semaine|mois|jours?|alertes?|attente|livré|ouvrir|voir)\b/i;
const visibleSource = src => src
  .replace(/\/\*[\s\S]*?\*\//g, '')           // comments
  .replace(/(^|[^:'"`])\/\/[^\n]*/g, '$1')    // line comments (not the :// of a URL)
  .replace(/<!--[\s\S]*?-->/g, '');
// a property read (`y.chantiers`) is the snapshot's JSON, not text
const frenchIn = src => visibleSource(src).split('\n').filter(line => FRENCH.test(line.replace(/\.[A-Za-z_]\w*/g, '')))
  // quality.py's rule names are data keys, read and never shown
  .filter(line => !/rule: \{ 'même commit'/.test(line));

test('no French left in the page: its words, its HTML, its CSS', () => {
  assert.deepEqual(frenchIn(page), []);
});

test('the guard sees French: one word put back is caught (counter-proof)', () => {
  const one = page.replace("noData: 'no data yet'", "noData: 'aucune donnée encore'");
  assert.notEqual(one, page);
  assert.equal(frenchIn(one).length, 1);
  assert.equal(frenchIn(page.replace("checkHint: 'the CI of each push'", "checkHint: 'la CI de chaque push'")).length, 1);
});

test('every TXT key the page reads exists', () => {
  const words = new Function(`${textSource}; return TXT;`)();
  const used = [...visibleSource(page).matchAll(/TXT\.([a-zA-Z]+)(?:\.([a-zA-Z]+))?/g)];
  assert.ok(used.length > 150, `the page reads its words from TXT (${used.length})`);
  for (const [whole, key, sub] of used) {
    assert.ok(key in words, `${whole}: no such key`);
    const v = words[key];
    if (sub && v && typeof v === 'object' && !Array.isArray(v)) assert.ok(sub in v, `${whole}: no such key`);
  }
});

// ---- the queue's next pass (#869): its hour in the BROWSER's time zone, a
// countdown on the server's clock, the refusal start-next would give ----
test('the next start: its hour, its countdown, why it would launch nothing', () => {
  const R = new Function(`${textSource}; ${quotaSource};
    const esc = s => String(s ?? '');
    ${between('/* jobs:begin', '/* jobs:end */')}
    ${between('/* relauncher:begin', '/* relauncher:end */')}
    return { nextStart, countdown };`)();
  const at = 1_790_414_100, q = [{ number: 1 }];
  const hm = new Date(at * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  const ok = R.nextStart({ next_at: at, refusal: null }, q, at - 252);
  assert.ok(ok.includes(`next start: <b>${hm}</b>`), ok);
  // the browser's zone, never a fixed one: test_dashboard_page.py runs this under two
  const zoned = { 'America/New_York': '05:15', 'Asia/Tokyo': '18:15' }[process.env.TZ];
  if (zoned) assert.equal(hm, zoned);
  assert.match(ok, /data-until="1790414100">in 4m 12s</);
  assert.doesNotMatch(ok, /class="next-why/);
  assert.equal(R.countdown(-3), 'now');

  const full = R.nextStart({ next_at: at, refusal: 'no new worker: 2 workers already running (#1, #2), maximum 2' }, q, at - 60);
  assert.match(full, /class="next-why[^"]*">no new worker: 2 workers already running \(#1, #2\), maximum 2</);

  const unknown = R.nextStart({ next_at: null, refusal: null }, q, at);
  assert.ok(unknown.includes('next start: <b>unknown</b>'), unknown);
  assert.doesNotMatch(unknown, /data-until/);
  assert.ok(R.nextStart(undefined, q, at).includes('<b>unknown</b>'), 'a snapshot from before #869');

  const empty = R.nextStart({ next_at: at, refusal: 'whatever' }, [], at);
  assert.ok(empty.includes('queue empty'), empty);
  assert.doesNotMatch(empty, /next start|data-until|whatever/);
});

test('a parked worker is in CI: its words, and for whom it was set aside (#1025, #1269)', () => {
  const R = new Function(`${textSource}; return { TXT };`)();
  assert.equal(R.TXT.state.parked, 'parked (CI)');
  assert.equal(R.TXT.setAsideFor(1233), 'set aside for #1233');
});

test('a card says the chantier state in the words of status (#1729)', () => {
  const S = new Function('esc', `${textSource}; ${page.slice(page.indexOf('/* state:begin'), page.indexOf('/* state:end */'))}; return { stateOf };`)(x => String(x));
  assert.equal(S.stateOf({ phase: 'human', phase_text: 'waits for a human (permission: Bash command: rm x)', state: 'asking' }),
    '<span class="st-blocked">■ waits for a human (permission: Bash command: rm x)</span>');
  assert.equal(S.stateOf({ phase: 'light_check', phase_text: 'light check (PR #7)', state: 'idle' }),
    '<span class="st-ci">◌ light check (PR #7)</span>');
  assert.match(S.stateOf({ state: 'idle' }), /◇ idle/); // a snapshot without it
});

test('a card set aside says for whom', () => {
  const R = new Function(`${textSource}; const esc = s => String(s ?? '');
    ${page.slice(page.indexOf('/* state:begin'), page.indexOf('/* state:end */'))}
    return { stateOf };`)();
  assert.equal(R.stateOf({ state: 'stopped', role: 'set_aside', set_aside: { for: 1233 } }),
    '<span class="st-parked">◐ set aside for #1233</span>');
  assert.match(R.stateOf({ state: 'parked', role: 'ci' }), /parked \(CI\)/);
});

// The Factory tab as a light kanban (#1824): `kanban`, taken from the page
// between its markers, with the page's own words and escaping.
const kanbanSource = between('/* kanban:begin', '/* kanban:end */');
assert.ok(kanbanSource.length > 100, 'the kanban markers are in the page');
const ESC = new Function(`${page.match(/^const esc = .*$/m)[0]}; return esc;`)();
const K = new Function('esc', `${textSource}; ${kanbanSource};
  return { columnOf, toneOf, statusLine, needsOf, ciDots, prCi, deliveredRows, rankText, KANBAN_FOLD, TXT,
    SECTIONS, sectionsFor, queuePlace, queueLine, waitingColumns, usefulLabels, starMark, starRequest, starClick, settleStars, queueSays, queueOrder, placeWaitMark, sessionCount, workingCount };`)(ESC);
const chantier = (spec, extra = {}) => ({ spec, title: `Spec — chantier ${spec}`, state: 'active', phase: 'development',
  phase_text: 'in development', phase_reason: null, asking: null, blocked_by: null, current_ticket: null, ...extra });
const calmControl = () => ({ pull_requests: [], runners: [{ name: 'vps-1', status: 'online', busy: false }], waiting_jobs: [], recent_runs: [] });

test('sessions held by a resting merge-queue worker stay visible outside development', () => {
  const data = { order: [{ spec: 2102, role: 'development' }, { spec: 2121, role: 'development' }], workers: [chantier(2102, { state: 'idle', phase: 'merge_queue' }), chantier(2121)] };
  assert.equal(K.sessionCount(data), 2);
  assert.equal(K.workingCount(data), 1);
  assert.equal(K.TXT.kanban.devHint(5, 6, 2, K.sessionCount(data), K.workingCount(data)), '5/6 in flight · 1 working · 2/2 sessions occupied');
  assert.deepEqual(K.statusLine(chantier(2102, { phase: 'merge_queue', state: 'idle', session_occupied: true, session_hold_reason: '1 uncommitted file(s)' })),
    { word: 'session occupied', tone: '', why: '1 uncommitted file(s)' });
  assert.equal(K.statusLine(chantier(2102, { phase: 'merge_queue', state: 'parked', session_occupied: false })), null);
  assert.equal(K.sessionCount({ order: [] }), 0);
  assert.equal(K.sessionCount({ order: null }), null);
  assert.equal(K.workingCount({ workers: [] }), 0);
  assert.equal(K.workingCount({ workers: null }), null);
  assert.equal(K.workingCount({ workers: [], sessions_readable: false }), null);
  const parked = { ...data, order: [{ spec: 2102, role: 'ci' }, { spec: 2121, role: 'development' }] };
  assert.equal(K.sessionCount(parked), 1);
  assert.equal(K.queueSays({ ...parked, in_flight: 5, in_flight_limit: 6, max_workers: 2 }), 'starts at the next pass');
});

test('each phase has its column: light check, merge queue, and in development everything else', () => {
  const at = phase => K.columnOf(chantier(1, { phase }));
  assert.equal(at('light_check'), 'check');
  assert.equal(at('merge_queue'), 'mq');
  for (const phase of ['development', 'human', 'blocked', 'parked', 'done', 'queued', undefined]) assert.equal(at(phase), 'dev', phase);
});

test('a card\'s edge: green while its worker works, violet when it waits for Sam, red when blocked, none otherwise', () => {
  assert.equal(K.toneOf(chantier(1)), 'work');
  assert.equal(K.toneOf(chantier(1, { state: 'asking', asking: 'may I?' })), 'you');
  assert.equal(K.toneOf(chantier(1, { blocked_by: { number: 9, reason: 'which one?' } })), 'you');
  assert.equal(K.toneOf(chantier(1, { phase: 'human', state: 'idle' })), 'you');
  assert.equal(K.toneOf(chantier(1, { phase: 'blocked', state: 'dead' })), 'bad');
  assert.equal(K.toneOf(chantier(1, { state: 'idle' })), '', 'idle: not working');
  assert.equal(K.toneOf(chantier(1, { phase: 'light_check' })), '', 'a working worker in light check: the column says it');
});

test('the state line only in development (the ticket worked on) or when something is wrong', () => {
  const working = K.statusLine(chantier(1, { current_ticket: { number: 12, title: 'the tiles' } }));
  assert.deepEqual([working.word, working.why], ['', '#12 the tiles'], 'working: the ticket, never the word');
  assert.equal(K.statusLine(chantier(1, { phase: 'light_check', phase_reason: 'PR #7, CI running' })), null);
  assert.equal(K.statusLine(chantier(1, { phase: 'merge_queue', state: 'parked' })), null);
  const asking = K.statusLine(chantier(1, { phase: 'light_check', state: 'asking', asking: 'Bash: rm x' }));
  assert.deepEqual([asking.word, asking.why], ['waits for you', '⚑ Bash: rm x']);
  const dead = K.statusLine(chantier(1, { phase: 'blocked', state: 'dead', phase_reason: 'its window has no provider' }));
  assert.deepEqual([dead.word, dead.why], ['blocked', 'its window has no provider']);
  const parked = K.statusLine(chantier(1, { phase: 'parked', state: 'parked', phase_text: 'parked (set aside for #3)' }));
  assert.deepEqual([parked.word, parked.why], ['', 'parked (set aside for #3)']);
});

test('the banner: nothing to do when nothing waits for Sam', () => {
  assert.deepEqual(K.needsOf({ workers: [chantier(1), chantier(2, { phase: 'light_check', state: 'parked' })], control: calmControl() }), []);
});

test('the banner lists a question, a needs-human, a red CI, a runner off line — one line each', () => {
  const control = calmControl();
  control.pull_requests.push({ number: 70, title: 'red one', ci: 'failure' }, { number: 71, title: 'running', ci: 'running' });
  control.runners.push({ name: 'vps-2', status: 'offline', busy: false });
  const needs = K.needsOf({ workers: [chantier(1, { state: 'asking', asking: 'Bash: rm x' }), chantier(2),
    chantier(3, { blocked_by: { number: 30, reason: 'which template?' } }), chantier(4, { phase: 'blocked', state: 'dead' })], control });
  assert.deepEqual(needs.map(n => [n.num, n.title, n.why, n.tone]), [
    ['#1', 'chantier 1', '⚑ Bash: rm x', 'you'],
    ['#3', 'chantier 3', '⚑ #30 : which template?', 'you'],
    ['#4', 'chantier 4', 'blocked', 'bad'],
    ['PR #70', 'red one', 'CI red, no worker', 'bad'],
    ['vps-2', '', 'runner off line', 'bad'],
  ]);
  assert.deepEqual(needs.map(n => n.open && n.open.kind), ['work', 'work', 'work', 'pr', 'runner']);
});

// A red CI waits for Sam only when nobody holds it (#2057): its worker at work, parked with a
// resume due, or a machine's re-run hold it; at rest past the threshold, or without a worker, it is his.
const NOW_S = 1790400000;
const redPull = (number, extra = {}) => ({ number, title: `red ${number}`, ci: 'failure', red_owner: null, red_unowned_since: NOW_S - 60, ...extra });
const redWorld = (pulls, workers = [], extra = {}) => ({ generated_at: NOW_S, red_unowned_minutes: 15, workers,
  control: { ...calmControl(), pull_requests: pulls }, ...extra });
const onPull = (spec, number, extra = {}) => chantier(spec, { pull_request: { number, ci: 'failure' }, ...extra });

test('the banner stays calm for a red its worker, its parking or its re-run holds', () => {
  for (const owner of ['worker', 'parked', 'machine']) {
    const world = redWorld([redPull(70, { red_owner: owner, red_unowned_since: null })], [onPull(1, 70)]);
    assert.deepEqual(K.needsOf(world), [], owner);
  }
  assert.deepEqual(K.needsOf(redWorld([redPull(70, { red_owner: 'machine', red_unowned_since: null })])), [],
    'a machine\'s re-run holds a red without a worker too');
});

test('a red nobody holds enters the banner past the threshold, with how long its worker rests', () => {
  const resting = since => redWorld([redPull(70, { red_unowned_since: NOW_S - since })], [onPull(1, 70, { state: 'idle' })]);
  assert.deepEqual(K.needsOf(resting(14 * 60)), [], 'under 15 min: its worker may still take it');
  const needs = K.needsOf(resting(20 * 60));
  assert.deepEqual(needs.map(n => [n.num, n.title, n.why, n.tone]), [['PR #70', 'red 70', 'CI red, its worker at rest for 20 min', 'bad']]);
  assert.equal(needs[0].open.kind, 'pr');
  assert.equal(K.needsOf(resting(135 * 60))[0].why, 'CI red, its worker at rest for 2 h 15 min');
  const stopped = redWorld([redPull(70, { red_unowned_since: NOW_S - 30 * 60 })], [onPull(1, 70, { state: 'stopped', phase: 'parked' })]);
  assert.equal(K.needsOf(stopped)[0].why, 'CI red, its worker stopped for 30 min');
});

test('a red without a worker is Sam\'s at once', () => {
  const needs = K.needsOf(redWorld([redPull(70, { red_unowned_since: NOW_S })]));
  assert.deepEqual(needs.map(n => [n.num, n.why]), [['PR #70', 'CI red, no worker']]);
});

test('the threshold is a setting', () => {
  const world = minutes => redWorld([redPull(70, { red_unowned_since: NOW_S - 6 * 60 })], [onPull(1, 70, { state: 'idle' })],
    { red_unowned_minutes: minutes });
  assert.equal(K.needsOf(world(5)).length, 1);
  assert.equal(K.needsOf(world(10)).length, 0);
});

test('a red whose worker the banner already lists is not said twice', () => {
  const needs = K.needsOf(redWorld([redPull(70, { red_unowned_since: NOW_S - 3600 })],
    [onPull(1, 70, { state: 'asking', asking: 'Bash: rm x' })]));
  assert.deepEqual(needs.map(n => n.num), ['#1']);
});

test('GitHub unreadable is never taken for calm (counter-proof)', () => {
  const needs = K.needsOf({ workers: [], control: { ...calmControl(), pull_requests: null, runners: null } });
  assert.deepEqual(needs.map(n => n.why), ['pull requests unknown (GitHub unreadable)', 'runners unknown (GitHub unreadable)']);
});

const job = (state, name = 'tests (pytest)') => ({ name, state, runner: state === 'running' ? 'vps-1' : null });
const dotsOf = out => (out.match(/<span class="dots">(.*?)<\/span>/) || [, ''])[1].match(/<i class="[a-z]*"><\/i>/g) || [];

// A PR's line on a card (#2058): a red its worker holds loses its dots for the count of reds; a new CI
// shows its dots and the reds before; a red nobody holds keeps its red dots, counted; no red, nothing new.
const PLAIN = '<span>plain</span>';
const pullOf = (ci, jobs, extra = {}) => ({ number: 70, ci, ci_jobs: jobs, reds: 0, red_owner: null, ...extra });

test('a red its worker holds: no dots, only the count of reds', () => {
  for (const owner of ['worker', 'parked', 'machine']) {
    const out = K.prCi(pullOf('failure', [job('success'), job('failure')], { reds: 2, red_owner: owner }), PLAIN);
    assert.equal(dotsOf(out).length, 0, owner);
    assert.match(out, /<span class="reds">✗ 2 reds<\/span>/);
    assert.doesNotMatch(out, /plain|1\/2/, 'nothing else said');
  }
  assert.match(K.prCi(pullOf('failure', [job('failure')], { reds: 1, red_owner: 'worker' }), PLAIN), /✗ 1 red</);
});

test('a new CI shows its dots, and the reds before it', () => {
  const out = K.prCi(pullOf('running', [job('success'), job('running')], { reds: 2 }), PLAIN);
  assert.equal(dotsOf(out).length, 2);
  assert.match(out, /<span class="reds">✗ 2 reds before<\/span>/);
});

test('a red nobody holds keeps its red dots, with its count', () => {
  const out = K.prCi(pullOf('failure', [job('success'), job('failure')], { reds: 3 }), PLAIN);
  assert.deepEqual(dotsOf(out), ['<i class="ok"></i>', '<i class="ko"></i>']);
  assert.match(out, /<span class="reds">✗ 3 reds<\/span>/);
});

test('no red: the dots as before, nothing new; no job: the plain CI state', () => {
  const out = K.prCi(pullOf('running', [job('running')]), PLAIN);
  assert.equal(dotsOf(out).length, 1);
  assert.doesNotMatch(out, /reds/);
  assert.equal(K.prCi(pullOf('queued', []), PLAIN), PLAIN);
});

test('a PR rerun green has no red dot: its jobs are the last attempt\'s (#2079)', () => {
  // #2063, 29/10: run 40917525833 green on its 2nd attempt, the card showed 9/11 and two reds.
  // `--failed` reran the two red tranches; the nine others stay the 1st attempt's, green.
  const jobs = [...Array(9).fill(0).map(() => ({ ...job('success'), attempt: 1, current: true })),
    { ...job('success', 'integration (1/2)'), attempt: 2, current: true },
    { ...job('success', 'integration (2/2)'), attempt: 2, current: true }];
  const out = K.prCi(pullOf('success', jobs), PLAIN);
  assert.equal(dotsOf(out).filter(d => d.includes('ko')).length, 0);
  assert.match(out, /11\/11/);
});

test('a PR\'s CI in dots, one a job, and its count of green ones; skipped jobs left out', () => {
  const out = K.ciDots([job('success'), job('success'), job('success'), job('running'), job('waiting'), job('skipped')]);
  assert.deepEqual(dotsOf(out), ['<i class="ok"></i>', '<i class="ok"></i>', '<i class="ok"></i>', '<i class="run"></i>', '<i class=""></i>']);
  assert.match(out, /<span>3\/5<\/span>$/);
  assert.match(out, /title="tests \(pytest\): green\ntests \(pytest\): green\ntests \(pytest\): green\ntests \(pytest\): running · vps-1\ntests \(pytest\): waiting"/);
  assert.match(K.ciDots([job('failure')]), /<i class="ko"><\/i>.*0\/1/);
});

test('« 0/1 » while only `changes` exists; no job at all draws nothing', () => {
  assert.match(K.ciDots([job('running', 'changes (which jobs this change needs)')]), /<span>0\/1<\/span>/);
  assert.equal(K.ciDots([]), '');
  assert.equal(K.ciDots(null), '');
  assert.equal(K.ciDots([job('skipped')]), '');
});

test('past 12 jobs the dots fold into one bar by state; at 12 they stay dots', () => {
  assert.equal(K.KANBAN_FOLD, 12);
  const twelve = K.ciDots(Array.from({ length: 12 }, () => job('success')));
  assert.equal(dotsOf(twelve).length, 12);
  const twenty = [...Array(9).fill('success'), ...Array(4).fill('running'), job('failure').state, ...Array(6).fill('waiting')].map(st => job(st));
  const out = K.ciDots(twenty);
  assert.equal(dotsOf(out).length, 0, 'no dot past the fold');
  assert.match(out, /<span class="cibar"><i class="ok" style="flex:9"><\/i><i class="run" style="flex:4"><\/i><i class="ko" style="flex:1"><\/i><i class="" style="flex:6"><\/i><\/span>/);
  assert.match(out, /<span>9\/20<\/span>$/);
});

test('a job\'s name in the CI\'s detail is escaped', () => {
  assert.match(K.ciDots([job('success', '<b>x</b>')]), /title="&lt;b&gt;x&lt;\/b&gt;: green"/);
});

const NOW = 1_792_900_000, HOUR = 3600;
const pr = (number, hoursAgo) => ({ kind: 'pr', number, title: `PR ${number}`, url: `u${number}`, at: NOW - hoursAgo * HOUR });
const deploy = hoursAgo => ({ kind: 'deploy', title: 'prod', url: `d${hoursAgo}`, at: NOW - hoursAgo * HOUR });

test('Delivered stacks merges and deployments, newest first, and counts the merges not in prod yet', () => {
  const out = K.deliveredRows([pr(1, 10), deploy(5), pr(2, 4), pr(3, 1), deploy(20), pr(4, 30)], NOW);
  assert.deepEqual(out.rows.map(r => r.kind === 'pr' ? r.d.number : 'deploy'), [3, 2, 'deploy', 1, 'deploy', 4]);
  assert.equal(out.pending, 2, 'the two above the latest deployment');
  assert.equal(out.count, 4);
  assert.equal(out.older, 0);
});

test('Delivered keeps 48 h; without a deployment in them, every merge is not in prod yet', () => {
  const out = K.deliveredRows([pr(1, 2), pr(2, 47.9), pr(3, 48), deploy(60)], NOW);
  assert.deepEqual(out.rows.map(r => r.d.number), [1, 2]);
  assert.equal(out.pending, 2);
  assert.equal(out.count, 2);
  assert.equal(K.deliveredRows([deploy(1), pr(1, 2)], NOW).pending, 0, 'just deployed: none waits');
});

test('Delivered shows 16 merges, then how many older ones', () => {
  const many = Array.from({ length: 20 }, (_, i) => pr(i + 1, i + 1));
  const out = K.deliveredRows([...many, deploy(10.5)], NOW);
  assert.equal(out.rows.filter(r => r.kind === 'pr').length, 16);
  assert.deepEqual([out.older, out.count, out.pending], [4, 20, 10]);
  assert.equal(out.rows.filter(r => r.kind === 'deploy').length, 1);
  assert.equal(K.deliveredRows([...many, deploy(19.5)], NOW).rows.some(r => r.kind === 'deploy'), false, 'a deployment past the 16th merge is not drawn');
});

test('a rank reads 1st, 2nd, 3rd, 4th… 11th, 12th, 13th, 21st; none when the snapshot has none', () => {
  assert.deepEqual([1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111, 112].map(K.rankText),
    ['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '23rd', '101st', '111th', '112th']);
  assert.equal(K.rankText(undefined), '');
  assert.equal(K.rankText(null), '');
});

// ---- a chantier's panel (#1819): its six sections, the one of its card's column first ----
test('the section of the card\'s column comes first; the others keep their fixed order', () => {
  assert.deepEqual(K.SECTIONS, ['work', 'tickets', 'ci', 'mq', 'story', 'spec']);
  assert.deepEqual(K.sectionsFor('queue', false), ['tickets', 'work', 'ci', 'mq', 'story', 'spec']);
  assert.deepEqual(K.sectionsFor('dev', false), ['work', 'tickets', 'ci', 'mq', 'story', 'spec']);
  assert.deepEqual(K.sectionsFor('check', false), ['ci', 'work', 'tickets', 'mq', 'story', 'spec']);
  assert.deepEqual(K.sectionsFor('mq', true), ['ci', 'work', 'tickets', 'mq', 'story', 'spec'], 'in test: its PR and CI');
  assert.deepEqual(K.sectionsFor('mq', false), ['mq', 'work', 'tickets', 'ci', 'story', 'spec'], 'in line: its place');
  for (const col of ['queue', 'dev', 'check', 'mq']) for (const inTest of [true, false]) {
    const order = K.sectionsFor(col, inTest);
    assert.equal(new Set(order).size, 6, `${col}: each section once`);
    assert.deepEqual(order.slice(1), K.SECTIONS.filter(k => k !== order[0]), `${col}: the rest in the fixed order`);
  }
});

const queue = (testing, ...waiting) => ({ testing: testing ? { pull: testing } : null, waiting: waiting.map(pull => ({ pull })) });

test('a pull request\'s place in the merge queue: in test, its place in line, or not in it', () => {
  const q = queue(10, 11, 12, 13);
  assert.deepEqual([10, 11, 12, 13, 14, null].map(n => K.queuePlace(q, n)), ['test', 0, 1, 2, null, null]);
  assert.equal(K.queuePlace(null, 10), null, 'an unreadable queue places nothing');
});

test('its place said in words: in test, the PRs ahead, or next', () => {
  assert.equal(K.queueLine(queue(10, 11, 12), 10), 'in test: its full suite runs');
  assert.equal(K.queueLine(queue(10, 11, 12), 11), '2nd in line: tested after the PR in test');
  assert.equal(K.queueLine(queue(10, 11, 12), 12), '3rd in line: tested after the 1 PR ahead and the PR in test');
  assert.equal(K.queueLine(queue(null, 11, 12, 13), 13), '3rd in line: tested after the 2 PRs ahead');
  assert.equal(K.queueLine(queue(null, 11), 11), '1st in line: tested after nothing — next to be tested');
  assert.equal(K.queueLine(queue(10), 99), 'not in the merge queue');
});

test('a pull request that entered on an earlier head\'s green says it (#1844)', () => {
  const q = { testing: { pull: 10, reused_from: 'abcdef0123456789' }, waiting: [{ pull: 11, reused_from: '0123456789abcdef' }] };
  assert.equal(K.queueLine(q, 10), 'in test: its full suite runs — entered on the light lane of abcdef01 (same code diff)');
  assert.equal(K.queueLine(q, 11), '2nd in line: tested after the PR in test — entered on the light lane of 01234567 (same code diff)');
});

test('a pull request waiting whose run the queue cancelled says it (#2109)', () => {
  const run = 'https://github.com/o/r/actions/runs/41044208987';
  const q = { testing: { pull: 10, cancelled: null }, waiting: [{ pull: 11, cancelled: { url: run, at: 1 } }, { pull: 12, cancelled: null }] };
  assert.equal(K.queueLine(q, 11), '2nd in line: tested after the PR in test — its run 41044208987 cancelled: the queue reruns everything at its turn');
  assert.equal(K.queueLine(q, 12), '3rd in line: tested after the 1 PR ahead and the PR in test');
  assert.equal(K.queueLine(q, 10), 'in test: its full suite runs');
});

// ---- Waiting on GitHub (#1820): a tab, a column a nature ----
test('Waiting on GitHub: a column a nature, in order, each with its count and its line', () => {
  const issue = (number, labels = []) => ({ number, title: `issue ${number}`, labels });
  const cols = K.waitingColumns({ specs: [issue(1, ['spec']), issue(2, ['spec'])], bugs: [issue(3, ['bug'])], tasks: [], ideas: [issue(4, ['idea'])], to_sort: [issue(5)] });
  assert.deepEqual(cols.map(c => [c.name, c.items.map(i => i.number)]),
    [['Specs ready', [1, 2]], ['Bugs', [3]], ['Tasks', []], ['Ideas', [4]], ['To sort', [5]]]);
  assert.deepEqual(cols.map(c => c.hint), ['to launch on your word', 'something broken', 'measures, settings, clean-ups', 'not asked for yet', 'no nature: to label']);
});

test('a nature absent from the snapshot is an empty column; GitHub unreadable is unknown, never empty', () => {
  const cols = K.waitingColumns({ specs: [{ number: 1 }] });
  assert.deepEqual(cols.map(c => c.items.length), [1, 0, 0, 0, 0]);
  assert.equal(K.waitingColumns(null), null);
  assert.equal(K.waitingColumns(undefined), null);
  assert.equal(K.waitingColumns([{ number: 1 }]), null, 'a snapshot from before #857: unknown');
});

test('a row shows the labels its column does not already say', () => {
  assert.deepEqual(K.usefulLabels(['ready-for-agent', 'spec', 'needs-human', 'to-launch']), ['needs-human', 'to-launch']);
  assert.deepEqual(K.usefulLabels(['bug', 'priority']), ['priority']);
  assert.deepEqual(K.usefulLabels(undefined), []);
});

test('a starred card shows ⭐, an unstarred one ☆ to click, and its label is not repeated (#1875)', () => {
  assert.match(K.starMark({ number: 1, is_starred: true }), /<span class="star on" role="button" tabindex="0" aria-pressed="true" data-star="1" data-on="true" title="starred: your pick[^"]*">⭐<\/span>/);
  const off = K.starMark({ number: 2, is_starred: false });
  assert.match(off, /class="star" [^>]*data-on="false"[^>]*>☆</);
  assert.ok(!off.includes('⭐'));
  assert.equal(K.starMark({ number: 3 }), K.starMark({ number: 3, is_starred: false }), 'a snapshot from before #1875: no star');
  // both cards draw it: the queue's and Waiting on GitHub's
  assert.match(page, /const queueCard = [^\n]*\n[^\n]*\$\{starMark\(s, STARS\)\}/);
  assert.match(page, /function waitingRow\([^]*?const head = [^\n]*\$\{starMark\(i, STARS\)\}/);
  // ☆ shows on hover, and always on touch
  assert.match(page, /\.star:not\(\.on\)\{opacity:0/);
  assert.match(page, /@media \(hover:none\)\{\.star:not\(\.on\)\{opacity:\.6\}\}/);
});

test('a click posts the star with the action header, and shows it at once (#1877)', async () => {
  const pending = {}, sent = [], drawn = [];
  const send = async request => { sent.push(request); return { ok: true, status: 200, text: async () => '' }; };
  const card = { number: 42, is_starred: false };
  const clicked = K.starClick(pending, 42, false, send, () => drawn.push(K.starMark(card, pending)), 1000);
  assert.ok(drawn[0].includes('⭐'), 'flipped before the answer');
  await clicked;
  assert.equal(sent.length, 1);
  assert.equal(sent[0].method, 'POST');
  assert.equal(sent[0].headers['X-Agentctl-Action'], 'star');
  assert.equal(sent[0].headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(sent[0].body), { number: 42, is_starred: true });
  assert.ok(K.starMark(card, pending).includes('⭐'), 'still starred until the snapshot says it');
  K.settleStars(pending, { queue: [], waiting: { bugs: [{ number: 42, is_starred: false }] } }, 1010);
  assert.ok(K.starMark(card, pending).includes('⭐'), 'an older snapshot does not undo the click');
  K.settleStars(pending, { queue: [], waiting: { bugs: [{ number: 42, is_starred: true }] } }, 1020);
  assert.deepEqual(pending, {}, 'the snapshot caught up');
});

test('a click that fails puts the state back and says why on the card', async () => {
  for (const send of [async () => ({ ok: false, status: 409, text: async () => '#42 is closed\n' }),
    async () => { throw new Error('network down'); }]) {
    const pending = {}, card = { number: 42, is_starred: true };
    await K.starClick(pending, 42, true, send, () => {}, 1000);
    const mark = K.starMark(card, pending);
    assert.ok(mark.includes('⭐'), 'back to starred');
    assert.match(mark, /class="starerr r"[^>]*>✗ star not saved: (HTTP 409 #42 is closed|network down)</);
    K.settleStars(pending, { queue: [card], waiting: {} }, 1010);
    assert.ok(K.starMark(card, pending).includes('starerr'), 'the message stays a while');
    K.settleStars(pending, { queue: [card], waiting: {} }, 1016);
    assert.ok(!K.starMark(card, pending).includes('starerr'));
  }
});

test('a star click opens neither the card nor its GitHub link; Enter and Space click it too', () => {
  assert.match(page, /const star = starAt\(e\); if \(star\) return onStar\(e, star\);\n  if \(e\.target\.closest\('\[data-refresh\]'\)\)/);
  assert.match(page, /function onStar\(e, star\) \{\n  e\.preventDefault\(\); e\.stopPropagation\(\);/);
  assert.match(page, /if \(star && \(e\.key === 'Enter' \|\| e\.key === ' '\)\) return onStar\(e, star\);/);
  assert.match(page, /settleStars\(STARS, DATA, now\(\)\)/);
});

// ---- the order (#1823): what the queue's head may hope ----
test('the queue says it starts at the next pass under the limit, else waits for a place', () => {
  assert.equal(K.queueSays({ in_flight: 5, in_flight_limit: 6, order: [] }), 'starts at the next pass');
  assert.equal(K.queueSays({ in_flight: 6, in_flight_limit: 6 }), 'waits for a place: 6 in flight, the limit reached');
  const busy = { in_flight: 3, in_flight_limit: 6, max_workers: 2, order: [{ role: 'development' }, { role: 'development' }, { role: 'ci' }] };
  assert.equal(K.queueSays(busy), 'waits for a session: 2/2 busy', 'under the limit, every session busy: start-next refuses');
  assert.equal(K.queueSays({ ...busy, max_workers: 3 }), 'starts at the next pass');
  assert.equal(K.queueSays({ in_flight: null, in_flight_limit: 6 }), '', 'the order unknown: nothing promised');
  assert.equal(K.queueSays({}), '', 'a snapshot from before the order');
});

// ---- a spec waiting for a place (#2118): it heads the queue, said ----
test('the specs waiting for a place head the queue in their rank, then priority, then the rest', () => {
  const q = [{ number: 1, place_wait: null }, { number: 2, prioritized: true, place_wait: null },
    { number: 3, place_wait: { rank: 2, since: 10 } }, { number: 4, place_wait: { rank: 1, since: 5 } }];
  assert.deepEqual(K.queueOrder(q).map(s => s.number), [4, 3, 2, 1]);
  assert.deepEqual(K.queueOrder([{ number: 1 }, { number: 2, prioritized: true }]).map(s => s.number), [2, 1], 'a snapshot from before');
});

test('a spec waiting for a place says it, its rank and since when; the others nothing', () => {
  const mark = K.placeWaitMark({ place_wait: { rank: 1, since: 1000 } }, 1000 + 3 * 3600);
  assert.match(mark, /waits for a place · 1st · 3h/);
  assert.match(mark, /title="its start was refused for want of a place: it comes back before any new spec"/);
  assert.equal(K.placeWaitMark({ place_wait: null }, 0), '');
  assert.equal(K.placeWaitMark({}, 0), '');
});

test('the kanban: five columns, no bubble, each column scrolling on its own down to the window\'s bottom', () => {
  assert.match(page, /\.plan\{[^}]*grid-template-columns:repeat\(5,minmax\(0,1fr\)\)[^}]*"queue dev check mq livre"/);
  assert.match(page, /\.plan > \.st\{background:none;border:0;border-radius:0/);
  assert.match(page, /\.fitmode \.plan > \.st\{[^}]*min-height:0[^}]*overflow-y:auto/);
  assert.match(page, /split\(' '\)\.length === 5/);
  assert.ok(!/post\('wait'/.test(page), 'Waiting on GitHub is no longer a column');
});

test('a card: 11.5 px, its title on two lines at most', () => {
  assert.match(page, /\.kcard\{[^}]*font-size:11\.5px/);
  assert.match(page, /\.kcard \.t\{[^}]*-webkit-line-clamp:2/);
  assert.ok(!/font-size:1[0-9](\.\d)?px/.test(page.match(/\.kcard \.[a-z]+\{[^}]*\}/g).join('').replace(/\.kcard \.h\{[^}]*\}/, '')), 'no text size of its own inside a card');
});

// A card's tickets (the operator, 16/10): one row of blocks of one size that slides
// sideways; `fitTickets`, taken from the page between its markers, run on a
// row of 49-px steps (46 + gap) that only knows its scroll and its classes.
const tkSource = between('/* tkrow:begin', '/* tkrow:end */');
assert.ok(tkSource.length > 100, 'the tkrow markers are in the page');
const fitTickets = new Function(`${tkSource}; return fitTickets;`)();
const STEP = 49;
const ticketRow = (statuses, width) => {
  const row = { scrollLeft: 0, clientWidth: width, scrollWidth: statuses.length * STEP - 3, names: new Set() };
  row.classList = { toggle: (c, on) => on ? row.names.add(c) : row.names.delete(c) };
  row.getBoundingClientRect = () => ({ left: 100, width });
  row.tks = statuses.map((status, i) => ({ status, dataset: { key: `t${i + 1}` },
    getBoundingClientRect: () => ({ left: 100 + i * STEP - row.scrollLeft, width: 46 }) }));
  row.querySelector = sel => row.tks.find(t => `.tk.${t.status}` === sel) || null;
  return row;
};
const firstShown = row => Math.ceil(row.scrollLeft / STEP) + 1;  // the first block wholly in view

test('the ticket worked on is put in the middle of the row, both edges fading', () => {
  const row = ticketRow(['done', 'done', 'done', 'done', 'done', 'current', 'todo', 'todo', 'todo'], 5 * STEP);
  fitTickets(row);
  assert.equal(firstShown(row), 4, 'the 6th of 9 sits third of five');
  assert.deepEqual([...row.names].sort(), ['more-l', 'more-r']);
});

test('at the start of the row, the first ticket stays first: only the right edge fades', () => {
  const row = ticketRow(['current', 'todo', 'todo', 'todo', 'todo', 'todo', 'todo', 'todo', 'todo'], 5 * STEP);
  fitTickets(row);
  assert.equal(row.scrollLeft, 0);
  assert.deepEqual([...row.names], ['more-r']);
});

test('without a current ticket, the first to do is centred; near the end, held at the end', () => {
  const row = ticketRow(['done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'todo'], 5 * STEP);
  fitTickets(row);
  assert.equal(row.scrollLeft, row.scrollWidth - row.clientWidth);
  assert.deepEqual([...row.names], ['more-l']);
});

test('the hand moves the row: a refresh leaves it; the next ticket centres it again', () => {
  const row = ticketRow(['done', 'done', 'done', 'done', 'done', 'current', 'todo', 'todo', 'todo'], 5 * STEP);
  fitTickets(row);
  row.scrollLeft = 0;
  fitTickets(row);
  assert.equal(row.scrollLeft, 0, 'same ticket worked on: the row stays where it was moved');
  row.tks[5].status = 'done'; row.tks[6].status = 'current';
  fitTickets(row);
  assert.equal(firstShown(row), 5);
});

test('a row that holds all its tickets does not fade', () => {
  const row = ticketRow(['done', 'current', 'todo'], 5 * STEP);
  fitTickets(row);
  assert.equal(row.scrollLeft, 0);
  assert.equal(row.names.size, 0);
});

test('a ticket block keeps its size: the row slides instead of squeezing them', () => {
  assert.match(page, /\.tk\{flex:0 0 46px;/);
  assert.match(page, /\.tks-row\{[^}]*overflow-x:auto/);
});

// The memory low for long (#2096): on 29/10 it stayed near 1 GB all night, read by no one, and the kernel
// killed the runners. Past `[waits] low_memory_minutes`, the banner says it with the largest processes.
const lowWorld = minutes => ({ generated_at: NOW_S, low_memory_minutes: 15, workers: [], control: calmControl(),
  low_memory: { since: NOW_S - minutes * 60, top: [
    { pid: 201, name: 'node', mb: 7270, spec: 1958 }, { pid: 301, name: 'python3', mb: 1200, spec: null },
    { pid: 100, name: 'claude', mb: 400, spec: 1958 }] } });

test('the memory low for 16 min enters the banner, with its three largest processes and their worker', () => {
  const needs = K.needsOf(lowWorld(16));
  assert.deepEqual(needs.map(n => [n.key, n.num, n.why, n.tone]), [
    ['mem', 'VPS', 'memory low for 16 min: node 7.1 GB #1958 · python3 1.2 GB · claude 0.4 GB #1958', 'bad']]);
  assert.equal(needs[0].open, null);
});

test('the memory low for 10 min, or not low, stays calm', () => {
  assert.deepEqual(K.needsOf(lowWorld(10)), []);
  assert.deepEqual(K.needsOf({ ...lowWorld(60), low_memory: null }), []);
});

// ---- the Settings tab (#2190), taken from the page between its markers ----
const SETS = new Function('TAB_DATA', `${textSource}; ${between('/* settings:begin', '/* settings:end */')};
  const tabState = () => null;
  return { settingsChanges, settingsTab, boundsText, SET };`)({ settings: { data: null } });
const SETTINGS = [
  { key: 'max_workers', block: 'launches', label: 'Workers at most', unit: '', value: 2, default: 2, minimum: 1, maximum: 3, set: false },
  { key: 'min_available_memory_mb', block: 'launches', label: 'Minimum available memory', unit: 'MB', value: 4000, default: 2500, minimum: 1500, maximum: null, set: true },
];

test('« Save » sends only the values typed that differ from those in force', () => {
  assert.deepEqual(SETS.settingsChanges(SETTINGS, { max_workers: '2', min_available_memory_mb: ' 3000 ' }), { changes: { min_available_memory_mb: 3000 } });
  assert.deepEqual(SETS.settingsChanges(SETTINGS, { max_workers: '2' }), { changes: {} });
});

test('a value that is not a whole number is said, and nothing is sent', () => {
  for (const typed of ['', '1.5', '-1', 'two'])
    assert.deepEqual(SETS.settingsChanges(SETTINGS, { max_workers: typed }), { error: 'Workers at most: a whole number' });
});

test('the bounds said: from–to, or at least', () => {
  assert.equal(SETS.boundsText(SETTINGS[0]), 'from 1 to 3');
  assert.equal(SETS.boundsText(SETTINGS[1]), 'at least 1500');
});

test('each setting shows its value, its default, its bounds; « Default value » only when the file sets it', () => {
  const T = new Function('TAB_DATA', `${textSource}; ${between('/* settings:begin', '/* settings:end */')};
    const esc = ${ESC.toString()}; const tabState = () => null; return settingsTab;`)({ settings: { data: { blocks: [{ key: 'launches', label: 'Launches' }], settings: SETTINGS } } });
  const html = T();
  assert.match(html, /<h3><span>LAUNCHES<\/span>/);
  assert.match(html, /id="set-max_workers" data-block="launches" type="number"[^>]* min="1" max="3" value="2" data-setting="max_workers"/);
  assert.match(html, /id="set-min_available_memory_mb"[^>]* type="number"[^>]* min="1500" value="4000"/);
  assert.match(html, /default 2500 MB · at least 1500 · set by the state file/);
  assert.match(html, /default 2 · from 1 to 3 · the default/);
  assert.match(html, /data-reset="max_workers"[^>]*disabled/);
  assert.doesNotMatch(html, /data-reset="min_available_memory_mb"[^>]*disabled/);
  assert.match(html, /data-save="launches"/);
});

test('the gear opens the tab from the header, and the page writes to its own route', () => {
  assert.match(page, /<a class="gear [^"]*" href="#settings"/);
  const tabOf = new Function(`${between('/* tabs:begin', '/* tabs:end */')}; return tabOf;`)();
  assert.equal(tabOf('#settings'), 'settings');
  assert.equal(tabOf('#nowhere'), 'factory');
  assert.match(page, /fetch\('settings', request\)/);
  assert.match(page, /'X-Agentctl-Action': 'settings'/);
  assert.match(page, /name === 'settings' \? 'settings\.json'/);
});

test('the queue pause is a switch, sent as it is flipped; « Save » leaves it out (#2192)', () => {
  const pause = { key: 'queue.paused', block: 'launches', label: 'Queue paused', kind: 'bool', unit: '', value: true, default: false, minimum: null, maximum: null, set: true };
  assert.deepEqual(SETS.settingsChanges([pause, ...SETTINGS], { 'queue.paused': 'on', max_workers: '3' }), { changes: { max_workers: 3 } });
  const T = new Function('TAB_DATA', `${textSource}; ${between('/* settings:begin', '/* settings:end */')};
    const esc = ${ESC.toString()}; const tabState = () => null; return settingsTab;`)({ settings: { data: { blocks: [{ key: 'launches', label: 'Launches' }], settings: [pause] } } });
  const html = T();
  assert.match(html, /id="set-queue\.paused" data-block="launches" type="checkbox" role="switch" checked data-switch="queue\.paused"/);
  assert.match(html, /default off · saved at once · set by the state file/);
  assert.match(page, /if \(flip\) sendSettings\(flip\.dataset\.block, \{ \[flip\.dataset\.switch\]: flip\.checked \}\)\.then\(ok => \{ if \(!ok\) flip\.checked = !flip\.checked; \}\)/);
  assert.doesNotMatch(html, /data-save=/, 'a block of switches only has no « Save »');
});

test('a paused queue is said at the top of every tab, with the way to the settings', () => {
  assert.match(page, /\$\{DATA\.queue_paused \? `<div class="paused" data-key="paused">\$\{TXT\.settings\.paused\} <a href="#settings">/);
});

// ---- a worker's model and effort, asked and sent (#2252) ----
const escLine = page.slice(page.indexOf('const esc = '), page.indexOf('\n', page.indexOf('const esc = ')));
const L = new Function(`${textSource}; ${escLine}; ${between('/* level:begin', '/* level:end */')}; return { levelText, levelMark, levelWhy };`)();
const side = (asked, seen, more = {}) => ({ asked, seen, source: 'default', label: null, anomaly: null,
  differs: asked == null || seen == null ? null : !String(seen).includes(asked), ...more });

test('a worker\'s level: what was asked, plain when sent the same, its gap red', () => {
  const same = { model: side('opus', 'claude-opus-5-5'), effort: side('high', 'high') };
  assert.equal(L.levelText(same), 'opus · high');
  assert.ok(!L.levelMark(same).includes('lvl r'));
  const gap = { model: side('opus', 'claude-opus-5-5'), effort: { ...side('high', 'medium'), differs: true } };
  assert.equal(L.levelText(gap), 'opus · high asked — ≠ claude-opus-5-5 · medium sent');
  assert.match(L.levelMark(gap), /class="lvl r"/);
});

test('a worker\'s level unknown or unsent is said, and where each value comes from', () => {
  assert.equal(L.levelText({ model: side(null, null), effort: side(null, null) }), 'unknown');
  assert.equal(L.levelText({ model: side('opus', null), effort: side('high', null) }), 'opus · high asked (sent: ? · ?)');
  assert.equal(L.levelText({}), 'unknown');
  assert.equal(L.levelMark({}), '');
  const why = L.levelWhy({ model: side('sonnet', null, { source: 'label', label: 'model-sonnet' }),
    effort: side('high', null, { anomaly: 'effort-ultra unknown' }) });
  assert.equal(why, 'label model-sonnet · high: effort-ultra unknown');
});
