"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const i18n = require("./i18n.js");

test("fixed product locale exposes complete Chinese operational labels", () => {
  assert.equal(i18n.locale, "zh-CN");
  for (const key of [
    "nav.watchlist",
    "nav.analysis",
    "nav.active",
    "nav.reports",
    "nav.settings",
    "run_status.timed_out",
    "provider_status.degraded",
    "freshness.stale",
    "cache_status.hit",
    "assets.crypto",
    "exchange.shh",
    "common.unavailable",
  ]) {
    assert.notEqual(i18n.t(key), key, key);
  }
});

test("unknown and empty values have safe deterministic fallbacks", () => {
  assert.equal(i18n.label("provider_status", "future_state"), "future_state");
  assert.equal(i18n.label("provider_status", "future_state", "原始状态"), "原始状态");
  assert.equal(i18n.displayValue(null), "暂无");
  assert.equal(i18n.displayValue("   "), "暂无");
  assert.equal(i18n.displayValue("OpenAI"), "OpenAI");
});

test("asset identity prefers Chinese then raw provider values", () => {
  assert.deepEqual(
    i18n.assetIdentity({
      asset_name_zh: "招商证券",
      asset_name: "China Merchants Securities Co., Ltd.",
      exchange_name_zh: "上海证券交易所",
      exchange: "SHH",
    }),
    { name: "招商证券", exchange: "上海证券交易所" },
  );
  assert.deepEqual(
    i18n.assetIdentity({ asset_name: "Example Inc.", exchange: "XYZ" }),
    { name: "Example Inc.", exchange: "XYZ" },
  );
  assert.deepEqual(i18n.assetIdentity({}), { name: "暂无", exchange: "暂无" });
});
