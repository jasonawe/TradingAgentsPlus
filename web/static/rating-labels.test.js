"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const formatInvestmentRating = require("./rating-labels.js");

const RATINGS = [
  ["Strong Buy", "强烈买入（Strong Buy）"],
  ["Buy", "买入（Buy）"],
  ["Overweight", "超配（Overweight）"],
  ["Hold", "持有（Hold）"],
  ["Underweight", "减持（Underweight）"],
  ["Sell", "卖出（Sell）"],
  ["Strong Sell", "强烈卖出（Strong Sell）"],
];

test("formats every canonical rating for Chinese and English interfaces", () => {
  for (const [rating, chineseLabel] of RATINGS) {
    assert.equal(formatInvestmentRating(rating), chineseLabel);
    assert.equal(formatInvestmentRating(rating, "zh"), chineseLabel);
    assert.equal(formatInvestmentRating(rating, "en"), rating);
  }
});

test("normalizes representative case and separator variants", () => {
  const cases = [
    ["STRONG BUY", "强烈买入（Strong Buy）"],
    ["StrongBuy", "强烈买入（Strong Buy）"],
    ["  strong   buy  ", "强烈买入（Strong Buy）"],
    ["strong\tbuy", "强烈买入（Strong Buy）"],
    ["strong_buy", "强烈买入（Strong Buy）"],
    ["strong-buy", "强烈买入（Strong Buy）"],
    ["UNDER_WEIGHT", "减持（Underweight）"],
    ["strong---sell", "强烈卖出（Strong Sell）"],
  ];

  for (const [value, expected] of cases) {
    assert.equal(formatInvestmentRating(value), expected);
  }
  assert.equal(formatInvestmentRating("strong\t\t_sell", "en"), "Strong Sell");
});

test("returns trimmed unknown ratings without interpreting their contents", () => {
  assert.equal(formatInvestmentRating("  Accumulate  "), "Accumulate");
  assert.equal(formatInvestmentRating("  <strong>Custom</strong>  "), "<strong>Custom</strong>");
  assert.equal(formatInvestmentRating("constructor"), "constructor");
  assert.equal(formatInvestmentRating("constructor", "en"), "constructor");
});

test("returns null for nullish, empty, and whitespace-only inputs", () => {
  for (const value of [null, undefined, "", "   ", "\t\n "]) {
    assert.equal(formatInvestmentRating(value), null);
  }
});
