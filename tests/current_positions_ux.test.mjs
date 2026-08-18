import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const read = (path) => fs.readFileSync(new URL(path, import.meta.url), "utf8");
const index = read("../src/trading_control_plane/web/index.html");
const positions = read("../src/trading_control_plane/web/positions.js");
const proposals = read("../src/trading_control_plane/web/proposals.js");
const router = read("../src/trading_control_plane/web/router.js");
const session = read("../src/trading_control_plane/web/session.js");

test("the sidebar keeps one proposal queue and inserts current positions between trades and reports", () => {
  assert.equal(index.includes('href="/proposals"'), false);
  assert.equal(index.match(/>审核队列</g)?.length, 1);
  const trades = index.indexOf('href="/campaigns"');
  const currentPositions = index.indexOf('href="/positions"');
  const reports = index.indexOf('href="/results"');
  assert.ok(trades >= 0 && trades < currentPositions && currentPositions < reports);
  assert.equal(index.includes('href="/system"'), true);
});

test("the review queue owns current and historical proposal views", () => {
  assert.equal(proposals.includes('href="/reviews?view=current"'), true);
  assert.equal(proposals.includes('href="/reviews?view=history"'), true);
  assert.equal(proposals.includes('href="/proposals"'), false);
  assert.equal(router.includes("await renderProposalList(null, '当前提案')"), true);
});

test("current positions are read-only and expose all requested filters", () => {
  for (const marker of [
    'id="position-venue"',
    'id="position-account"',
    'id="position-direction"',
    'id="position-pnl"',
    'data-position-row',
    'renderCurrentPositions',
  ]) assert.equal(positions.includes(marker), true, marker);
  assert.equal(positions.includes('data-close-position'), false);
  assert.equal(positions.includes('/close'), false);
  assert.equal(router.includes("path === '/positions'"), true);
  assert.equal(router.includes('await renderCurrentPositions()'), true);
  assert.equal(session.includes("if (path === '/positions') return 'venue.view'"), true);
});
