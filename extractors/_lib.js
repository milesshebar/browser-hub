// Shared helpers for extractors. An extractor exports: name, required (fields that must be
// non-null for status "ok"), url(id), and async lookup(page, id) -> {status, data?, reason?}.
export const clean = s => (s == null ? null : String(s).replace(/\s+/g, ' ').trim() || null);

// Signals that a page is a wall rather than content, checked before extracting so a wall comes back
// as {status: "blocked"} instead of nulls. Title, URL and DOM markers are trusted. Body text is
// only consulted when the page's content selector is missing: product copy says "press and hold
// the power button" or "security verification key", and that must not read as a wall.
const CAPTCHA = /just a moment|security verification|captcha|are you a (human|robot)|verify you are human|confirm you.re human|unusual (browser activity|traffic)|press (&|and) hold|access denied|request unsuccessful/i;
// Amazon's bot checkpoint (hit 2026-11-11 on the logged-in Business account after ~25 product
// loads in 30 minutes) keeps the normal title and site menu, so its URL is the reliable sign.
const WALL_URL = /\/checkpoint\/|\/errors\/validateCaptcha|\/cdn-cgi\/challenge/;
// Titles are short and specific: a wall's title starts with one of these. A product title can
// contain "security verification" (a YubiKey) without being one.
const TITLE_WALL = /^(just a moment|attention required|access denied|robot check|security check|are you a (human|robot))|captcha/i;
// Only whole-page challenge forms. Captcha and Turnstile iframes also sit inside normal pages
// (Micro Center embeds one), so those count only when the page's content is missing.
const WALL_DOM = 'form[action*="validateCaptcha"], #px-captcha, #challenge-form';
const WEAK_DOM = 'iframe[src*="challenges.cloudflare.com"], iframe[src*="captcha"]';
export function wallFrom({ url = '', title = '', body = '', marker = false, weakMarker = false, content = true }, { loginUrl, loginText } = {}) {
  if (loginUrl && loginUrl.test(url)) return { status: 'blocked', reason: 'login', at: url };
  if (WALL_URL.test(url) || marker || TITLE_WALL.test(title.trim())) return { status: 'blocked', reason: 'captcha', at: url };
  if (!content && (weakMarker || CAPTCHA.test(body))) return { status: 'blocked', reason: 'captcha', at: url };
  if (!content && loginText && loginText.test(body)) return { status: 'blocked', reason: 'login', at: url };
  return null;
}
// opts.content: the selector that is present on a real page of this kind.
export async function wall(page, opts = {}) {
  const title = await page.title().catch(() => '');
  const marker = !!(await page.$(WALL_DOM).catch(() => null));
  const content = opts.content ? !!(await page.$(opts.content).catch(() => null)) : true;
  const weakMarker = !content && !!(await page.$(WEAK_DOM).catch(() => null));
  const body = content ? '' : await page.evaluate(() => document.body?.innerText.slice(0, 4000) || '').catch(() => '');
  return wallFrom({ url: page.url(), title, body, marker, weakMarker, content }, opts);
}


export async function go(page, url, waitFor) {
  const resp = await page.goto(url, { waitUntil: 'domcontentloaded' });
  // Cloudflare's "Just a moment..." interstitial clears itself in a real browser within seconds.
  if (/just a moment/i.test(await page.title().catch(() => ''))) {
    await page.waitForFunction(() => !/just a moment/i.test(document.title), null, { timeout: 15000 }).catch(() => {});
    await page.waitForLoadState('domcontentloaded').catch(() => {});
  }
  if (waitFor) await page.waitForSelector(waitFor, { timeout: 10000 }).catch(() => {});
  return resp;
}
