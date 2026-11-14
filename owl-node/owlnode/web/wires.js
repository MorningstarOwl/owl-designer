// Owl Designer: how wires are drawn and how a design is tidied up. No dependencies; designer.js uses it in the page and
// tests/wires.test.js runs it under node.
//
//   route(nodes, edges)     every wire as right-angle segments that go AROUND blocks, never through them. Wires that
//                           would run on top of each other are spread into lanes in the free space between the blocks,
//                           ordered so they cross as little as possible; wires from the same output share one trunk.
//   organize(nodes, edges)  new block positions: signal flows left to right (sources left, outputs right), blocks are
//                           ordered and lined up so most wires run straight, unwired blocks are parked underneath.
"use strict";
(function (root) {
  const CLEAR = 14;     // free space between a wire and a block it passes
  const THROUGH = 400;  // ... and what entering a block's space costs: only when boxed in
  const BEND = 30;      // what a corner costs, in px of wire: fewer corners beat a slightly shorter wire
  const LANE = 10;      // distance between parallel wires
  const PAD = 8;        // lanes keep this far from blocks
  const RADIUS = 7;     // corner rounding
  const HOP = 4;        // radius of the little bridge where a wire crosses another

  class Heap {          // min-heap of (key, value)
    constructor() { this.k = []; this.v = []; }
    get size() { return this.k.length; }
    push(key, val) {
      const k = this.k, v = this.v; let i = k.length; k.push(key); v.push(val);
      while (i > 0) { const p = (i - 1) >> 1; if (k[p] <= key) break; k[i] = k[p]; v[i] = v[p]; i = p; }
      k[i] = key; v[i] = val;
    }
    pop() {             // returns the value; the key is in this.top
      const k = this.k, v = this.v, out = v[0]; this.top = k[0];
      const key = k.pop(), val = v.pop(), n = k.length;
      if (n) {
        let i = 0;
        for (;;) { let c = 2 * i + 1; if (c >= n) break; if (c + 1 < n && k[c + 1] < k[c]) c++; if (k[c] >= key) break; k[i] = k[c]; v[i] = v[c]; i = c; }
        k[i] = key; v[i] = val;
      }
      return out;
    }
  }

  const simplify = (pts) => {       // drop repeated points and points in the middle of a straight run
    const out = [];
    for (const p of pts) {
      const n = out.length;
      if (n && out[n - 1][0] === p[0] && out[n - 1][1] === p[1]) continue;
      if (n > 1 && ((out[n - 2][0] === out[n - 1][0] && out[n - 1][0] === p[0]) || (out[n - 2][1] === out[n - 1][1] && out[n - 1][1] === p[1]))) out[n - 1] = p;
      else out.push(p);
    }
    return out;
  };

  // nodes: [{id, x, y, w, h}]   edges: [{id, net, s: {node, x, y}, t: {node, x, y}}]  (s = an output on the right edge
  // of its block, t = an input on the left edge; net = what identifies the output, shared by all wires leaving it)
  function route(nodes, edges) {
    const result = new Map();
    if (!edges.length) return result;
    const byId = new Map(nodes.map((n) => [n.id, n]));
    // a sparse grid: wires only ever turn on the clearance lines around blocks and on the height of a port
    const xsSet = new Set(), ysSet = new Set();
    for (const n of nodes) { xsSet.add(n.x - CLEAR); xsSet.add(n.x + n.w + CLEAR); ysSet.add(n.y - CLEAR); ysSet.add(n.y + n.h + CLEAR); }
    const jobs = [];
    for (const e of edges) {
      const sn = byId.get(e.s.node), tn = byId.get(e.t.node);
      const sy = e.s.y, ty = Math.abs(e.t.y - e.s.y) <= 3 ? e.s.y : e.t.y;      // nearly level: draw it straight
      const ax = sn ? sn.x + sn.w + CLEAR : e.s.x + CLEAR, bx = tn ? tn.x - CLEAR : e.t.x - CLEAR;
      xsSet.add(ax); xsSet.add(bx); ysSet.add(sy); ysSet.add(ty);
      jobs.push({ e, ax, bx, sy, ty });
    }
    const xs = [...xsSet].sort((a, b) => a - b), ys = [...ysSet].sort((a, b) => a - b);
    const xi = new Map(xs.map((v, i) => [v, i])), yi = new Map(ys.map((v, i) => [v, i]));
    const nx = xs.length, ny = ys.length;
    // hBlock[iy*nx+ix]: the piece from (ix, iy) to (ix+1, iy) runs through a block (or its clearance); vBlock likewise down
    const hBlock = new Uint8Array(nx * ny), vBlock = new Uint8Array(nx * ny);
    for (const n of nodes) {
      const l = xi.get(n.x - CLEAR), r = xi.get(n.x + n.w + CLEAR), t = yi.get(n.y - CLEAR), b = yi.get(n.y + n.h + CLEAR);
      for (let iy = t + 1; iy < b; iy++) for (let ix = l; ix < r; ix++) hBlock[iy * nx + ix] = 1;
      for (let ix = l + 1; ix < r; ix++) for (let iy = t; iy < b; iy++) vBlock[iy * nx + ix] = 1;
    }
    // A* over (grid point, heading); headings 0 = east, 1 = south, 2 = west, 3 = north
    const N = nx * ny * 4, g = new Float64Array(N), from = new Int32Array(N), stamp = new Uint32Array(N);
    const DX = [1, 0, -1, 0], DY = [0, 1, 0, -1];
    let gen = 0;
    const paths = [];
    for (const j of jobs) {
      gen++;
      const sx = xi.get(j.ax), sy = yi.get(j.sy), gx = xi.get(j.bx), gy = yi.get(j.ty), X = xs[gx], Y = ys[gy];
      const heap = new Heap();
      const s0 = (sy * nx + sx) * 4;                 // leaves the output heading east
      g[s0] = 0; stamp[s0] = gen; from[s0] = -1;
      heap.push(Math.abs(xs[sx] - X) + Math.abs(ys[sy] - Y), s0);
      let goal = -1;
      if (sx === gx && sy === gy) goal = s0;
      while (goal < 0 && heap.size) {
        const st = heap.pop(), d = st & 3, cell = st >> 2, ix = cell % nx, iy = (cell - ix) / nx;
        if (heap.top > g[st] + Math.abs(xs[ix] - X) + Math.abs(ys[iy] - Y) + 1e-6) continue;       // an outdated entry
        if (ix === gx && iy === gy) { goal = st; break; }
        for (let nd = 0; nd < 4; nd++) {
          if (nd === ((d + 2) & 3)) continue;        // no U-turns
          const jx = ix + DX[nd], jy = iy + DY[nd];
          if (jx < 0 || jy < 0 || jx >= nx || jy >= ny) continue;
          const blocked = nd === 0 ? hBlock[iy * nx + ix] : nd === 2 ? hBlock[iy * nx + jx] : nd === 1 ? vBlock[iy * nx + ix] : vBlock[jy * nx + ix];
          const atGoal = jx === gx && jy === gy;
          if (atGoal && nd === 2) continue;          // the input is entered heading east
          const len = Math.abs(xs[jx] - xs[ix]) + Math.abs(ys[jy] - ys[iy]);
          let c = g[st] + len + (nd !== d ? BEND : 0) + (atGoal && nd !== 0 ? BEND : 0);
          // through a block (or closer to one than CLEAR) only when there is no way around: blocks that touch or
          // overlap leave none, and a wire squeezed between two of them is better than one that gives up
          if (blocked) c += THROUGH + len * 8;
          // among equally good routes, turn close to the destination: wires into one column then meet in one channel
          if (nd & 1) c += len * 0.05 * Math.min(1, Math.abs(xs[ix] - X) / 4000);
          const ns = (jy * nx + jx) * 4 + nd;
          if (stamp[ns] === gen && g[ns] <= c) continue;
          stamp[ns] = gen; g[ns] = c; from[ns] = st;
          heap.push(c + Math.abs(xs[jx] - X) + Math.abs(ys[jy] - Y), ns);
        }
      }
      let pts;
      const e = j.e, end = [e.t.x, j.ty];
      if (goal >= 0) {
        pts = [end];
        for (let st = goal; st >= 0; st = from[st]) { const cell = st >> 2, ix = cell % nx; pts.push([xs[ix], ys[(cell - ix) / nx]]); }
        pts.push([e.s.x, e.s.y]); pts.reverse();
      } else if (e.t.x - e.s.x >= 2 * CLEAR) {       // boxed in (blocks overlapping or touching): a plain step
        const mx = (e.s.x + e.t.x) / 2;
        pts = [[e.s.x, e.s.y], [mx, e.s.y], [mx, j.ty], end];
      } else {
        const my = (e.s.y + j.ty) / 2 + (Math.abs(e.s.y - j.ty) < 40 ? 60 : 0);
        pts = [[e.s.x, e.s.y], [e.s.x + CLEAR, e.s.y], [e.s.x + CLEAR, my], [e.t.x - CLEAR, my], [e.t.x - CLEAR, j.ty], end];
      }
      paths.push({ id: e.id, net: e.net || e.id, pts: simplify(pts.map((p) => [p[0], p[1]])) });
    }
    nudge(paths, nodes, 0);       // vertical runs: spread sideways
    nudge(paths, nodes, 1);       // horizontal runs between two corners: spread up and down
    nudge(paths, nodes, 0);       // ... which changes how far the vertical runs reach: once more
    // where a level run crosses another output's vertical run it hops over it: a crossing never reads as a junction
    for (const p of paths) p.pts = simplify(p.pts);
    const vert = [];
    for (const p of paths) for (let i = 1; i < p.pts.length; i++) { const a = p.pts[i - 1], b = p.pts[i]; if (a[0] === b[0]) vert.push({ x: a[0], lo: Math.min(a[1], b[1]), hi: Math.max(a[1], b[1]), net: p.net }); }
    for (const p of paths) {
      const hops = {};
      for (let i = 1; i < p.pts.length; i++) {
        const a = p.pts[i - 1], b = p.pts[i];
        if (a[1] !== b[1]) continue;
        const lo = Math.min(a[0], b[0]) + RADIUS + HOP + 2, hi = Math.max(a[0], b[0]) - RADIUS - HOP - 2, y = a[1];
        const xs = [...new Set(vert.filter((v) => v.net !== p.net && v.x > lo && v.x < hi && v.lo < y - 1 && v.hi > y + 1).map((v) => v.x))].sort((m, n) => (b[0] > a[0] ? m - n : n - m));
        const keep = []; for (const x of xs) if (!keep.length || Math.abs(x - keep[keep.length - 1]) >= 2 * HOP + 2) keep.push(x);
        if (keep.length) hops[i] = keep;
      }
      result.set(p.id, render(p.pts, hops));
    }
    return result;
  }

  // Runs that share a channel are given lanes: centred in the free space between the blocks on either side, LANE apart,
  // ordered so that the wires entering and leaving the channel cross as few of the others as possible.
  // ci = the coordinate a run can move along (0: x, for vertical runs; 1: y, for horizontal ones)
  function nudge(paths, nodes, ci) {
    const ri = 1 - ci;
    const rc = nodes.map((n) => (ci === 0 ? { c0: n.x, c1: n.x + n.w, r0: n.y, r1: n.y + n.h } : { c0: n.y, c1: n.y + n.h, r0: n.x, r1: n.x + n.w }));
    const segs = [];
    for (const p of paths) {
      const pts = p.pts;
      for (let i = 1; i < pts.length - 2; i++) {            // never the first or last run: those are fixed to a port
        const a = pts[i], b = pts[i + 1];
        if (a[ci] !== b[ci] || a[ri] === b[ri]) continue;
        const c = a[ci], lo = Math.min(a[ri], b[ri]), hi = Math.max(a[ri], b[ri]);
        let cl = -Infinity, cr = Infinity;                   // the free corridor around this run
        for (const q of rc) {
          if (q.r1 + PAD <= lo || q.r0 - PAD >= hi) continue;
          if (q.c1 <= c && q.c1 > cl) cl = q.c1;
          if (q.c0 >= c && q.c0 < cr) cr = q.c0;
        }
        cl = cl === -Infinity ? c - 2 * LANE : Math.min(c, cl + PAD);
        cr = cr === Infinity ? c + 2 * LANE : Math.max(c, cr - PAD);
        segs.push({ pts, i, c, lo, hi, cl, cr, s: a[ri], e: b[ri], sSide: Math.sign(pts[i - 1][ci] - c), eSide: Math.sign(pts[i + 2][ci] - c), key: p.net + "@" + c, net: p.net });
      }
    }
    const par = segs.map((_, i) => i);
    const find = (i) => { while (par[i] !== i) { par[i] = par[par[i]]; i = par[i]; } return i; };
    for (let a = 0; a < segs.length; a++) for (let b = a + 1; b < segs.length; b++) {
      const A = segs[a], B = segs[b];
      if (A.lo < B.hi + PAD && B.lo < A.hi + PAD && Math.max(A.cl, B.cl) <= Math.min(A.cr, B.cr)) par[find(a)] = find(b);
    }
    const clusters = new Map();
    segs.forEach((s, i) => { const r = find(i); if (!clusters.has(r)) clusters.set(r, []); clusters.get(r).push(s); });
    const inside = (v, s) => v > s.lo && v < s.hi;
    const crossings = (A, B) => {                            // if A's lane is on the low side of B's
      let n = 0;
      for (const a of A) for (const b of B) {
        if (inside(a.s, b) && a.sSide > 0) n++;
        if (inside(a.e, b) && a.eSide > 0) n++;
        if (inside(b.s, a) && b.sSide < 0) n++;
        if (inside(b.e, a) && b.eSide < 0) n++;
      }
      return n;
    };
    for (const cl of clusters.values()) {
      const groups = new Map();                              // runs of one output on one line stay one trunk
      for (const s of cl) { if (!groups.has(s.key)) groups.set(s.key, []); groups.get(s.key).push(s); }
      const members = [...groups.values()];
      members.sort((A, B) => (crossings(A, B) - crossings(B, A)) || (A[0].c - B[0].c) || (A[0].net < B[0].net ? -1 : 1));
      const L = Math.max(...cl.map((s) => s.cl)), R = Math.min(...cl.map((s) => s.cr));
      const n = members.length, mid = L <= R ? (L + R) / 2 : cl.reduce((a, s) => a + s.c, 0) / cl.length;
      const step = n > 1 ? Math.max(4, Math.min(LANE, (R - L) / (n - 1))) : 0;
      members.forEach((m, k) => { const v = Math.round((mid + (k - (n - 1) / 2) * step) * 2) / 2; for (const s of m) { s.pts[s.i][ci] = v; s.pts[s.i + 1][ci] = v; } });
    }
  }

  function render(pts, hops) {            // an SVG path with rounded corners, and where its label goes
    const f = (v) => Math.round(v * 10) / 10;
    let d = `M${f(pts[0][0])},${f(pts[0][1])}`, best = -1, lx = pts[0][0], ly = pts[0][1];
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i], len = Math.abs(b[0] - a[0]) + Math.abs(b[1] - a[1]);
      if (len > best) { best = len; lx = (a[0] + b[0]) / 2; ly = (a[1] + b[1]) / 2; }
      for (const x of (hops && hops[i]) || []) { const s = Math.sign(b[0] - a[0]); d += ` L${f(x - s * HOP)},${f(a[1])} A${HOP},${HOP} 0 0 ${s > 0 ? 1 : 0} ${f(x + s * HOP)},${f(a[1])}`; }
      if (i === pts.length - 1) { d += ` L${f(b[0])},${f(b[1])}`; break; }
      const c = pts[i + 1], len2 = Math.abs(c[0] - b[0]) + Math.abs(c[1] - b[1]);
      const r = Math.min(RADIUS, len / 2, len2 / 2);
      const ux = Math.sign(b[0] - a[0]), uy = Math.sign(b[1] - a[1]), vx = Math.sign(c[0] - b[0]), vy = Math.sign(c[1] - b[1]);
      d += ` L${f(b[0] - ux * r)},${f(b[1] - uy * r)} Q${f(b[0])},${f(b[1])} ${f(b[0] + vx * r)},${f(b[1] + vy * r)}`;
    }
    return { d, lx, ly, pts };
  }

  // nodes: [{id, x, y, w, h, kind, host, label, ports: {"s:<output id>": y inside the block, "t:<input id>": y}}]
  // edges: [{s, sp, t, tp}] (block ids and port ids).  Returns {id: {x, y}} for every node.
  // A node may carry `group` (the box it sits in): the blocks of one box are kept together.
  function organize(nodes, edges) {
    const VGAP = 30, CGAP = 90, COMPGAP = 70, PARKGAP = 110, BOXTOP = 46, BOXPAD = 30;
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const es = edges.filter((e) => byId.has(e.s) && byId.has(e.t) && e.s !== e.t);
    const portY = (n, kind, id) => { const v = n.ports && n.ports[kind + ":" + id]; return v == null ? n.h / 2 : v; };
    const out = {}, inn = {};
    for (const n of nodes) { out[n.id] = []; inn[n.id] = []; }
    for (const e of es) { out[e.s].push(e); inn[e.t].push(e); }
    const wired = nodes.filter((n) => out[n.id].length || inn[n.id].length);
    const byPlace = (a, b) => a.y - b.y || a.x - b.x || (a.id < b.id ? -1 : 1);

    // 1. loops (a satellite that sends a mic and plays the result): the wire that closes one is drawn backwards
    const seen = {}, back = new Set();
    const dfs = (id) => { seen[id] = 1; for (const e of out[id]) { if (seen[e.t] === 1) back.add(e); else if (!seen[e.t]) dfs(e.t); } seen[id] = 2; };
    for (const n of wired.slice().sort((a, b) => (inn[a.id].length ? 1 : 0) - (inn[b.id].length ? 1 : 0) || a.x - b.x || byPlace(a, b))) if (!seen[n.id]) dfs(n.id);
    const fwdOut = (id) => out[id].filter((e) => !back.has(e)), fwdIn = (id) => inn[id].filter((e) => !back.has(e));

    // 2. columns: a block sits one column right of the furthest thing that feeds it
    const rank = {}, indeg = {};
    for (const n of wired) { rank[n.id] = 0; indeg[n.id] = fwdIn(n.id).length; }
    const q = wired.filter((n) => !indeg[n.id]).map((n) => n.id);
    const topo = [];
    while (q.length) { const id = q.shift(); topo.push(id); for (const e of fwdOut(id)) { rank[e.t] = Math.max(rank[e.t], rank[id] + 1); if (--indeg[e.t] === 0) q.push(e.t); } }
    // connected groups are laid out one under the other
    const comp = {}; let nComp = 0;
    for (const n of wired) {
      if (comp[n.id] != null) continue;
      const stack = [n.id]; comp[n.id] = nComp;
      while (stack.length) { const id = stack.pop(); for (const e of out[id].concat(inn[id])) for (const o of [e.s, e.t]) if (comp[o] == null) { comp[o] = nComp; stack.push(o); } }
      nComp++;
    }
    const compMax = Array(nComp).fill(0);
    for (const n of wired) compMax[comp[n.id]] = Math.max(compMax[comp[n.id]], rank[n.id]);
    // outputs (devices nothing is fed from) line up in the last column; a generator sits just before what it feeds
    for (const n of wired) if (n.kind === "device" && !fwdOut(n.id).length && fwdIn(n.id).length) rank[n.id] = compMax[comp[n.id]];
    for (const id of topo.slice().reverse()) { const n = byId.get(id); if (n.kind !== "device" && !fwdIn(id).length && fwdOut(id).length) rank[id] = Math.min(...fwdOut(id).map((e) => rank[e.t])) - 1; }

    // a box's blocks stay together: they are one unit, laid out on its own. Everything else: one unit per connected group
    const unitOf = (n) => (n.group != null ? "g:" + n.group : "c:" + comp[n.id]);
    const laid = nodes.filter((n) => n.group != null || rank[n.id] != null);
    for (const n of laid) if (rank[n.id] == null) {            // in a box but not wired: its box's first column
      const mates = laid.filter((m) => m.group === n.group && inn[m.id].length + out[m.id].length).map((m) => rank[m.id]);
      rank[n.id] = mates.length ? Math.min(...mates) : 0;
    }

    // 3. column positions: as wide as the widest block, with a channel that has room for the wires crossing it
    const nRank = Math.max(0, ...laid.map((n) => rank[n.id])) + 1;
    const colW = Array(nRank).fill(0), nets = Array.from({ length: nRank }, () => new Set());
    for (const n of laid) colW[rank[n.id]] = Math.max(colW[rank[n.id]], n.w);
    for (const e of es) { const a = Math.min(rank[e.s], rank[e.t]), b = Math.max(rank[e.s], rank[e.t]); for (let r = a; r < b; r++) nets[r].add(e.s + "|" + e.sp); }
    const colX = [0];
    for (let r = 1; r < nRank; r++) colX[r] = colX[r - 1] + colW[r - 1] + CGAP + LANE * Math.min(10, nets[r - 1].size);

    // 4. inside each unit: order the blocks of a column by where their wires come from, and line the ports up
    const pos = {};
    const units = new Map();
    for (const n of laid) { const k = unitOf(n); if (!units.has(k)) units.set(k, []); units.get(k).push(n); }
    const groups = [...units.values()].sort((A, B) => Math.min(...A.map((n) => n.y)) - Math.min(...B.map((n) => n.y)) || Math.min(...A.map((n) => n.x)) - Math.min(...B.map((n) => n.x)));
    const sky = Array(nRank).fill(0);          // how far down each column is taken: units that use different columns sit side by side
    for (const grp of groups) {
      const boxed = grp[0].group != null;
      const layers = new Map();
      for (const n of grp.slice().sort(byPlace)) { if (!layers.has(rank[n.id])) layers.set(rank[n.id], []); layers.get(rank[n.id]).push(n); }
      const ranks = [...layers.keys()].sort((a, b) => a - b);
      const y = {};
      for (const r of ranks) { let yy = 0; for (const n of layers.get(r)) { y[n.id] = yy; yy += n.h + VGAP; } }
      // where a block would sit if its wires to one side (-1 left, +1 right, 0 both) ran straight
      const want = (n, side) => {
        let sum = 0, c = 0;
        for (const e of inn[n.id]) { const u = byId.get(e.s), d = Math.sign(rank[u.id] - rank[n.id]); if (y[u.id] != null && d && (!side || d === side)) { sum += y[u.id] + portY(u, "s", e.sp) - portY(n, "t", e.tp); c++; } }
        for (const e of out[n.id]) { const u = byId.get(e.t), d = Math.sign(rank[u.id] - rank[n.id]); if (y[u.id] != null && d && (!side || d === side)) { sum += y[u.id] + portY(u, "t", e.tp) - portY(n, "s", e.sp); c++; } }
        return c ? sum / c : null;
      };
      const place = (layer, side, reorder) => {
        const items = layer.map((n) => { const w = want(n, side); return { n, d: w == null ? y[n.id] : w }; });
        if (reorder) items.sort((a, b) => (a.d + a.n.h / 2) - (b.d + b.n.h / 2) || byPlace(a.n, b.n));
        const k = items.length, dn = Array(k), up = Array(k);      // pushed apart downwards, and upwards; the mean of both
        for (let i = 0; i < k; i++) dn[i] = i ? Math.max(items[i].d, dn[i - 1] + items[i - 1].n.h + VGAP) : items[0].d;
        for (let i = k - 1; i >= 0; i--) up[i] = i < k - 1 ? Math.min(items[i].d, up[i + 1] - items[i].n.h - VGAP) : items[k - 1].d;
        items.forEach((it, i) => { y[it.n.id] = (dn[i] + up[i]) / 2; });
        if (reorder) layer.splice(0, k, ...items.map((it) => it.n));
      };
      for (let it = 0; it < 6; it++) {
        for (const r of ranks) place(layers.get(r), -1, true);
        for (const r of ranks.slice().reverse()) place(layers.get(r), 1, true);
      }
      for (let it = 0; it < 4; it++) { for (const r of ranks) place(layers.get(r), 0, false); for (const r of ranks.slice().reverse()) place(layers.get(r), 0, false); }
      const top = Math.min(...grp.map((n) => y[n.id])), bottom = Math.max(...grp.map((n) => y[n.id] + n.h));
      const r0 = ranks[0], r1 = ranks[ranks.length - 1];
      let y0 = 0; for (let r = r0; r <= r1; r++) y0 = Math.max(y0, sky[r]);
      if (boxed) y0 += BOXTOP;                                // room for the box's title
      for (const n of grp) {
        const r = rank[n.id], rightAlign = r === 0 && !fwdIn(n.id).length;      // sources: their outputs in one line
        pos[n.id] = { x: Math.round(colX[r] + (rightAlign ? colW[r] - n.w : 0)), y: Math.round((y[n.id] - top + y0) * 100) / 100 };
      }
      for (let r = r0; r <= r1; r++) sky[r] = y0 + bottom - top + COMPGAP + (boxed ? BOXPAD : 0);
    }
    const yOff = Math.max(0, ...sky);

    // 5. what is not wired: parked in rows underneath, processing blocks first, then devices by machine
    const parked = nodes.filter((n) => !pos[n.id]).sort((a, b) => (a.kind === "device" ? 1 : 0) - (b.kind === "device" ? 1 : 0) ||
      (a.host || "").localeCompare(b.host || "") || (a.label || "").localeCompare(b.label || "") || (a.id < b.id ? -1 : 1));
    const width = Math.max(1100, nRank ? colX[nRank - 1] + colW[nRank - 1] : 0);
    let px = 0, py = laid.length ? yOff - COMPGAP + PARKGAP : 0, rowH = 0;
    for (const n of parked) {
      if (px && px + n.w > width) { px = 0; py += rowH + VGAP; rowH = 0; }
      pos[n.id] = { x: Math.round(px), y: Math.round(py) };
      px += n.w + 40; rowH = Math.max(rowH, n.h);
    }
    return pos;
  }

  const api = { route, organize, CLEAR, LANE, BOX: { top: 40, side: 22, bottom: 22 } };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.OwlWires = api;
})(typeof window !== "undefined" ? window : globalThis);
