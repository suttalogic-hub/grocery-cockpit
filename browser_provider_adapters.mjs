/**
 * Browser-probe policies for each supported provider.
 *
 * Extraction implementations stay in browser_scan_worker.mjs. This registry
 * describes which implementation and browser behavior each provider needs.
 */
export const PROVIDER_ADAPTERS = Object.freeze({
  zepto: Object.freeze({
    homeUrl: "https://www.zepto.com/",
    extractor: "default",
  }),
  blinkit: Object.freeze({
    homeUrl: "https://blinkit.com/",
    extractor: "default",
  }),
  swiggy_instamart: Object.freeze({
    homeUrl: "https://www.swiggy.com/instamart",
    extractor: "default",
    extendedPriceParsing: true,
  }),
  amazon_fresh: Object.freeze({
    homeUrl: "https://www.amazon.in/tez/browse/now",
    extractor: "amazon",
    matchStatusMode: "best_probe_match",
  }),
  jiomart: Object.freeze({
    homeUrl: "https://www.jiomart.com/",
    extractor: "default",
  }),
  dmart: Object.freeze({
    homeUrl: "https://www.dmart.in/",
    extractor: "dmart",
    matchStatusMode: "best_probe_match",
  }),
  bigbasket: Object.freeze({
    homeUrl: "https://www.bigbasket.com/",
    extractor: "default",
    categoryScan: true,
  }),
});

export const BIGBASKET_CATEGORY_URLS = Object.freeze([
  { label: "Fruits & Vegetables", url: "https://www.bigbasket.com/cl/fruits-vegetables/?nc=nb" },
  { label: "Bakery, Cakes & Dairy", url: "https://www.bigbasket.com/cl/bakery-cakes-dairy/?nc=nb" },
  { label: "Beverages", url: "https://www.bigbasket.com/cl/beverages/?nc=nb" },
  { label: "Foodgrains, Oil & Masala", url: "https://www.bigbasket.com/cl/foodgrains-oil-masala/?nc=nb" },
  { label: "Snacks & Branded Foods", url: "https://www.bigbasket.com/cl/snacks-branded-foods/?nc=nb" },
  { label: "Cleaning & Household", url: "https://www.bigbasket.com/cl/cleaning-household/?nc=nb" },
  { label: "Beauty & Hygiene", url: "https://www.bigbasket.com/cl/beauty-hygiene/?nc=nb" },
  { label: "Eggs, Meat & Fish", url: "https://www.bigbasket.com/cl/eggs-meat-fish/?nc=nb" },
]);

const GENERIC_PROVIDER_ADAPTER = Object.freeze({
  homeUrl: "about:blank",
  extractor: "default",
});

export function providerAdapter(providerId) {
  return PROVIDER_ADAPTERS[providerId] || GENERIC_PROVIDER_ADAPTER;
}

export function providerHome(providerId) {
  return providerAdapter(providerId).homeUrl;
}

export function providerExtractor(providerId) {
  return providerAdapter(providerId).extractor;
}

export function providerMatchStatusMode(providerId) {
  return providerAdapter(providerId).matchStatusMode;
}

export function usesCategoryScan(providerId) {
  return Boolean(providerAdapter(providerId).categoryScan);
}

export function usesExtendedPriceParsing(providerId) {
  return Boolean(providerAdapter(providerId).extendedPriceParsing);
}
