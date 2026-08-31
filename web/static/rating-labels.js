(function (root, factory) {
  "use strict";

  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.formatInvestmentRating = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const RATING_LABELS = {
    strongbuy: { en: "Strong Buy", zh: "强烈买入" },
    buy: { en: "Buy", zh: "买入" },
    overweight: { en: "Overweight", zh: "超配" },
    hold: { en: "Hold", zh: "持有" },
    underweight: { en: "Underweight", zh: "减持" },
    sell: { en: "Sell", zh: "卖出" },
    strongsell: { en: "Strong Sell", zh: "强烈卖出" },
  };

  return function formatInvestmentRating(value, uiLanguage = "zh") {
    if (value == null) return null;
    const raw = String(value).trim();
    if (!raw) return null;
    const key = raw.toLowerCase().replace(/[\s_-]+/g, "");
    const rating = Object.prototype.hasOwnProperty.call(RATING_LABELS, key) ? RATING_LABELS[key] : null;
    if (!rating) return raw;
    return uiLanguage === "zh" ? `${rating.zh}（${rating.en}）` : rating.en;
  };
});
