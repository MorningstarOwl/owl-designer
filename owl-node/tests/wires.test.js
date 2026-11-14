// The wire router and the organizer (owlnode/web/wires.js). Run by test_wires.py when node is installed:
//   node tests/wires.test.js
"use strict";
const assert = require("assert");
const path = require("path");
const W = require(path.join(__dirname, "..", "owlnode", "web", "wires.js"));

const node = (id, x, y, w = 220, h = 100, extra = {}) => ({ id, x, y, w, h, kind: "proc", ...extra });
const out = (n, dy = 50) => ({ node: n.id, x: n.x + n.w, y: n.y + dy });
const inp = (n, dy = 50) => ({ node: n.id, x: n.x, y: n.y + dy });
const segs = (r) => r.pts.slice(1).map((b, i) => [r.pts[i], b]);
const hits = (r, n, m = 1) => segs(r).some(([a, b]) => Math.max(a[0], b[0]) > n.x + m && Math.min(a[0], b[0]) < n.x + n.w - m && Math.max(a[1], b[1]) > n.y + m && Math.min(a[1], b[1]) < n.y + n.h - m);
const orthogonal = (r) => segs(r).every(([a, b]) => a[0] === b[0] || a[1] === b[1]);

// 1. level ports: one straight line
{
  const a = node("a", 0, 0), b = node("b", 400, 0);
  const r = W.route([a, b], [{ id: "w", net: "a|out", s: out(a), t: inp(b) }]).get("w");
  assert.strictEqual(r.pts.length, 2, "a straight wire has two points");
  assert.ok(/^M220,50 L400,50$/.test(r.d), r.d);
}
// 2. a block in the way: the wire goes around it, at right angles
{
  const a = node("a", 0, 0), wall = node("wall", 300, -100, 220, 300), b = node("b", 700, 0);
  const r = W.route([a, wall, b], [{ id: "w", net: "a|out", s: out(a), t: inp(b) }]).get("w");
  assert.ok(orthogonal(r), "right angles only");
  assert.ok(!hits(r, wall), "not through the block in the way");
  assert.ok(r.pts.length >= 6, "it took a detour");
}
// 3. backwards (a loop): out of the right side, around its own block, into the left side of the target
{
  const a = node("a", 600, 0), b = node("b", 0, 0);
  const r = W.route([a, b], [{ id: "w", net: "a|out", s: out(a), t: inp(b) }]).get("w");
  assert.ok(orthogonal(r) && !hits(r, a) && !hits(r, b));
  assert.ok(r.pts[1][0] > a.x + a.w, "leaves heading right");
  assert.ok(r.pts[r.pts.length - 2][0] < b.x, "arrives from the left");
}
// 4. wires sharing a channel get their own lanes, in an order that does not cross; one output's wires share a trunk
{
  const s1 = node("s1", 0, 0), s2 = node("s2", 0, 140), t1 = node("t1", 500, 200), t2 = node("t2", 500, 340);
  const R = W.route([s1, s2, t1, t2], [
    { id: "w1", net: "s1|out", s: out(s1), t: inp(t1) },
    { id: "w2", net: "s2|out", s: out(s2), t: inp(t2) },
    { id: "w3", net: "s1|out", s: out(s1), t: inp(t2, 80) }]);
  const vx = (id) => R.get(id).pts[1][0];
  assert.ok(Math.abs(vx("w1") - vx("w2")) >= 4, "two outputs: two lanes");
  assert.ok(vx("w2") < vx("w1"), "the wire that ends lower takes the left lane: no crossing");
  assert.strictEqual(vx("w1"), vx("w3"), "one output: one trunk");
  for (const id of ["w1", "w2"]) assert.ok(vx(id) > 220 + 8 && vx(id) < 500 - 8, "lanes stay in the channel");
}
// 5. boxed in (overlapping blocks): still a path, never an exception
{
  const a = node("a", 0, 0), b = node("b", 100, 20), c = node("c", 200, 40);
  const R = W.route([a, b, c], [{ id: "w", net: "a|out", s: out(a), t: inp(c) }, { id: "v", net: "c|out", s: out(c), t: inp(a) }]);
  assert.ok(R.get("w").d.startsWith("M") && orthogonal(R.get("v")));
}
// 6. organize: flow left to right, no overlaps, ports lined up, loops and unwired blocks handled
{
  const mk = (id, kind, x, y, h = 100) => ({ id, x, y, w: 220, h, kind, host: "hub", label: id, ports: { "s:out": 50, "t:in": 50 } });
  const ns = [mk("spk", "device", 0, 0), mk("lim", "proc", 0, 0), mk("mic", "device", 900, 500), mk("gain", "proc", 0, 0), mk("tone", "proc", 5, 5),
              mk("sat", "device", 50, 900), mk("fx", "proc", 60, 900), mk("idle1", "device", 0, 0), mk("idle2", "proc", 0, 0)];
  const e = (s, t) => ({ s, sp: "out", t, tp: "in" });
  const es = [e("mic", "gain"), e("gain", "lim"), e("lim", "spk"), e("tone", "lim"), e("sat", "fx"), e("fx", "sat")];
  const p = W.organize(ns, es);
  assert.strictEqual(Object.keys(p).length, ns.length, "every block gets a place");
  assert.ok(p.mic.x < p.gain.x && p.gain.x < p.lim.x && p.lim.x < p.spk.x, "mic → gain → limiter → speaker");
  assert.ok(p.tone.x < p.lim.x && p.tone.x >= p.gain.x, "a generator sits just before what it feeds");
  assert.ok(Math.abs(p.mic.y - p.gain.y) < 1, "a 1:1 wire runs level");
  assert.ok(p.sat.x !== p.fx.x, "a loop still spreads over columns");
  const main = Math.max(...["spk", "lim", "mic", "gain", "tone", "sat", "fx"].map((id) => p[id].y + 100));
  assert.ok(p.idle1.y > main && p.idle2.y > main, "unwired blocks are parked underneath");
  assert.ok(p.idle2.x < p.idle1.x, "processing blocks first");
  const placed = ns.map((n) => ({ ...n, ...p[n.id] }));
  for (const a of placed) for (const b of placed) if (a.id < b.id) assert.ok(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y, `${a.id} and ${b.id} overlap`);
  // and the organized design routes cleanly: no wire through any block
  const R = W.route(placed, es.map((w, i) => { const a = placed.find((n) => n.id === w.s), b = placed.find((n) => n.id === w.t); return { id: "w" + i, net: w.s + "|out", s: out(a), t: inp(b) }; }));
  for (const r of R.values()) for (const n of placed) assert.ok(!hits(r, n), "an organized design has no wire through a block");
  assert.deepStrictEqual(W.organize(ns, es), p, "the same input gives the same layout");
}
// 7. a crossing is drawn as a hop
{
  const a = node("a", 0, 0), b = node("b", 600, 0), c = node("c", 0, 300), d = node("d", 600, -300);
  const R = W.route([a, b, c, d], [{ id: "flat", net: "a|out", s: out(a), t: inp(b) }, { id: "up", net: "c|out", s: out(c), t: inp(d) }]);
  assert.ok(/ A4,4 /.test(R.get("flat").d), "the level wire hops over the one going up: " + R.get("flat").d);
  assert.ok(!/ A4,4 /.test(R.get("up").d));
}
// 8. organize keeps the blocks of a box together, and boxes that use different columns sit side by side
{
  const mk = (id, group, kind = "proc") => ({ id, x: 0, y: 0, w: 220, h: 100, kind, group, ports: { "s:out": 50, "t:in": 50 } });
  const e = (s, t) => ({ s, sp: "out", t, tp: "in" });
  const ns = [mk("m1", "in", "device"), mk("m2", "in", "device"), mk("p1", "fx"), mk("p2", "fx"), mk("spare", "fx"), mk("o1", "out", "device"), mk("o2", "out", "device"), mk("x", null), mk("y", null)];
  const p = W.organize(ns, [e("m1", "p1"), e("m2", "p2"), e("p1", "o1"), e("p2", "o2"), e("x", "y")]);
  const bounds = (g) => { const m = ns.filter((n) => n.group === g).map((n) => p[n.id]); return { x0: Math.min(...m.map((q) => q.x)), x1: Math.max(...m.map((q) => q.x)) + 220, y0: Math.min(...m.map((q) => q.y)), y1: Math.max(...m.map((q) => q.y)) + 100 }; };
  const A = bounds("in"), B = bounds("fx"), C = bounds("out");
  assert.ok(A.x1 < B.x0 && B.x1 < C.x0, "in | fx | out, left to right");
  assert.ok(Math.abs(A.y0 - B.y0) < 1 && Math.abs(B.y0 - C.y0) < 1, "side by side, not stacked");
  assert.ok(p.spare && p.spare.x === p.p1.x, "an unwired block stays in its box");
  for (const id of ["x", "y"]) assert.ok(p[id].y >= Math.max(A.y1, B.y1, C.y1) || p[id].x >= C.x1 || p[id].x + 220 <= A.x0, "blocks outside the boxes stay out of them");
}
// 9. nothing to do
assert.strictEqual(W.route([], []).size, 0);
assert.deepStrictEqual(W.organize([], []), {});
console.log("wires: ok");
