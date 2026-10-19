import json
from pathlib import Path
import shutil
import subprocess
import unittest


class VehicleMileageSyncTests(unittest.TestCase):
    def test_visible_polling_focus_dedup_and_failures_preserve_last_reference(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js required for mileage synchronization behavior tests")
        source = (Path(__file__).resolve().parents[1] / "WinPython_公務電腦使用包/static/task-entry-mileages.js").read_text(encoding="utf-8")
        script = r"""
const source = require('fs').readFileSync(0, 'utf8'), assert = require('assert');
const windowHandlers = {}, documentHandlers = {}, intervals = [], requests = [], updates = [];
let resolveRequest;
const document = {visibilityState: 'visible', addEventListener: (name, fn) => documentHandlers[name] = fn};
const window = {setTimeout: () => 1, clearTimeout: () => {}, setInterval: (fn, ms) => intervals.push({fn, ms}),
  addEventListener: (name, fn) => windowHandlers[name] = fn};
const fetch = (...args) => {requests.push(args); return new Promise(resolve => {resolveRequest = resolve;});};
require('vm').runInNewContext(source, {window, document, fetch, AbortController});
async function finish(response) {resolveRequest(response); await new Promise(resolve => setImmediate(resolve));}
(async () => {
  window.startVehicleMileageSync(data => updates.push(data));
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], '/api/vehicle-mileages');
  assert.equal(requests[0][1].cache, 'no-store');
  assert.equal(requests[0][1].method, undefined);
  windowHandlers.focus();
  assert.equal(requests.length, 1);
  await finish({ok: true, json: async () => ({mileages: {car: '4886'}, times: {}, history: {}})});
  document.visibilityState = 'hidden';
  await documentHandlers.visibilitychange();
  assert.equal(requests.length, 1);
  document.visibilityState = 'visible';
  documentHandlers.visibilitychange();
  await finish({ok: false});
  assert.equal(updates.length, 1);
  intervals[0].fn();
  await finish({ok: true, json: async () => ({mileages: {car: '4919'}, times: {}, history: {}})});
  assert.equal(updates[1].mileages.car, '4919');
  assert.equal(intervals[0].ms, 30000);
  process.stdout.write('ok');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run([node, "-e", script], input=source, text=True, encoding="utf-8",
                                capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
