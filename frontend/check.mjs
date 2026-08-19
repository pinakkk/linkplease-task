import { chromium } from "playwright";

const SCRATCH = "/private/tmp/claude-501/-Users-pinak-projects-linkplease/67ce2b9f-cf6f-46ae-a88f-e20a40660a21/scratchpad";
const browser = await chromium.launch();
const errors = [];

async function newPage(opts = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, ...opts });
  const page = await ctx.newPage();
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") errors.push(`[${m.type()}] ${m.text()}`);
  });
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  return { ctx, page };
}

// --- 1. Fresh visit, no stored theme, light system default
const { ctx, page } = await newPage({ colorScheme: "light" });
await page.goto("http://localhost:3111/", { waitUntil: "networkidle" });
console.log("initial data-theme (light system):", await page.getAttribute("html", "data-theme"));
console.log("body bg:", await page.evaluate(() => getComputedStyle(document.body).backgroundColor));
console.log("stored:", await page.evaluate(() => localStorage.getItem("linkplease-theme")));

// count-up settled?
const statText = await page.locator("text=DMs sent").locator("xpath=../..").innerText().catch(()=> "n/a");
console.log("stat card text:", JSON.stringify(statText));

await page.screenshot({ path: `${SCRATCH}/light.png`, fullPage: true });

// --- 2. Toggle to dark
await page.getByRole("button", { name: /Switch to dark theme/i }).click();
await page.waitForTimeout(900);
console.log("after toggle data-theme:", await page.getAttribute("html", "data-theme"));
console.log("after toggle body bg:", await page.evaluate(() => getComputedStyle(document.body).backgroundColor));
console.log("persisted:", await page.evaluate(() => localStorage.getItem("linkplease-theme")));
console.log("theme-reveal class cleaned up:", await page.evaluate(() => !document.documentElement.classList.contains("theme-reveal")));
console.log("theme-transition cleaned up:", await page.evaluate(() => !document.documentElement.classList.contains("theme-transition")));
await page.screenshot({ path: `${SCRATCH}/dark.png`, fullPage: true });

// --- 3. Reload: dark must persist with NO flash (attribute set before paint)
await page.reload({ waitUntil: "domcontentloaded" });
console.log("after reload data-theme:", await page.getAttribute("html", "data-theme"));

// --- 4. Dark system preference, no stored choice
await ctx.close();
const { ctx: ctx2, page: page2 } = await newPage({ colorScheme: "dark" });
await page2.goto("http://localhost:3111/", { waitUntil: "networkidle" });
console.log("system-dark initial data-theme:", await page2.getAttribute("html", "data-theme"));
await ctx2.close();

// --- 5. Reduced motion + touch: tilt & pulse must be off
const { ctx: ctx3, page: page3 } = await newPage({ reducedMotion: "reduce", hasTouch: true, isMobile: true, viewport: { width: 390, height: 844 } });
await page3.goto("http://localhost:3111/", { waitUntil: "networkidle" });
const transformed = await page3.evaluate(() => {
  const cards = [...document.querySelectorAll("*")].filter(e => getComputedStyle(e).transform.includes("matrix3d"));
  return cards.length;
});
console.log("mobile+reduced: elements with 3d transform:", transformed);
console.log("mobile: content visible (stat text):", await page3.locator("text=DMs sent").first().isVisible());
await page3.screenshot({ path: `${SCRATCH}/mobile.png`, fullPage: true });

// mobile nav
await page3.getByRole("button", { name: /Open menu/i }).click();
await page3.waitForTimeout(200);
console.log("mobile nav panel visible:", await page3.locator("#mobile-nav").isVisible());
await page3.screenshot({ path: `${SCRATCH}/mobile-nav.png` });
await ctx3.close();

// --- 6. Dashboard page + skip link
const { ctx: ctx4, page: page4 } = await newPage();
await page4.goto("http://localhost:3111/dashboard", { waitUntil: "networkidle" });
await page4.keyboard.press("Tab");
const focused = await page4.evaluate(() => document.activeElement?.textContent);
console.log("first tab stop:", JSON.stringify(focused));
await ctx4.close();

console.log("\n=== console errors/warnings ===");
console.log(errors.length ? errors.join("\n") : "(none)");
await browser.close();
