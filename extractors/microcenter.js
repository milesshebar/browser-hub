// Micro Center product page, per store: "<productId>" or "<productId>@<storeid>" (029 is
// ship-only; a store's id is the `storeid` in the site's URLs once that store is selected).
import { wall, go } from './_lib.js';
export const name = 'microcenter';
export const required = ['price'];
export const canary = ['705936@029'];   // long-lived listings: `hubctl run <profile> --check`
export const url = id => { const [p, s] = id.split('@'); return `https://www.microcenter.com/product/${p}/x${s ? `?storeid=${s}` : ''}`; };
export async function lookup(page, id) {
  // ?storeid= also moves the profile's selected store (a cookie), which every other agent and
  // the person using the profile see. Put it back afterwards.
  const ctx = page.context();
  const before = (await ctx.cookies('https://www.microcenter.com')).filter(c => /store/i.test(c.name));
  try { return await read(page, id); }
  finally {
    // Leave the page first: its scripts keep setting the store cookie after DOMContentLoaded.
    await page.goto('about:blank').catch(() => {});
    const after = (await ctx.cookies('https://www.microcenter.com')).filter(c => /store/i.test(c.name));
    const changed = after.filter(a => !before.some(b => b.name === a.name && b.value === a.value));
    // Overwrite with the saved values; a store cookie that didn't exist before is removed by name.
    // (Measured 2026-11-11: addCookies restores reliably; clearCookies with a RegExp did not.)
    if (changed.length) await ctx.addCookies(before.filter(b => changed.some(c => c.name === b.name))).catch(() => {});
    for (const c of changed.filter(c => !before.some(b => b.name === c.name)))
      await ctx.clearCookies({ name: c.name, domain: c.domain }).catch(() => {});
  }
}
async function read(page, id) {
  const r = await go(page, url(id), '.big-price, #pnlInventory');
  const w = await wall(page, { content: 'h1' });
  if (w) return w;
  if (r?.status() === 404) return { status: 'not_found' };
  const data = await page.evaluate(() => {
    const t = s => document.querySelector(s)?.innerText.replace(/\s+/g, ' ').trim() || null;
    return { title: t('h1'), store: t('.storeName'), price: t('.big-price'), inventory: t('#pnlInventory') };
  });
  return { status: 'ok', data, url: page.url() };   // the tab moves on before the runner reads it
}
