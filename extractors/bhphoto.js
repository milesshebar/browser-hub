// B&H by product id ("1834129" or "1834129-REG"): price, stock, BH#/MFR#. Or "q:<terms>" for the
// top search results (name, BH#, price, stock), when the agent doesn't know the id yet.
import { clean, wall, go } from './_lib.js';
export const name = 'bhphoto';
export const required = ['title', 'price', 'stock'];
export const hitRequired = ['name', 'price', 'stock'];
export const canary = ['1975130-REG', 'q:Ryzen 5 9600X'];   // long-lived listings: `hubctl run <profile> --check`
export const url = id => id.startsWith('q:')
  ? `https://www.bhphotovideo.com/c/search?q=${encodeURIComponent(id.slice(2))}`
  : `https://www.bhphotovideo.com/c/product/${id.replace(/-REG$/i, '')}-REG`;
export async function lookup(page, id) {
  const search = id.startsWith('q:');
  const r = await go(page, url(id), search ? '[data-selenium="miniProductPage"], [data-selenium="titleNumberingPagination"]' : '[data-selenium="productTitle"]');
  const w = await wall(page, { loginUrl: /\/login/, content: search ? '[data-selenium="titleNumberingPagination"], [data-selenium="miniProductPage"]' : '[data-selenium="productTitle"]' });
  if (w) return w;
  if (r?.status() === 404) return { status: 'not_found' };
  if (search) {
    const hits = await page.evaluate(() => [...document.querySelectorAll('[data-selenium="miniProductPage"]')].slice(0, 8).map(el => {
      const t = s => el.querySelector(`[data-selenium="${s}"]`)?.innerText.replace(/\s+/g, ' ').trim() || null;
      const conv = t('miniProductPageProductConversion') || '';
      return { name: t('miniProductPageProductName'), sku: t('miniProductPageProductSkuInfo'),
        url: el.querySelector('a[data-selenium="miniProductPageProductNameLink"]')?.href || null,
        // "$205.99 $179 99 Holiday Savings $26.00 Add to Cart…": the price paid is the last one
        // before any savings line (cents are a separate superscript, hence "$179 99").
        // The last amount before any savings line ("$205.99 $179 99 Holiday Savings $26.00"; cents
        // are a superscript, so the price element alone drops them). Financing ("/mo") is never a price.
        price: conv.split(/Save|Savings|Add to Cart/i)[0].replace(/\$[\d,.]+\s*\/\s*mo\S*/gi, '')
          .match(/\$[\d,]+(\.\d\d| \d\d)?/g)?.pop()?.replace(' ', '.') || null,
        stock: conv.match(/In Stock|Backordered|Special Order|More on the Way|Out of Stock|Discontinued|Expected[^$]*?(?=Add|$)/i)?.[0]?.trim() || null };
    }));
    const count = await page.$eval('[data-selenium="titleNumberingPagination"]', e => e.innerText).catch(() => null);
    return hits.length ? { status: 'ok', data: { count, hits } } : { status: 'not_found', data: { count } };
  }
  const data = await page.evaluate(() => {
    const t = s => document.querySelector(`[data-selenium="${s}"]`)?.innerText.replace(/\s+/g, ' ').trim() || null;
    return { title: t('productTitle'), price: t('pricingPrice'), wasPrice: t('strikethroughPrice'),
      stock: t('stockStatus') || t('stockInfo'), codes: t('codeCrumb'), ship: t('shipMessage') };
  });
  return { status: 'ok', data };
}
