import assert from "node:assert/strict";

import {
  BIGBASKET_CATEGORY_URLS,
  PROVIDER_ADAPTERS,
  providerAdapter,
  providerExtractor,
  providerHome,
  providerMatchStatusMode,
  usesCategoryScan,
  usesExtendedPriceParsing,
} from "../browser_provider_adapters.mjs";

const expectedProviders = [
  "amazon_fresh",
  "bigbasket",
  "blinkit",
  "dmart",
  "jiomart",
  "swiggy_instamart",
  "zepto",
];

assert.deepEqual(Object.keys(PROVIDER_ADAPTERS).sort(), expectedProviders);
for (const providerId of expectedProviders) {
  assert.match(providerHome(providerId), /^https:\/\//);
  assert.ok(["default", "amazon", "dmart"].includes(providerExtractor(providerId)));
}

assert.equal(providerExtractor("amazon_fresh"), "amazon");
assert.equal(providerExtractor("dmart"), "dmart");
assert.equal(providerMatchStatusMode("amazon_fresh"), "best_probe_match");
assert.equal(providerMatchStatusMode("zepto"), undefined);
assert.equal(usesCategoryScan("bigbasket"), true);
assert.equal(usesExtendedPriceParsing("swiggy_instamart"), true);
assert.ok(BIGBASKET_CATEGORY_URLS.length >= 8);
assert.equal(providerAdapter("unknown").homeUrl, "about:blank");
assert.equal(providerExtractor("unknown"), "default");

console.log("Browser provider adapter contract passed.");
