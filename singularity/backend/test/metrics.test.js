const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const { NodeTelemetryProbe } = require("../metricsBridge.js");

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hotaru-metrics-"));
  const log = path.join(directory, "events.jsonl");
  fs.writeFileSync(log, "");
  const probe = new NodeTelemetryProbe({ observatoryPath: log, pollIntervalMs: 0 });
  t.after(() => {
    probe.close();
    fs.rmSync(directory, { recursive: true });
  });
  return { log, probe };
}

test("retains records split across writes, including UTF-8 characters", (t) => {
  const { log, probe } = fixture(t);
  const record = Buffer.from(JSON.stringify({ event: "split", component: "test", detail: "привет" }) + "\n");
  const split = record.indexOf(Buffer.from("п")) + 1;
  fs.appendFileSync(log, record.subarray(0, split));
  probe._pollObservatoryLog();
  assert.equal(probe._debugBuffer.some((entry) => entry.event === "split"), false);
  fs.appendFileSync(log, record.subarray(split));
  probe._pollObservatoryLog();
  const entries = probe._debugBuffer.filter((entry) => entry.event === "split");
  assert.equal(entries.length, 1);
  assert.match(entries[0].detail, /привет/);
  probe._pollObservatoryLog();
  assert.equal(probe._debugBuffer.filter((entry) => entry.event === "split").length, 1);
});

test("retains a record crossing the read block boundary", (t) => {
  const { log, probe } = fixture(t);
  const first = JSON.stringify({ event: "first", component: "test", detail: "x".repeat(65000) });
  const second = JSON.stringify({ event: "second", component: "test", detail: "y".repeat(2000) });
  fs.appendFileSync(log, first + "\n" + second + "\n");
  probe._pollObservatoryLog();
  probe._pollObservatoryLog();
  assert.equal(probe._debugBuffer.filter((entry) => entry.event === "first").length, 1);
  assert.equal(probe._debugBuffer.filter((entry) => entry.event === "second").length, 1);
});

test("preserves a complete record at the initial and rotated tail boundary", (t) => {
  const { log, probe } = fixture(t);
  const first = '{"event":"boundary_first"}\n';
  const second = JSON.stringify({ event: "boundary_second", detail: "x".repeat(16384 - first.length - 40) }) + "\n";
  const tail = first + second + "\n".repeat(16384 - first.length - second.length);
  for (const rotate of [false, true]) {
    if (rotate) fs.renameSync(log, log + ".old");
    fs.writeFileSync(log, "{}\n" + tail);
    if (!rotate) probe._obsInode = null;
    const received = [];
    const record = probe._recordDebug.bind(probe);
    probe._recordDebug = (entry) => { received.push(entry.event); record(entry); };
    probe._pollObservatoryLog();
    assert.ok(received.includes("boundary_first"));
    probe._recordDebug = record;
  }
});

test("drops old partial data on rotation and truncation", (t) => {
  const { log, probe } = fixture(t);
  for (const rotate of [false, true]) {
    fs.appendFileSync(log, '{"event":"unfinished","detail":"' + "x".repeat(100));
    probe._pollObservatoryLog();
    if (rotate) fs.renameSync(log, log + ".old");
    const event = rotate ? "rotated" : "truncated";
    fs.writeFileSync(log, JSON.stringify({ event }) + "\n");
    probe._pollObservatoryLog();
    assert.equal(probe._debugBuffer.filter((entry) => entry.event === event).length, 1);
  }
});

test("skips oversized unfinished lines and recovers at the next record", (t) => {
  const { log, probe } = fixture(t);
  fs.appendFileSync(log, '{"event":"oversized","detail":"' + "x".repeat(4 * 1024 * 1024 + 1));
  while (probe._obsOffset < fs.statSync(log).size) probe._pollObservatoryLog();
  fs.appendFileSync(log, '"}\n{"event":"recovered"}\n');
  probe._pollObservatoryLog();
  assert.equal(probe._debugBuffer.some((entry) => entry.event === "oversized"), false);
  assert.equal(probe._debugBuffer.some((entry) => entry.event === "recovered"), true);
});
