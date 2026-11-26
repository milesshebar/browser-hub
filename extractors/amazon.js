// Amazon product page by ASIN: price, buy-box seller, ships-from, stock, condition.
// Selectors verified 2026-07-29, re-checked 2026-11-09.
import { clean, wall, go } from './_lib.js';
export const name = 'amazon';
export const minGapMs = 4000;   // Amazon's bot checkpoint is the strictest of these sites
export const required = ['title', 'price', 'seller'];
export const canary = ['B0D6NN6TM7', 'B0CBRJ63RT'];   // long-lived listings: `hubctl run <profile> --check`
export const url = asin => `https://www.amazon.com/dp/${encodeURIComponent(asin)}?th=1&psc=1`;
export async function lookup(page, asin) {
  if (!/^[A-Z0-9]{10}$/.test(asin)) return { status: 'error', error: 'expected a 10-character ASIN' };
  const r = await go(page, url(asin), '#productTitle, #g, form[action*="validateCaptcha"], input[type=checkbox]');
  if (r?.status() === 404) return { status: 'not_found' };
  const w = await wall(page, { loginUrl: /\/ap\/signin/, content: '#productTitle' });
  if (w) return w;
  const data = await page.evaluate(() => {
    const t = s => document.querySelector(s)?.innerText.replace(/\s+/g, ' ').trim() || null;
    return {
      title: t('#productTitle'),
      price: t('#corePrice_feature_div .a-offscreen') || t('#corePriceDisplay_desktop_feature_div .a-offscreen') || t('#apex_desktop .a-offscreen'),
      // Consumer pages split seller and ships-from; Amazon Business pages (a business account) print one
      // "Ships from and sold by X." line in #merchant-info instead (2026-11-11).
      ...(() => {
        const seller = t('#sellerProfileTriggerId') || t('#merchantInfoFeature_feature_div .offer-display-feature-text-message');
        const ships = t('#fulfillerInfoFeature_feature_div .offer-display-feature-text-message');
        if (seller) return { seller, shipsFrom: ships };
        const line = t('#merchant-info') || t('#shipsFromSoldByODF_feature_div') || '';
        // Up to the first sentence end: the line can go on ("... by Amazon. Gift-wrap available.").
        const both = line.match(/ships from and sold by (.+?)(?:\.\s|\.?$)/i);
        if (both) return { seller: both[1], shipsFrom: both[1] };
        return { seller: line.match(/sold by (.+?)(?: and |\.\s|\.?$)/i)?.[1] ?? null,
                 shipsFrom: line.match(/ships from (.+?)(?: and |\.\s|\.?$)/i)?.[1] ?? ships };
      })(),
      availability: t('#availability'),
      // Any used/renewed buy box, or a selected used accordion, means the price may not be new.
      condition: document.querySelector('#usedBuyBox, #renewedBuyBox, #usedAccordionRow.a-accordion-active, [id*="usedAccordion"][class*="active"]') ? 'NOT-NEW-BUYBOX' : 'new',
      prime: !!document.querySelector('#primeSavingsUpsellCaption_feature_div, .a-icon-prime'),
      otherOffers: t('#aod-ingress-link, #olpLinkWidget_feature_div'),
    };
  });
  // A page with no buy box at all is an unavailable listing, not a broken selector.
  if (!data.price && /currently unavailable/i.test(data.availability || '')) return { status: 'ok', data: { ...data, price: null, unavailable: true } };
  return { status: 'ok', data };
}
