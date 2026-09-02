const test = require("node:test");
const assert = require("node:assert/strict");
const { QuoteRefreshController } = require("./quote-refresh.js");

test("backs off, resets after success, and ignores older responses", async () => {
  const delays = [];
  let current = 0;
  const pending = [];
  const controller = new QuoteRefreshController({
    fetcher: (_signal, sequence) => new Promise((resolve, reject) => pending.push({ resolve, reject, sequence })),
    onData: (value) => { current = value; },
    onError: () => {},
    schedule: (_fn, delay) => { delays.push(delay); return delays.length; },
    cancelSchedule: () => {},
    timeoutMs: 4000,
  });
  const first = controller.refresh();
  const second = controller.refresh();
  pending[1].resolve(2);
  await second;
  pending[0].resolve(1);
  await first;
  assert.equal(current, 2);
  assert.equal(delays.at(-1), 5000);

  controller.fetcher = async () => { throw new Error("down"); };
  await controller.refresh();
  await controller.refresh();
  assert.deepEqual(delays.filter((delay) => delay !== 4000).slice(-2), [10000, 20000]);
  controller.fetcher = async () => 3;
  await controller.refresh();
  assert.equal(delays.at(-1), 5000);
});
