// Newegg product page by item number (N82E…, 9SI…). Forces ?Item=, because a bare /p/<item> URL
// can redirect to a different listing, and reports the seller so a 3P listing can't pass as Newegg's.
import { wall, go } from './_lib.js';
export const name = 'newegg';
export const required = ['title', 'price', 'seller'];
export const canary = ['N82E16819113844', 'N82E16813162131'];   // long-lived listings: `hubctl run <profile> --check`
export const url = item => `https://www.newegg.com/p/${encodeURIComponent(item)}?Item=${encodeURIComponent(item)}`;
export async function lookup(page, item) {
  const r = await go(page, url(item), '.product-title, .page-404');
  if (r?.status() === 404 || await page.$('.page-404')) return { status: 'not_found' };
  const w = await wall(page, { loginUrl: /secure\.newegg\.com\/identity/, content: '.product-title' });
  if (w) return w;
  // Some listings show "Buy Used" and "Buy New" options side by side in one buy box, Used first
  // (hit 2026-11-10: a 9600X at $159 used against $219 new), and the page's first price is the Used
  // one. Take the New option's price and report Used apart. The seller line belongs to the listing,
  // whose item number says whose it is: N82E… is Newegg's own, 9SI… a marketplace seller's.
  const hasOpts = await page.locator('.product-raidobox').count() > 0;
  const data = await page.evaluate(hasOpts => {
    const t = s => document.querySelector(s)?.innerText.replace(/\s+/g, ' ').trim() || null;
    const price = s => s && s.querySelector('[class^="price-current"]')?.innerText.replace(/\s+/g, ' ').trim() || null;
    const opts = [...document.querySelectorAll('.product-raidobox')];
    // Each option box carries its own label ("Buy Used", "Buy New"; 2026-11-11). Trust exactly one
    // box that says New and doesn't say Used; anything else is ambiguous and gives no price.
    const newBoxes = opts.filter(o => /buy new/i.test(o.innerText) && !/used|refurb|open box/i.test(o.innerText));
    const nu = newBoxes.length === 1 ? newBoxes[0] : null;
    const used = opts.find(o => /used|refurb|open box/i.test(o.innerText)) || null;
    // The price class picked up a suffix in 2026 (.price-current_2026), so match the prefix.
    const shown = price(document.querySelector('.product-buy-box')) || price(document.querySelector('.product-price--x')) || price(document.querySelector('.product-pane'));
    const buy = t('.product-buy-box') || '';
    const d = {
      title: t('.product-title'),
      price: hasOpts ? price(nu) : shown,
      usedPrice: price(used),
      seller: t('.product-buy-box .product-seller') || t('.product-seller-sold-by') ||
        (buy.match(/Sold (?:and shipped )?by:?\s*([^.$]+?)(?=\s+Shipped|\.|$)/i)?.[1]?.trim() ?? null),
      stock: t('.product-inventory') || (/in stock/i.test(buy) ? 'In stock' : null),
      outOfStock: /out of stock/i.test(buy), comingSoon: /coming soon/i.test(buy),
    };
    // Two options but no way to tell which is New: no price, so the run says partial.
    if (hasOpts && !nu) d.price = null;
    return d;
  }, hasOpts);
  data.firstParty = /^N82E/i.test(item) && (/sold (?:and shipped )?by:?\s*newegg\b/i.test(data.seller || '') || /^newegg$/i.test(data.seller || ''));
  return { status: 'ok', data };
}
