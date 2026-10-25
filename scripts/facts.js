(opts) => {
  // teardown-pairs facts extraction (spec 3.7). One function, run with
  // page.evaluate after the load sequence, identical for every side. It
  // reads the rendered DOM only and returns plain JSON. The CLI unwraps
  // archive replay URLs afterwards, so hrefs here are taken as the browser
  // resolves them.
  const o = opts || {};
  const VIEW_W = window.innerWidth;
  const VIEW_H = window.innerHeight;
  const SCROLL_Y = window.scrollY || 0;
  const PRICE_RE = /[$£€¥₹]\s?\d/;
  const PRICE_TOKEN_RE = /[$£€¥₹]\s?\d[\d.,]*/;
  const PROMO_RE = /%\s*off|\boff\b|\bsale\b|free shipping|free delivery|\bcode\b/i;
  const CART_RE = /add to (cart|bag|basket)/i;
  const REVIEW_RE = /\b\d[\d,.]*\s*(reviews?|ratings?)\b/i;
  const FILTER_RE = /filter|facet|refine/i;
  const REFERENCE_RE = /Reference\s*#\s*\d+(?:\.[0-9a-f]+)+/i;
  const CAPTCHA_MARKERS = ['hcaptcha', 'recaptcha', 'px-captcha', 'captcha-delivery'];
  const HIDDEN_TEXT = 'script,style,noscript,template,svg,title,head';
  const BUTTONS = 'button, [role=button], a[class*="btn" i], a[class*="button" i]';
  const FOLD_BUTTONS = BUTTONS + ', input[type=submit], input[type=button]';

  const clean = (s, max) => {
    const t = String(s === undefined || s === null ? '' : s).replace(/\s+/g, ' ').trim();
    return max && t.length > max ? t.slice(0, max) : t;
  };
  const str = (v) => (v === undefined || v === null || v === '' ? null : clean(v, 300));
  const abs = (href) => {
    if (!href) return '';
    try { return new URL(href, location.href).href; } catch (e) { return String(href); }
  };
  const meta = (sel) => {
    const m = document.querySelector(sel);
    return m ? clean(m.getAttribute('content'), 500) : '';
  };

  const styleCache = new Map();
  const style = (el) => {
    let s = styleCache.get(el);
    if (!s) { s = getComputedStyle(el); styleCache.set(el, s); }
    return s;
  };
  const visibleCache = new Map();
  const visible = (el) => {
    if (!el || el.nodeType !== 1) return false;
    let v = visibleCache.get(el);
    if (v !== undefined) return v;
    v = false;
    if (el.getClientRects().length) {
      const s = style(el);
      if (s.display !== 'none' && s.visibility !== 'hidden' && s.visibility !== 'collapse' && parseFloat(s.opacity) !== 0) {
        const r = el.getBoundingClientRect();
        v = r.width > 0 && r.height > 0;
      }
    }
    visibleCache.set(el, v);
    return v;
  };
  const inFold = (r) => r.top + SCROLL_Y < VIEW_H && r.bottom + SCROLL_Y > 0;
  const textOf = (el) => clean(el.innerText || el.value || el.getAttribute('aria-label') || el.textContent, 120);
  const uniqueTexts = (els, max) => {
    const out = [];
    const seen = new Set();
    for (const el of els) {
      if (out.length >= max) break;
      if (!visible(el)) continue;
      const t = textOf(el);
      if (!t || seen.has(t.toLowerCase())) continue;
      seen.add(t.toLowerCase());
      out.push(t);
    }
    return out;
  };

  // Visible text nodes in document order.
  const nodes = [];
  if (document.body) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        const p = n.parentElement;
        if (!p || p.closest(HIDDEN_TEXT) || !visible(p)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let n;
    while ((n = walker.nextNode()) && nodes.length < 20000) nodes.push(n);
  }

  // Text blocks: each text node belongs to its nearest ancestor that is not
  // display:inline, so a heading, a button and a paragraph are separate
  // blocks while a link inside a paragraph is not.
  const blockOf = (el) => {
    while (el && el !== document.body) {
      const d = style(el).display;
      if (d !== 'inline' && d !== 'contents') return el;
      el = el.parentElement;
    }
    return document.body;
  };
  const blocks = new Map();
  for (const n of nodes) {
    const blk = blockOf(n.parentElement);
    let b = blocks.get(blk);
    if (!b) {
      const range = document.createRange();
      range.selectNodeContents(n);
      const r = range.getBoundingClientRect();
      b = { parts: [], font: parseFloat(style(n.parentElement).fontSize) || null, top: r.top + SCROLL_Y,
            bottom: r.bottom + SCROLL_Y, left: r.left, area: r.width * r.height };
      blocks.set(blk, b);
    }
    b.parts.push(n.nodeValue);
  }
  const blockList = [];
  for (const b of blocks.values()) {
    const text = clean(b.parts.join(' '), 300);
    if (text && b.area > 0) blockList.push({ text, font_size: b.font, top: b.top, bottom: b.bottom, left: b.left });
  }
  const foldBlocks = blockList
    .filter((b) => b.top < VIEW_H && b.bottom > 0)
    .sort((a, b) => Math.round(a.top / 4) - Math.round(b.top / 4) || a.left - b.left)
    .slice(0, 25)
    .map((b) => ({ text: b.text, font_size: b.font_size }));

  const foldButtons = uniqueTexts(
    Array.from(document.querySelectorAll(FOLD_BUTTONS)).filter((el) => inFold(el.getBoundingClientRect())), 30);

  // Images in the first viewport: media elements plus sizeable CSS backgrounds.
  let foldImages = 0;
  if (document.body) {
    const all = document.body.getElementsByTagName('*');
    const limit = Math.min(all.length, 6000);
    for (let i = 0; i < limit; i++) {
      const el = all[i];
      const tag = el.tagName.toLowerCase();
      const media = tag === 'img' || tag === 'svg' || tag === 'video' || tag === 'canvas' ||
        (tag !== 'img' && el.getAttribute('role') === 'img');
      const r = el.getBoundingClientRect();
      if (r.width < 16 || r.height < 16 || !inFold(r)) continue;
      if (media) {
        if (visible(el)) foldImages++;
      } else if (r.width >= 50 && r.height >= 50) {
        const bg = style(el).backgroundImage;
        if (bg && bg !== 'none' && bg.indexOf('url(') !== -1 && visible(el)) foldImages++;
      }
    }
  }

  const promos = [];
  const promoSeen = new Set();
  for (const b of blockList) {
    if (promos.length >= 20) break;
    if (b.top < 300 && PROMO_RE.test(b.text) && !promoSeen.has(b.text.toLowerCase())) {
      promoSeen.add(b.text.toLowerCase());
      promos.push(b.text);
    }
  }

  const navLinks = [];
  document.querySelectorAll('header nav a[href], header [role=navigation] a[href], ' +
                            '[role=banner] nav a[href], [role=banner] [role=navigation] a[href]').forEach((a) => {
    if (navLinks.length >= 40) return;
    navLinks.push({ text: clean(a.innerText || a.textContent || a.getAttribute('aria-label'), 120),
                    href: a.href, visible: visible(a) });
  });

  const scopeEl = document.querySelector('main');
  const scope = scopeEl || document.body;
  const priceParents = new Set();
  for (const n of nodes) {
    if (scope && scope.contains(n) && PRICE_RE.test(n.nodeValue)) priceParents.add(n.parentElement);
  }

  // Structured data: every ld+json block; bad JSON is recorded, not fatal.
  const sd = { blocks: 0, errors: [], types: [], product: null, breadcrumbs: [], item_list: null };
  const typed = [];
  const walk = (v, depth) => {
    if (depth > 40 || v === null || typeof v !== 'object') return;
    if (Array.isArray(v)) { v.forEach((x) => walk(x, depth + 1)); return; }
    const t = v['@type'];
    const ts = (Array.isArray(t) ? t : (t ? [t] : [])).map(String);
    ts.forEach((x) => { if (!sd.types.includes(x)) sd.types.push(x); });
    if (ts.length) typed.push({ types: ts, node: v });
    for (const k of Object.keys(v)) if (k !== '@type' && k !== '@context') walk(v[k], depth + 1);
  };
  document.querySelectorAll('script[type="application/ld+json" i]').forEach((s, i) => {
    sd.blocks++;
    let data;
    try { data = JSON.parse(s.textContent); } catch (e) {
      sd.errors.push({ index: i, error: clean(e && e.message ? e.message : e, 200) });
      return;
    }
    walk(data, 0);
  });
  const firstOf = (type) => { const x = typed.find((t) => t.types.includes(type)); return x ? x.node : null; };
  const prod = firstOf('Product');
  if (prod) {
    let offer = Array.isArray(prod.offers) ? prod.offers[0] : prod.offers;
    offer = offer && typeof offer === 'object' ? offer : {};
    let price = offer.price;
    if (price === undefined) price = offer.lowPrice;
    if (price === undefined && offer.priceSpecification) price = offer.priceSpecification.price;
    const rating = prod.aggregateRating && typeof prod.aggregateRating === 'object' ? prod.aggregateRating : {};
    let brand = prod.brand;
    if (Array.isArray(brand)) brand = brand[0];
    if (brand && typeof brand === 'object') brand = brand.name;
    sd.product = {
      name: str(prod.name), brand: str(brand), sku: str(prod.sku), price: str(price),
      currency: str(offer.priceCurrency), availability: str(offer.availability),
      rating: str(rating.ratingValue),
      review_count: str(rating.reviewCount !== undefined ? rating.reviewCount : rating.ratingCount),
    };
  }
  const crumbs = firstOf('BreadcrumbList');
  if (crumbs && Array.isArray(crumbs.itemListElement)) {
    sd.breadcrumbs = crumbs.itemListElement
      .map((e) => (e && (e.name || (e.item && typeof e.item === 'object' && e.item.name))) || null)
      .filter(Boolean).map((x) => clean(x, 120));
  }
  const itemList = firstOf('ItemList');
  if (itemList) {
    if (itemList.numberOfItems !== undefined && !isNaN(Number(itemList.numberOfItems))) {
      sd.item_list = { count: Number(itemList.numberOfItems), source: 'numberOfItems' };
    } else if (Array.isArray(itemList.itemListElement)) {
      sd.item_list = { count: itemList.itemListElement.length, source: 'elements' };
    }
  }

  // PDP heuristics.
  const h1 = document.querySelector('h1');
  let firstPrice = null;
  if (h1) {
    for (const n of nodes) {
      if (!(h1.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING) || h1.contains(n)) continue;
      const m = n.nodeValue.match(PRICE_TOKEN_RE);
      if (m) { firstPrice = clean(m[0], 40); break; }
    }
  }
  let cart = { present: false, text: null };
  for (const el of document.querySelectorAll('button, [role=button], input[type=submit], input[type=button], a')) {
    if (!visible(el)) continue;
    const t = textOf(el);
    if (CART_RE.test(t)) { cart = { present: true, text: clean(t, 80) }; break; }
  }
  let reviewText = null;
  for (const n of nodes) {
    const m = n.nodeValue.match(REVIEW_RE);
    if (m) { reviewText = clean(m[0], 80); break; }
  }

  // PLP heuristics.
  let filterCount = 0;
  const filterLabels = [];
  const labelSeen = new Set();
  const cands = document.querySelectorAll('[aria-label], [class]');
  for (let i = 0; i < cands.length && i < 8000; i++) {
    const el = cands[i];
    const aria = el.getAttribute('aria-label') || '';
    const cls = el.getAttribute('class') || '';
    if (!FILTER_RE.test(aria) && !FILTER_RE.test(cls)) continue;
    filterCount++;
    const label = clean(aria || el.innerText || el.textContent, 60);
    if (label && filterLabels.length < 20 && !labelSeen.has(label)) { labelSeen.add(label); filterLabels.push(label); }
  }

  // Signals for suspect-page detection (spec 3.6).
  const bodyText = clean(document.body ? document.body.innerText : '');
  const captcha = [];
  for (const marker of CAPTCHA_MARKERS) {
    const sel = 'iframe[src*="' + marker + '" i], script[src*="' + marker + '" i], [id*="' + marker + '" i], ' +
      '[class*="' + marker + '" i]';
    let hit = false;
    try { hit = !!document.querySelector(sel); } catch (e) { hit = false; }
    if (!hit && marker === 'hcaptcha') hit = !!document.querySelector('.h-captcha');
    if (!hit && marker === 'recaptcha') hit = !!document.querySelector('.g-recaptcha');
    if (hit) captcha.push(marker);
  }

  const canon = document.querySelector('link[rel~="canonical" i]');
  return {
    page: {
      url: o.url || location.href,
      final_url: location.href,
      title: clean(document.title, 300),
      description: meta('meta[name="description" i]'),
      canonical: canon ? abs(canon.getAttribute('href')) : '',
      robots: meta('meta[name="robots" i]'),
      og_title: meta('meta[property="og:title" i]'),
      og_image: meta('meta[property="og:image" i]'),
      lang: document.documentElement.getAttribute('lang') || '',
    },
    headings: {
      h1: Array.from(document.querySelectorAll('h1')).map((h) => clean(h.innerText || h.textContent, 200)).filter(Boolean),
      h2: Array.from(document.querySelectorAll('h2')).slice(0, 30).map((h) => clean(h.innerText || h.textContent, 200))
        .filter(Boolean),
    },
    nav: navLinks,
    above_fold: {
      viewport: { width: VIEW_W, height: VIEW_H },
      text_blocks: foldBlocks,
      buttons: foldButtons,
      image_count: foldImages,
    },
    ctas: uniqueTexts(document.querySelectorAll(BUTTONS), 50),
    promos: promos,
    counts: {
      images: document.images.length,
      links: document.querySelectorAll('a[href]').length,
      forms: document.forms.length,
      iframes: document.querySelectorAll('iframe').length,
      price_like: priceParents.size,
      price_scope: scopeEl ? 'main' : 'body',
    },
    structured_data: sd,
    pdp_heuristic: { label: 'heuristic', first_price_after_h1: firstPrice, add_to_cart: cart,
                     review_count_text: reviewText },
    plp_heuristic: { label: 'heuristic', price_like_in_main: priceParents.size, price_scope: scopeEl ? 'main' : 'body',
                     filter_elements: filterCount, filter_labels: filterLabels },
    signals: { text_chars: bodyText.length, text_sample: bodyText.slice(0, 2000), captcha: captcha,
               reference_block: REFERENCE_RE.test(bodyText) },
  };
}
