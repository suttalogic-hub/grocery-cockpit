#!/usr/bin/env node
import fs from "node:fs/promises";
import fsSync from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import {
  BIGBASKET_CATEGORY_URLS,
  providerExtractor,
  providerHome,
  providerMatchStatusMode,
  usesCategoryScan,
  usesExtendedPriceParsing,
} from "./browser_provider_adapters.mjs";

const root = path.dirname(fileURLToPath(import.meta.url));
const dataDir = path.join(root, "data");
const defaultPlanPath = path.join(dataDir, "latest_scan_plan.json");
const profileRoot = path.join(dataDir, "browser-profiles");
const screenshotDir = path.join(dataDir, "probe-screenshots");
const chromePath = resolveBrowserExecutablePath();
const bigBasketCategoryUrls = BIGBASKET_CATEGORY_URLS;

function resolveBrowserExecutablePath() {
  if (process.env.GROCERY_CHROME_PATH) {
    return process.env.GROCERY_CHROME_PATH;
  }
  const candidates = [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  ];
  for (const candidate of candidates) {
    if (fsSync.existsSync(candidate)) return candidate;
  }
  return undefined;
}

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("--")) {
      args._.push(token);
      continue;
    }
    const key = token.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      args[key] = true;
    } else {
      args[key] = next;
      i += 1;
    }
  }
  return args;
}

async function readJson(filePath) {
  return JSON.parse(await fs.readFile(filePath, "utf8"));
}

async function writeJson(filePath, payload) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const tempPath = path.join(
    path.dirname(filePath),
    `.${path.basename(filePath)}.${process.pid}.${Date.now()}.tmp`,
  );
  await fs.writeFile(tempPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  await fs.rename(tempPath, filePath);
}

function resolvePlanPath(args = {}) {
  return args.plan ? path.resolve(String(args.plan)) : defaultPlanPath;
}

async function readPlan(args = {}) {
  const planPath = resolvePlanPath(args);
  try {
    return await readJson(planPath);
  } catch (error) {
    if (error.code === "ENOENT") {
      throw new Error("No scan plan yet. Tap Scan in the dashboard first.");
    }
    throw error;
  }
}

function groupByProvider(targets) {
  const groups = new Map();
  for (const target of targets) {
    if (!groups.has(target.provider_id)) {
      groups.set(target.provider_id, {
        provider_id: target.provider_id,
        provider_name: target.provider_name,
        provider_status: target.provider_status,
        scan_mode: target.scan_mode,
        targets: [],
      });
    }
    groups.get(target.provider_id).targets.push(target);
  }
  return [...groups.values()];
}

function targetsForProvider(plan, providerId) {
  const targets = providerId
    ? plan.targets.filter(target => target.provider_id === providerId)
    : plan.targets;
  if (!targets.length) {
    throw new Error(`No targets found${providerId ? ` for ${providerId}` : ""}.`);
  }
  return targets;
}

async function showPlan(args = {}) {
  const plan = await readPlan(args);
  console.log(`Scan plan #${plan.run_id || "unassigned"}`);
  console.log(`Requested: ${plan.requested_at}`);
  console.log(`Items: ${plan.summary.items}`);
  console.log(`Providers: ${plan.summary.providers}`);
  console.log(`Targets: ${plan.summary.targets}`);
  console.log(`Ready targets: ${plan.summary.ready_targets || 0}`);
  console.log(`Setup targets: ${plan.summary.setup_targets || 0}`);
  console.log(`Manual targets: ${plan.summary.manual_targets || 0}`);
  console.log("");
  for (const group of groupByProvider(plan.targets)) {
    console.log(`${group.provider_name}: ${group.targets.length} targets (${group.scan_mode}, ${group.provider_status})`);
    for (const target of group.targets.slice(0, 3)) {
      console.log(`  - ${target.display_name}`);
      console.log(`    ${target.search_url}`);
    }
    if (group.targets.length > 3) {
      console.log(`  ... ${group.targets.length - 3} more`);
    }
  }
}

async function writeProviderQueue(providerId, args = {}) {
  const plan = await readPlan(args);
  const targets = targetsForProvider(plan, providerId);
  const output = {
    created_at: new Date().toISOString(),
    source_plan: plan.run_id || null,
    provider_id: providerId || "all",
    target_count: targets.length,
    targets,
  };
  const outPath = path.join(dataDir, providerId ? `${providerId}_browser_queue.json` : "browser_queue.json");
  await writeJson(outPath, output);
  console.log(outPath);
}

function safeProviderId(providerId) {
  return String(providerId || "shared").replace(/[^a-z0-9_-]/gi, "_").toLowerCase();
}

function providerProfileDir(providerId) {
  return path.join(profileRoot, safeProviderId(providerId));
}

async function launchContext(options = {}) {
  const userDataDir = providerProfileDir(options.providerId);
  await fs.mkdir(userDataDir, { recursive: true });
  const launchOptions = {
    headless: Boolean(options.headless),
    viewport: { width: 1280, height: 900 },
    locale: "en-IN",
    timezoneId: "Asia/Kolkata",
    args: [
      "--disable-blink-features=AutomationControlled",
      "--no-first-run",
      "--disable-notifications",
    ],
  };
  if (chromePath) {
    launchOptions.executablePath = chromePath;
  }
  return chromium.launchPersistentContext(userDataDir, launchOptions);
}

async function setupProvider(providerId, args) {
  const minutes = Number(args.minutes || 20);
  const context = await launchContext({ headless: false, providerId });
  const page = await context.newPage();
  await page.goto(providerHome(providerId), { waitUntil: "domcontentloaded", timeout: 60000 });
  console.log("");
  console.log("A dedicated Grocery Cockpit browser is open.");
  console.log("Use it to log in and set delivery location for this provider.");
  console.log(`Provider: ${providerId || "all"}`);
  console.log(`Profile: ${providerProfileDir(providerId)}`);
  console.log(`This setup window will stay open for ${minutes} minutes unless you close it first.`);
  await page.waitForTimeout(minutes * 60 * 1000).catch(() => {});
  await context.close().catch(() => {});
}

function priceCandidatesFromText(text, providerId = "") {
  const candidates = [];
  const seen = new Set();
  const patterns = [
    /₹\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/gi,
    /([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*₹/gi,
    /(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/gi,
    /([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:₹|rupees?)/gi,
  ];
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) {
      const value = Number(String(match[1]).replace(/,/g, ""));
      if (!Number.isFinite(value) || value <= 0 || value > 100000) continue;
      const key = value.toFixed(2);
      if (seen.has(key)) continue;
      seen.add(key);
      candidates.push(value);
    }
  }
  if (providerId && usesExtendedPriceParsing(providerId)) {
    const swiggyPatterns = [
      /\b[0-9]{1,2}%\s*OFF\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b(?!\s*(?:MINS?|MINUTES?)\b)(?:\s+[0-9][0-9,]*(?:\.[0-9]{1,2})?\b)?/gi,
    ];
    for (const pattern of swiggyPatterns) {
      for (const match of text.matchAll(pattern)) {
        const value = Number(String(match[1]).replace(/,/g, ""));
        if (!Number.isFinite(value) || value <= 0 || value > 100000) continue;
        const key = value.toFixed(2);
        if (seen.has(key)) continue;
        seen.add(key);
        candidates.push(value);
      }
    }
    const unitPattern = /\b(?:ml|g|kg|ltr|litre|liter|combo)\b(?:\s*x\s*\d+)?(?:\s+[A-Za-z][A-Za-z+*,.'-]*){0,8}\s+([1-9][0-9]{1,4})(?=\s|$)/gi;
    for (const match of text.matchAll(unitPattern)) {
      const value = Number(String(match[1]).replace(/,/g, ""));
      if (!Number.isFinite(value) || value <= 0 || value > 100000) continue;
      const key = value.toFixed(2);
      if (seen.has(key)) continue;
      seen.add(key);
      candidates.push(value);
    }
  }
  return candidates.sort((a, b) => a - b).slice(0, 12);
}

function uniquePriceCandidatesFromLinks(links) {
  const seen = new Set();
  const prices = [];
  for (const link of links) {
    for (const price of priceCandidatesFromText(link.text || "")) {
      const key = price.toFixed(2);
      if (seen.has(key)) continue;
      seen.add(key);
      prices.push(price);
    }
  }
  return prices.sort((a, b) => a - b).slice(0, 12);
}

function normalizeBigBasketCategoryUrl(url) {
  try {
    const parsed = new URL(url);
    if (!/bigbasket\.com$/i.test(parsed.hostname)) return "";
    if (!/^\/(?:cl|pc)\//i.test(parsed.pathname)) return "";
    const pathname = parsed.pathname.endsWith("/") ? parsed.pathname : `${parsed.pathname}/`;
    return `https://www.bigbasket.com${pathname}?nc=nb`;
  } catch {
    return "";
  }
}

function bigBasketCategorySlug(url) {
  const normalizedUrl = normalizeBigBasketCategoryUrl(url);
  if (!normalizedUrl) return "";
  try {
    const parts = new URL(normalizedUrl).pathname.split("/").filter(Boolean);
    return parts[1] || "";
  } catch {
    return "";
  }
}

function belongsToBigBasketRoot(parentUrl, childUrl) {
  const parentSlug = bigBasketCategorySlug(parentUrl);
  const childSlug = bigBasketCategorySlug(childUrl);
  return Boolean(parentSlug && childSlug && parentSlug === childSlug);
}

function balancedBigBasketDiscovery(discovered, maxCount) {
  const rootOrder = bigBasketCategoryUrls.map(category => bigBasketCategorySlug(category.url)).filter(Boolean);
  const buckets = new Map();
  for (const category of discovered) {
    const root = bigBasketCategorySlug(category.url);
    if (!root) continue;
    if (!buckets.has(root)) buckets.set(root, []);
    buckets.get(root).push(category);
  }
  const ordered = [];
  while (ordered.length < maxCount) {
    let added = false;
    for (const root of rootOrder) {
      const bucket = buckets.get(root) || [];
      const next = bucket.shift();
      if (!next) continue;
      ordered.push(next);
      added = true;
      if (ordered.length >= maxCount) break;
    }
    if (!added) break;
  }
  return ordered;
}

function bigBasketLocationReady(text) {
  const lower = (text || "").toLowerCase();
  return lower.includes("showing products & prices for") || lower.includes("home:");
}

function cleanProductLines(lines) {
  return cleanProductEntries(lines.map(line => ({ text: line, href: "" }))).map(entry => entry.text);
}

function cleanProductEntries(entries) {
  const seen = new Set();
  const clean = [];
  for (const entry of entries) {
    const text = String(entry?.text || "").replace(/\s+/g, " ").trim();
    if (!text || text.length < 24 || text.length > 520) continue;
    if (!/(\u20b9\s*\d|Rs\.?\s*\d)/i.test(text)) continue;
    if (!/(Add|Notify Me|OFF|Ratings|Sponsored)/i.test(text)) continue;
    const href = String(entry?.href || "").trim();
    const key = `${href}|${text.slice(0, 240)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    clean.push({ text, href });
  }
  return clean;
}

function moneyValue(raw) {
  const value = Number(String(raw || "").replace(/,/g, ""));
  return Number.isFinite(value) ? value : 0;
}

function moneyText(value) {
  if (!Number.isFinite(value)) return "";
  if (Number.isInteger(value)) return String(value);
  return value.toFixed(2).replace(/0+$/g, "").replace(/\.$/, "");
}

function cleanDmartProductName(value) {
  let text = String(value || "").replace(/\s+/g, " ").trim();
  const markerPatterns = [
    /\bSort\s+by:\s*Relevance\b/i,
    /\b\d+\s+of\s+\d+\s+Items?\b/i,
    /\bSearch\s+Results?\b/i,
  ];
  for (const pattern of markerPatterns) {
    const match = pattern.exec(text);
    if (match) text = text.slice(match.index + match[0].length).trim();
  }
  text = text
    .replace(/\b(?:MRP|DMart)\b.*$/i, "")
    .replace(/\b(?:ADD TO CART|Notify Me)\b.*$/i, "")
    .replace(/\s+/g, " ")
    .trim();
  if (text.length > 220) text = text.slice(-220).trim();
  return text;
}

function extractDmartProductCardsFromText(text, href) {
  const cards = [];
  const moneySymbol = "(?:\\u20b9|\\u00e2\\u201a\\u00b9|Rs\\.?)";
  const amount = "([0-9][0-9,]*(?:\\.[0-9]{1,2})?)";
  const pricePattern = new RegExp(`MRP\\s*${moneySymbol}\\s*${amount}\\s+DMart\\s*${moneySymbol}\\s*${amount}`, "i");
  const segments = String(text || "")
    .replace(/\r/g, "\n")
    .split(/ADD TO CART/i);

  for (const segment of segments) {
    const collapsed = segment.replace(/\s+/g, " ").trim();
    const match = pricePattern.exec(collapsed);
    if (!match) continue;
    const mrp = moneyValue(match[1]);
    const sale = moneyValue(match[2]);
    if (!sale || !mrp || sale > mrp * 1.25) continue;
    const name = cleanDmartProductName(collapsed.slice(0, match.index));
    if (name.length < 8) continue;
    cards.push({
      text: `${name} DMart \u20b9 ${moneyText(sale)} MRP \u20b9 ${moneyText(mrp)} ADD TO CART`,
      href,
      price: sale,
      mrp,
    });
  }
  return cards;
}

function moneyValueFromText(text) {
  const match = /(?:\u20b9|\u00e2\u201a\u00b9|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/i.exec(String(text || ""));
  return match ? moneyValue(match[1]) : 0;
}

function cleanAmazonProductTitle(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .replace(/\bSponsored\b/gi, "")
    .trim()
    .slice(0, 260);
}

function amazonSalePriceFromTexts(priceTexts) {
  for (const priceText of priceTexts || []) {
    const text = String(priceText || "");
    if (/[\/](?:\s*)?(?:100\s*)?(?:ml|g|kg|l|litre|liter|unit|pc|piece)\b/i.test(text)) continue;
    const value = moneyValueFromText(text);
    if (value) return value;
  }
  return 0;
}

function amazonMrpFromText(text, priceTexts, salePrice) {
  const body = String(text || "");
  const direct = /\b(?:M\.?\s*R\.?\s*P\.?|List Price)\s*:?\s*(?:\u20b9|\u00e2\u201a\u00b9|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/i.exec(body);
  if (direct) return moneyValue(direct[1]);
  for (const priceText of priceTexts || []) {
    if (/[\/](?:\s*)?(?:100\s*)?(?:ml|g|kg|l|litre|liter|unit|pc|piece)\b/i.test(String(priceText || ""))) continue;
    const value = moneyValueFromText(priceText);
    if (value && (!salePrice || value >= salePrice)) return value;
  }
  return 0;
}

function normalizeAmazonProductHref(href) {
  const raw = String(href || "").trim();
  if (!raw) return "";
  try {
    const parsed = new URL(raw);
    const redirectUrl = parsed.searchParams.get("url") || parsed.searchParams.get("U");
    if (redirectUrl) {
      const decoded = new URL(redirectUrl, parsed.origin).toString();
      return normalizeAmazonProductHref(decoded);
    }
    return parsed.toString();
  } catch {
    return raw;
  }
}

function buildAmazonCards(cardEntries) {
  const cards = [];
  for (const entry of cardEntries || []) {
    const title = cleanAmazonProductTitle(entry.title);
    const price = amazonSalePriceFromTexts(entry.price_texts) || moneyValueFromText(entry.price_text);
    if (!title || !price) continue;
    const rawMrp = amazonMrpFromText(entry.text, entry.mrp_texts, price) || null;
    const mrp = rawMrp && rawMrp >= price && rawMrp <= price * 5 ? rawMrp : null;
    cards.push({
      text: `${title} \u20b9 ${moneyText(price)}${mrp ? ` MRP \u20b9 ${moneyText(mrp)}` : ""} ADD`,
      href: normalizeAmazonProductHref(entry.href || ""),
      price,
      mrp,
    });
  }
  const seen = new Set();
  const clean = [];
  for (const card of cards) {
    const key = `${card.href}|${card.text.slice(0, 220)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    clean.push(card);
  }
  return clean;
}

function dedupeDmartProductCards(cards) {
  const seen = new Set();
  const clean = [];
  for (const card of cards) {
    const text = String(card.text || "").replace(/\s+/g, " ").trim();
    const href = String(card.href || "").trim();
    if (!text) continue;
    const key = `${href}|${text.slice(0, 220)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    clean.push({ ...card, text, href });
  }
  return clean;
}

function classifyDmartPage(snapshot, cards) {
  if (cards.length) return "price_candidates";
  const lower = `${snapshot.title || ""}\n${snapshot.text || ""}`.toLowerCase();
  if (lower.includes("no search results found") || lower.includes("no products found")) {
    return "no_price_found";
  }
  if (lower.includes("access denied") || lower.includes("captcha") || lower.includes("robot")) {
    return "blocked";
  }
  if (lower.includes("too many requests") || lower.includes("http error 429")) {
    return "rate_limited";
  }
  if (
    !lower.includes("earliest home delivery") &&
    (lower.includes("select location") || lower.includes("enter pincode") || lower.includes("delivery location"))
  ) {
    return "needs_setup";
  }
  return "no_price_found";
}

async function waitForDmartSearchSignal(page) {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    await page.waitForTimeout(1500);
    const hasSignal = await page.evaluate(() => {
      const text = document.body?.innerText || "";
      return (
        /MRP\s*(?:\u20b9|\u00e2\u201a\u00b9|Rs\.?).*DMart\s*(?:\u20b9|\u00e2\u201a\u00b9|Rs\.?)/is.test(text) ||
        /No Search results found|No products found/i.test(text)
      );
    });
    if (hasSignal) return;
  }
}

function classifyAmazonPage(snapshot, cards) {
  if (cards.length) return "price_candidates";
  const lower = `${snapshot.title || ""}\n${snapshot.text || ""}`.toLowerCase();
  if (lower.includes("no results for") || lower.includes("did not match any products")) return "no_price_found";
  if (lower.includes("captcha") || lower.includes("unusual traffic") || lower.includes("robot check")) return "blocked";
  if (lower.includes("enter the characters you see below")) return "blocked";
  if (lower.includes("update location") && !lower.includes("delivering to")) return "needs_setup";
  return "no_price_found";
}

async function waitForAmazonSearchSignal(page) {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    await page.waitForTimeout(1500);
    const hasSignal = await page.evaluate(() => {
      const text = document.body?.innerText || "";
      return (
        document.querySelectorAll('[data-component-type="s-search-result"]').length > 0 ||
        /No results for|did not match any products|Enter the characters you see below/i.test(text)
      );
    });
    if (hasSignal) return;
  }
}

function classifyPage({ title, text, priceCandidates }) {
  const lower = `${title}\n${text}`.toLowerCase();
  const setupSignals = [
    "enter your location",
    "select location",
    "login",
    "log in",
    "sign in",
    "verify",
    "otp",
    "delivery location",
    "where should we deliver",
  ];
  const blockedSignals = [
    "access denied",
    "captcha",
    "unusual traffic",
    "robot",
    "forbidden",
  ];
  const rateLimitSignals = [
    "http error 429",
    "too many requests",
    "rate limit",
  ];
  const productResultSignals = [
    "showing results for",
    "sort by",
    "all filters",
    "quick add",
    "add to cart",
  ];
  if (rateLimitSignals.some(signal => lower.includes(signal))) return "rate_limited";
  if (blockedSignals.some(signal => lower.includes(signal))) return "blocked";
  if (priceCandidates.length && productResultSignals.some(signal => lower.includes(signal))) {
    return "price_candidates";
  }
  if (setupSignals.some(signal => lower.includes(signal))) return "needs_setup";
  if (priceCandidates.length) return "price_candidates";
  return "no_price_found";
}

function classifyLaunchError(error) {
  const message = error?.message || String(error);
  const lower = message.toLowerCase();
  if (
    lower.includes("target page, context or browser has been closed") ||
    lower.includes("processsingleton") ||
    lower.includes("profile")
  ) {
    return {
      status: "profile_in_use",
      message: "The dedicated setup browser is still open or the provider profile is locked. Close it, then run Probe again.",
      raw: message,
    };
  }
  return {
    status: "error",
    message: message,
    raw: message,
  };
}

async function writeProbeOutput(providerId, plan, targets, results) {
  const output = {
    created_at: new Date().toISOString(),
    source_plan: plan.run_id || null,
    provider_id: providerId,
    target_count: targets.length,
    results,
  };
  const outPath = path.join(dataDir, `${providerId}_probe_results.json`);
  await writeJson(outPath, output);
  console.log("");
  console.log(outPath);
}

async function extractAmazonPageProbe(page, target) {
  await waitForAmazonSearchSignal(page);
  const snapshot = await page.evaluate(() => {
    const body = document.body;
    const text = body?.innerText || "";
    const cards = [...document.querySelectorAll('[data-component-type="s-search-result"]')]
      .slice(0, 48)
      .map(card => {
        const brandNode = card.querySelector("h2.s-line-clamp-1 span") || card.querySelector("h2.s-line-clamp-1");
        const nameNode = card.querySelector("a.s-line-clamp-3 h2") || card.querySelector("a.s-line-clamp-3") || card.querySelector("h2 span") || card.querySelector("h2");
        const anchors = [...card.querySelectorAll("a[href]")];
        const linkNode = anchors.find(anchor => {
          const href = anchor.href || "";
          return /\/(?:dp|gp\/product)\//i.test(href) || /%2f(?:dp|gp%2fproduct)%2f/i.test(href) || /\/sspa\/click/i.test(href);
        }) || card.querySelector("h2 a[href]") || card.querySelector("a.a-link-normal.s-no-outline[href]");
        const brand = (brandNode?.textContent || "").replace(/\s+/g, " ").trim();
        const name = (nameNode?.textContent || "").replace(/\s+/g, " ").trim();
        const title = brand && name && !name.toLowerCase().startsWith(brand.toLowerCase()) ? `${brand} ${name}` : (name || brand);
        const priceTexts = [...card.querySelectorAll(".a-price .a-offscreen")]
          .map(node => (node.textContent || "").trim())
          .filter(Boolean);
        const mrpTexts = [...card.querySelectorAll(".a-text-price .a-offscreen, .a-price.a-text-price .a-offscreen")]
          .map(node => (node.textContent || "").trim())
          .filter(Boolean);
        return {
          title,
          href: linkNode?.href || location.href,
          price_text: priceTexts[0] || "",
          price_texts: priceTexts,
          mrp_texts: mrpTexts,
          text: (card.innerText || card.textContent || "").replace(/\s+/g, " ").trim().slice(0, 1200),
        };
      });
    const images = [...document.querySelectorAll("img")]
      .slice(0, 20)
      .map(img => ({
        alt: (img.alt || "").trim().slice(0, 120),
        src: img.currentSrc || img.src,
      }));
    return {
      title: document.title,
      url: location.href,
      text: text.slice(0, 25000),
      cards,
      images,
    };
  });
  const cards = buildAmazonCards(snapshot.cards);
  const seenPrices = new Set();
  const priceCandidates = [];
  for (const card of cards) {
    for (const value of [card.price, card.mrp]) {
      if (!Number.isFinite(value) || value <= 0) continue;
      const key = value.toFixed(2);
      if (seenPrices.has(key)) continue;
      seenPrices.add(key);
      priceCandidates.push(value);
    }
  }
  const textNeedle = (target.search_display_name || target.name || "").toLowerCase().split(/\s+/).filter(Boolean).slice(0, 3);
  const lowerText = snapshot.text.toLowerCase();
  const productTextHits = textNeedle.filter(part => lowerText.includes(part));
  return {
    item_id: target.item_id,
    display_name: target.display_name,
    provider_id: target.provider_id,
    provider_name: target.provider_name,
    search_kind: target.search_kind || "exact",
    search_display_name: target.search_display_name || target.display_name,
    search_url: target.search_url,
    final_url: snapshot.url,
    title: snapshot.title,
    status: classifyAmazonPage(snapshot, cards),
    product_text_hits: productTextHits.length,
    price_candidates: priceCandidates.slice(0, 12),
    text_excerpt: cards.slice(0, 5).map(card => card.text).join(" | ") || snapshot.text.replace(/\s+/g, " ").slice(0, 700),
    links: cards.map(card => ({ text: card.text, href: card.href || snapshot.url })),
    images: snapshot.images,
    observed_at: new Date().toISOString(),
  };
}

async function extractPageProbe(page, target) {
  const snapshot = await page.evaluate(() => {
    const body = document.body;
    const text = body?.innerText || "";
    const links = [...document.querySelectorAll("a[href]")]
      .slice(0, 50)
      .map(link => ({
        text: (link.textContent || "").trim().slice(0, 120),
        href: link.href,
      }));
    const images = [...document.querySelectorAll("img")]
      .slice(0, 20)
      .map(img => ({
        alt: (img.alt || "").trim().slice(0, 120),
        src: img.currentSrc || img.src,
      }));
    return {
      title: document.title,
      url: location.href,
      text: text.slice(0, 25000),
      links,
      images,
    };
  });
  const priceCandidates = priceCandidatesFromText(snapshot.text, target.provider_id);
  const status = classifyPage({
    title: snapshot.title,
    text: snapshot.text.slice(0, 5000),
    priceCandidates,
  });
  const textNeedle = (target.search_display_name || target.name || "").toLowerCase().split(/\s+/).filter(Boolean).slice(0, 3);
  const lowerText = snapshot.text.toLowerCase();
  const productTextHits = textNeedle.filter(part => lowerText.includes(part));
  return {
    item_id: target.item_id,
    display_name: target.display_name,
    provider_id: target.provider_id,
    provider_name: target.provider_name,
    search_kind: target.search_kind || "exact",
    search_display_name: target.search_display_name || target.display_name,
    search_url: target.search_url,
    final_url: snapshot.url,
    title: snapshot.title,
    status,
    product_text_hits: productTextHits.length,
    price_candidates: priceCandidates,
    text_excerpt: snapshot.text.replace(/\s+/g, " ").slice(0, 700),
    links: snapshot.links,
    images: snapshot.images,
    observed_at: new Date().toISOString(),
  };
}

async function extractDmartPageProbe(page, target) {
  await waitForDmartSearchSignal(page);
  const snapshot = await page.evaluate(() => {
    const body = document.body;
    const text = body?.innerText || "";
    const cardEntries = [...document.querySelectorAll('a, button, [class*="product" i], [class*="card" i], li, div')]
      .map(el => {
        const anchor = el.closest?.("a[href]") || el.querySelector?.("a[href]");
        return {
          text: (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim(),
          href: anchor?.href || location.href,
        };
      })
      .filter(entry => /MRP\s*(?:\u20b9|\u00e2\u201a\u00b9|Rs\.?)/i.test(entry.text) && /DMart\s*(?:\u20b9|\u00e2\u201a\u00b9|Rs\.?)/i.test(entry.text))
      .slice(0, 220);
    const images = [...document.querySelectorAll("img")]
      .slice(0, 20)
      .map(img => ({
        alt: (img.alt || "").trim().slice(0, 120),
        src: img.currentSrc || img.src,
      }));
    return {
      title: document.title,
      url: location.href,
      text: text.slice(0, 25000),
      card_entries: cardEntries,
      images,
    };
  });
  const cards = dedupeDmartProductCards([
    ...snapshot.card_entries.flatMap(entry => extractDmartProductCardsFromText(entry.text, entry.href || snapshot.url)),
    ...extractDmartProductCardsFromText(snapshot.text, snapshot.url),
  ]);
  const seenPrices = new Set();
  const priceCandidates = [];
  for (const card of cards) {
    for (const value of [card.price, card.mrp]) {
      if (!Number.isFinite(value) || value <= 0) continue;
      const key = value.toFixed(2);
      if (seenPrices.has(key)) continue;
      seenPrices.add(key);
      priceCandidates.push(value);
    }
  }
  const textNeedle = (target.search_display_name || target.name || "").toLowerCase().split(/\s+/).filter(Boolean).slice(0, 3);
  const lowerText = snapshot.text.toLowerCase();
  const productTextHits = textNeedle.filter(part => lowerText.includes(part));
  return {
    item_id: target.item_id,
    display_name: target.display_name,
    provider_id: target.provider_id,
    provider_name: target.provider_name,
    search_kind: target.search_kind || "exact",
    search_display_name: target.search_display_name || target.display_name,
    search_url: target.search_url,
    final_url: snapshot.url,
    title: snapshot.title,
    status: classifyDmartPage(snapshot, cards),
    product_text_hits: productTextHits.length,
    price_candidates: priceCandidates.slice(0, 12),
    text_excerpt: cards.slice(0, 4).map(card => card.text).join(" | ") || snapshot.text.replace(/\s+/g, " ").slice(0, 700),
    links: cards.map(card => ({ text: card.text, href: card.href || snapshot.url })),
    images: snapshot.images,
    observed_at: new Date().toISOString(),
  };
}

async function collectBigBasketCategory(page, category) {
  await page.goto(category.url, { waitUntil: "domcontentloaded", timeout: 60000 });
  for (let attempt = 0; attempt < 7; attempt += 1) {
    await page.waitForTimeout(3000);
    const hasSignal = await page.evaluate(() => {
      const text = document.body?.innerText || "";
      return text.includes("₹") || text.includes("Rs") || /select location/i.test(text);
    });
    if (hasSignal) break;
  }
  return page.evaluate((cat) => {
    const text = document.body?.innerText || "";
    const productLines = [...document.querySelectorAll('a, button, [class*="product" i], [class*="sku" i], [class*="card" i], li')]
      .map(el => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
      .filter(Boolean)
      .slice(0, 300);
    return {
      label: cat.label,
      url: location.href,
      title: document.title,
      has_price: text.includes("₹") || text.includes("Rs"),
      location_ready: text.toLowerCase().includes("showing products & prices for") || text.toLowerCase().includes("home:"),
      text_excerpt: text.replace(/\s+/g, " ").slice(0, 900),
      product_lines: productLines,
    };
  }, category);
}

async function waitForBigBasketCategorySignal(page) {
  for (let attempt = 0; attempt < 7; attempt += 1) {
    await page.waitForTimeout(3000);
    const hasSignal = await page.evaluate(() => {
      const text = document.body?.innerText || "";
      return /\u20b9|Rs\.?|select location/i.test(text);
    });
    if (hasSignal) return;
  }
}

async function snapshotBigBasketCategory(page, category) {
  return page.evaluate((cat) => {
    const text = document.body?.innerText || "";
    const categoryLinks = [...document.querySelectorAll('a[href]')]
      .map(anchor => ({
        text: (anchor.innerText || anchor.textContent || "").replace(/\s+/g, " ").trim(),
        href: anchor.href,
      }))
      .filter(entry => entry.text && /^https?:\/\/(?:www\.)?bigbasket\.com\/(?:cl|pc)\//i.test(entry.href))
      .slice(0, 180);
    const productEntries = [...document.querySelectorAll('a, button, [class*="product" i], [class*="sku" i], [class*="card" i], li')]
      .map(el => {
        const anchor = el.closest?.("a[href]") || el.querySelector?.("a[href]");
        return {
          text: (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim(),
          href: anchor?.href || location.href,
        };
      })
      .filter(entry => entry.text)
      .slice(0, 700);
    return {
      label: cat.label,
      url: location.href,
      title: document.title,
      has_price: /\u20b9|Rs\.?/i.test(text),
      location_ready: text.toLowerCase().includes("showing products & prices for") || text.toLowerCase().includes("home:"),
      text_excerpt: text.replace(/\s+/g, " ").slice(0, 900),
      product_entries: productEntries,
      category_links: categoryLinks,
      scroll_y: Math.round(window.scrollY || 0),
      scroll_height: Math.round(document.documentElement?.scrollHeight || document.body?.scrollHeight || 0),
    };
  }, category);
}

async function advanceBigBasketCategory(page) {
  const clickedShowMore = await page.evaluate(() => {
    const buttons = [...document.querySelectorAll("button")];
    const button = buttons.find(candidate => /show\s*more/i.test(candidate.innerText || candidate.textContent || ""));
    if (!button) return false;
    button.click();
    return true;
  });
  if (clickedShowMore) {
    await page.waitForTimeout(900);
  }
  await page.evaluate(() => {
    const amount = Math.max(650, Math.floor((window.innerHeight || 900) * 0.85));
    window.scrollBy(0, amount);
  });
  await page.waitForTimeout(1600);
  return clickedShowMore;
}

async function collectBigBasketCategoryDeep(page, category, maxScrollRounds) {
  await page.goto(category.url, { waitUntil: "domcontentloaded", timeout: 60000 });
  await waitForBigBasketCategorySignal(page);
  const productMap = new Map();
  const categoryLinkMap = new Map();
  const snapshots = [];
  let lastScrollHeight = 0;
  let lastCount = 0;
  let steadyRounds = 0;

  const capture = async () => {
    const snapshot = await snapshotBigBasketCategory(page, category);
    const cleanEntries = cleanProductEntries(snapshot.product_entries);
    for (const entry of cleanEntries) {
      productMap.set(`${entry.href}|${entry.text.slice(0, 240)}`, entry);
    }
    for (const link of snapshot.category_links || []) {
      const normalizedUrl = normalizeBigBasketCategoryUrl(link.href);
      const label = String(link.text || "").replace(/\s+/g, " ").trim();
      if (!normalizedUrl || !label || label.length > 120) continue;
      categoryLinkMap.set(normalizedUrl, { label, url: normalizedUrl });
    }
    snapshots.push({
      y: snapshot.scroll_y,
      height: snapshot.scroll_height,
      visible_product_count: cleanEntries.length,
      total_product_count: productMap.size,
    });
    return snapshot;
  };

  let latest = await capture();
  for (let round = 0; round < maxScrollRounds; round += 1) {
    const clickedShowMore = await advanceBigBasketCategory(page);
    latest = await capture();
    const grew = latest.scroll_height > lastScrollHeight + 30 || productMap.size > lastCount;
    if (!grew && !clickedShowMore) {
      steadyRounds += 1;
    } else {
      steadyRounds = 0;
    }
    lastScrollHeight = latest.scroll_height;
    lastCount = productMap.size;
    if (steadyRounds >= 2) break;
  }

  return {
    ...latest,
    product_entries: [...productMap.values()],
    product_lines: [...productMap.values()].map(entry => entry.text),
    category_links: [...categoryLinkMap.values()],
    scroll_rounds: snapshots.length - 1,
    snapshots,
  };
}

async function probeBigBasketCategories(args) {
  const plan = await readPlan(args);
  const limit = Math.max(1, Number(args.limit || 20));
  const offset = Math.max(0, Number(args.offset || 0));
  const headless = Boolean(args.headless);
  const maxScrollRounds = Math.max(0, Math.min(Number(args["bb-scrolls"] ?? args.scrolls ?? 4), 16));
  const maxSharedLinks = Math.max(50, Math.min(Number(args["bb-max-links"] ?? args["max-links"] ?? 700), 2000));
  const maxCategoryPages = Math.max(
    bigBasketCategoryUrls.length,
    Math.min(Number(args["bb-max-categories"] ?? args.categories ?? 16), 48),
  );
  const providerTargets = targetsForProvider(plan, "bigbasket");
  const targets = providerTargets.slice(offset, offset + limit);
  if (!targets.length) {
    throw new Error(`No targets found for bigbasket at offset ${offset}. Provider has ${providerTargets.length} targets.`);
  }

  let context;
  try {
    context = await launchContext({ headless, providerId: "bigbasket" });
  } catch (error) {
    const classified = classifyLaunchError(error);
    const results = targets.map(target => ({
      item_id: target.item_id,
      display_name: target.display_name,
      provider_id: target.provider_id,
      provider_name: target.provider_name,
      search_kind: target.search_kind || "exact",
      search_display_name: target.search_display_name || target.display_name,
      search_url: target.search_url,
      status: classified.status,
      error: classified.message,
      launch_error: classified.raw,
      observed_at: new Date().toISOString(),
    }));
    await writeProbeOutput("bigbasket", plan, targets, results);
    console.log(classified.message);
    return;
  }

  const page = await context.newPage();
  const categories = [];
  const links = [];
  const categoryQueue = bigBasketCategoryUrls.map(category => ({ ...category, source: "seed" }));
  const seenCategoryUrls = new Set(categoryQueue.map(category => normalizeBigBasketCategoryUrl(category.url) || category.url));
  const pendingDiscoveredCategories = [];
  let discoveryQueued = false;
  try {
    for (let categoryIndex = 0; categoryIndex < categoryQueue.length && categories.length < maxCategoryPages; categoryIndex += 1) {
      const category = categoryQueue[categoryIndex];
      console.log(`BigBasket category: ${category.label}`);
      try {
        const result = await collectBigBasketCategoryDeep(page, category, maxScrollRounds);
        result.product_entries = cleanProductEntries(result.product_entries);
        const discoveredCategoryLinks = [];
        for (const link of result.category_links || []) {
          const normalizedUrl = normalizeBigBasketCategoryUrl(link.url);
          if (!normalizedUrl || seenCategoryUrls.has(normalizedUrl)) continue;
          if (!belongsToBigBasketRoot(category.url, normalizedUrl)) continue;
          seenCategoryUrls.add(normalizedUrl);
          const nextCategory = {
            label: `${result.label} > ${link.label}`.slice(0, 120),
            url: normalizedUrl,
            source: "discovered",
          };
          discoveredCategoryLinks.push(nextCategory);
          if ((category.source || "seed") === "seed") {
            pendingDiscoveredCategories.push(nextCategory);
          }
        }
        categories.push({
          label: result.label,
          url: result.url,
          source: category.source || "seed",
          title: result.title,
          has_price: result.has_price,
          location_ready: result.location_ready,
          product_line_count: result.product_entries.length,
          scroll_rounds: result.scroll_rounds,
          discovered_category_count: discoveredCategoryLinks.length,
          text_excerpt: result.text_excerpt,
        });
        for (const entry of result.product_entries) {
          links.push({
            text: /Add|Notify Me/i.test(entry.text) ? entry.text : `${entry.text} Add`,
            href: entry.href || result.url,
          });
        }
        console.log(`  ${result.location_ready ? "location-ready" : "needs-location"}; product lines: ${result.product_entries.length}; scrolls: ${result.scroll_rounds}; discovered: ${discoveredCategoryLinks.length}`);
      } catch (error) {
        categories.push({
          label: category.label,
          url: category.url,
          source: category.source || "seed",
          title: "",
          has_price: false,
          location_ready: false,
          product_line_count: 0,
          error: error.message || String(error),
        });
        console.log(`  error: ${error.message || String(error)}`);
      }
      if (!discoveryQueued && categoryIndex + 1 >= bigBasketCategoryUrls.length) {
        const remainingSlots = Math.max(0, maxCategoryPages - categoryQueue.length);
        categoryQueue.push(...balancedBigBasketDiscovery(pendingDiscoveredCategories, remainingSlots));
        discoveryQueued = true;
      }
    }
  } finally {
    await context.close().catch(() => {});
  }

  const locationReady = categories.some(category => category.location_ready);
  const cleanLinks = [];
  const seen = new Set();
  for (const link of links) {
    const key = `${link.href}|${link.text.slice(0, 240)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    cleanLinks.push(link);
  }
  const sharedLinks = cleanLinks.slice(0, maxSharedLinks);
  const priceCandidates = uniquePriceCandidatesFromLinks(sharedLinks);
  const observedAt = new Date().toISOString();
  const status = locationReady ? (sharedLinks.length ? "price_candidates" : "no_price_found") : "needs_setup";
  const setupMessage = "BigBasket category pages loaded, but this browser profile still needs delivery location setup.";
  const results = targets.map(target => ({
    item_id: target.item_id,
    display_name: target.display_name,
    provider_id: target.provider_id,
    provider_name: target.provider_name,
    search_kind: target.search_kind || "exact",
    search_display_name: target.search_display_name || target.display_name,
    search_url: target.search_url,
    final_url: "",
    title: "BigBasket category scan",
    status,
    price_candidates: status === "price_candidates" ? priceCandidates : [],
    text_excerpt: status === "needs_setup" ? setupMessage : "",
    links: [],
    observed_at: observedAt,
  }));
  const output = {
    created_at: observedAt,
    source_plan: plan.run_id || null,
    provider_id: "bigbasket",
    source: "bigbasket-category-scan",
    match_status_mode: "best_probe_match",
    target_count: targets.length,
    provider_target_count: providerTargets.length,
    offset,
    limit,
    range_start: offset + 1,
    range_end: offset + targets.length,
    location_ready: locationReady,
    candidate_link_count: cleanLinks.length,
    stored_candidate_link_count: sharedLinks.length,
    max_scroll_rounds: maxScrollRounds,
    max_category_pages: maxCategoryPages,
    scanned_category_count: categories.length,
    discovered_category_count: Math.max(0, seenCategoryUrls.size - bigBasketCategoryUrls.length),
    categories,
    shared_links: status === "price_candidates" ? sharedLinks : [],
    results,
  };
  const outPath = path.join(dataDir, "bigbasket_probe_results.json");
  await writeJson(outPath, output);
  console.log("");
  console.log(outPath);
}

async function shouldUseBigBasketCategoryScan(args) {
  const plan = await readPlan(args);
  const providerTargets = targetsForProvider(plan, "bigbasket");
  const limit = Math.max(1, Number(args.limit || 3));
  const source = String(plan.source || "");
  if (["item", "quick_need", "basket"].includes(source)) {
    return false;
  }
  if (providerTargets.length <= Math.max(3, limit)) {
    return false;
  }
  return true;
}

async function probeProvider(providerId, args) {
  if (usesCategoryScan(providerId) && await shouldUseBigBasketCategoryScan(args)) {
    await probeBigBasketCategories(args);
    return;
  }
  const plan = await readPlan(args);
  const limit = Math.max(1, Number(args.limit || 3));
  const offset = Math.max(0, Number(args.offset || 0));
  const headless = Boolean(args.headless);
  const slowMs = Math.max(0, Number(args.slow || 0));
  const providerTargets = targetsForProvider(plan, providerId);
  const targets = providerTargets.slice(offset, offset + limit);
  if (!targets.length) {
    throw new Error(`No targets found for ${providerId} at offset ${offset}. Provider has ${providerTargets.length} targets.`);
  }
  let context;
  try {
    context = await launchContext({ headless, providerId });
  } catch (error) {
    const classified = classifyLaunchError(error);
    const results = targets.map(target => ({
      item_id: target.item_id,
      display_name: target.display_name,
      provider_id: target.provider_id,
      provider_name: target.provider_name,
      search_kind: target.search_kind || "exact",
      search_display_name: target.search_display_name || target.display_name,
      search_url: target.search_url,
      status: classified.status,
      error: classified.message,
      launch_error: classified.raw,
      observed_at: new Date().toISOString(),
    }));
    await writeProbeOutput(providerId, plan, targets, results);
    console.log(classified.message);
    return;
  }
  const page = await context.newPage();
  await fs.mkdir(screenshotDir, { recursive: true });
  const results = [];
  try {
    for (let index = 0; index < targets.length; index += 1) {
      const target = targets[index];
      console.log(`[${index + 1}/${targets.length}] ${target.provider_name}: ${target.display_name}`);
      let navigationError = "";
      try {
        try {
          await page.goto(target.search_url, { waitUntil: "domcontentloaded", timeout: 60000 });
        } catch (error) {
          navigationError = error.message || String(error);
        }
        await page.waitForTimeout(2500 + slowMs);
        let result;
        const extractor = providerExtractor(providerId);
        if (extractor === "amazon") {
          result = await extractAmazonPageProbe(page, target);
        } else if (extractor === "dmart") {
          result = await extractDmartPageProbe(page, target);
        } else {
          result = await extractPageProbe(page, target);
        }
        if (navigationError) {
          result.navigation_error = navigationError;
          if (result.status === "no_price_found") {
            result.status = "navigation_error";
          }
        }
        const screenshotName = `${target.provider_id}-${target.item_id}-${Date.now()}.png`;
        const screenshotPath = path.join(screenshotDir, screenshotName);
        await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
        result.screenshot_path = screenshotPath;
        results.push(result);
        console.log(`  ${result.status}; prices: ${result.price_candidates.join(", ") || "none"}`);
      } catch (error) {
        results.push({
          item_id: target.item_id,
          display_name: target.display_name,
          provider_id: target.provider_id,
          provider_name: target.provider_name,
          search_kind: target.search_kind || "exact",
          search_display_name: target.search_display_name || target.display_name,
          search_url: target.search_url,
          status: "error",
          error: error.message || String(error),
          observed_at: new Date().toISOString(),
        });
        console.log(`  error: ${error.message || String(error)}`);
      }
    }
  } finally {
    await context.close().catch(() => {});
  }
  const output = {
    created_at: new Date().toISOString(),
    source_plan: plan.run_id || null,
    provider_id: providerId,
    target_count: targets.length,
    provider_target_count: providerTargets.length,
    offset,
    limit,
    range_start: offset + 1,
    range_end: offset + targets.length,
    match_status_mode: providerMatchStatusMode(providerId),
    results,
  };
  const outPath = path.join(dataDir, `${providerId}_probe_results.json`);
  await writeJson(outPath, output);
  console.log("");
  console.log(outPath);
}

async function showResults(providerId) {
  const providers = providerId
    ? [providerId]
    : (await fs.readdir(dataDir))
      .map(name => name.match(/^(.+)_probe_results\.json$/)?.[1])
      .filter(Boolean);
  if (!providers.length) {
    throw new Error("No probe results yet. Run: node browser_scan_worker.mjs probe zepto --limit 1");
  }
  for (const provider of providers) {
    const filePath = path.join(dataDir, `${provider}_probe_results.json`);
    let payload;
    try {
      payload = await readJson(filePath);
    } catch (error) {
      if (error.code === "ENOENT") continue;
      throw error;
    }
    const counts = new Map();
    for (const result of payload.results || []) {
      counts.set(result.status, (counts.get(result.status) || 0) + 1);
    }
    console.log(`${provider}: ${payload.results?.length || 0} probed`);
    for (const [status, count] of counts.entries()) {
      console.log(`  ${status}: ${count}`);
    }
    const first = payload.results?.[0];
    if (first) {
      console.log(`  sample: ${first.display_name}`);
      console.log(`  excerpt: ${(first.text_excerpt || first.error || "").slice(0, 220)}`);
      if (first.screenshot_path) console.log(`  screenshot: ${first.screenshot_path}`);
    }
  }
}

function usage() {
  console.log("Usage:");
  console.log("  node browser_scan_worker.mjs plan");
  console.log("  node browser_scan_worker.mjs queue [provider_id]");
  console.log("  node browser_scan_worker.mjs setup <provider_id> [--minutes 20]");
  console.log("  node browser_scan_worker.mjs probe <provider_id> [--limit 3] [--offset 0] [--headless] [--plan data/latest_scan_plan.json]");
  console.log("  node browser_scan_worker.mjs results [provider_id]");
}

const parsed = parseArgs(process.argv.slice(2));
const command = parsed._[0] || "plan";

try {
  if (command === "plan") {
    await showPlan(parsed);
  } else if (command === "queue") {
    await writeProviderQueue(parsed._[1] || "", parsed);
  } else if (command === "setup") {
    await setupProvider(parsed._[1] || "zepto", parsed);
  } else if (command === "probe") {
    const providerId = parsed._[1];
    if (!providerId) throw new Error("Pick a provider id, for example: zepto or blinkit.");
    await probeProvider(providerId, parsed);
  } else if (command === "results") {
    await showResults(parsed._[1] || "");
  } else {
    usage();
    process.exitCode = 1;
  }
} catch (error) {
  console.error(error.message || String(error));
  process.exitCode = 1;
}
