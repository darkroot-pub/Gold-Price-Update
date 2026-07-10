// fetch-gold.js
// Fetches gold price from fenegosida.org (official Nepal Gold & Silver Dealers Federation)
// Runs via GitHub Actions twice daily — 11:30 AM + 7:00 PM NPT

const https = require('https');

// ── Firebase config (injected from GitHub Secrets) ──
const PROJECT_ID = process.env.FIREBASE_PROJECT_ID;
const API_KEY    = process.env.FIREBASE_API_KEY;

if (!PROJECT_ID || !API_KEY) {
  console.error('❌ Missing FIREBASE_PROJECT_ID or FIREBASE_API_KEY env vars.');
  process.exit(1);
}

// ── Fetch URL (follows redirects) ──
function get(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (compatible; GoldBot/1.0)',
        'Accept':     'text/html,application/xhtml+xml',
      }
    }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        return get(res.headers.location).then(resolve).catch(reject);
      }
      if (res.statusCode !== 200) {
        return reject(new Error('HTTP ' + res.statusCode));
      }
      let data = '';
      res.setEncoding('utf8');
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(data));
    });
    req.on('error', reject);
    req.setTimeout(15000, () => { req.destroy(); reject(new Error('Timeout')); });
  });
}

// ── Strip HTML tags/scripts/styles down to plain text ──
// This is the key fix: the old parser regex'd against raw HTML, so any
// markup change (new wrapper divs, ad widgets, etc.) between a label like
// "per 1 tola" and its price could push them further apart in character
// distance than the old {0,100}/{0,300} windows allowed — even though
// they're right next to each other visually. Stripping tags first makes
// parsing resistant to that kind of structural change.
function stripHtml(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&#2352;/g, '\u0930')   // रु rendered as numeric HTML entities, just in case
    .replace(/&#2369;/g, '\u0941')
    .replace(/\s+/g, ' ')
    .trim();
}

// ── Parse gold price from fenegosida.org ──
// Page lists FINE GOLD, TEJABI GOLD, SILVER — each with a "per 10 grm" row
// and a "per 1 tola" row. We want the FINE GOLD "per 1 tola" figure, and
// "per 1 tola" appears three times on the page (fine/tejabi/silver) — the
// FIRST occurrence is always the FINE GOLD one, so we anchor on that.
function parseGoldPrice(html) {
  const text = stripHtml(html);

  // Primary strategy: anchor on the first "per 1 tola" (always FINE GOLD's row),
  // then look for a रु/Nrs-prefixed price shortly after it.
  const tolaIdx = text.search(/per\s*1\s*tola/i);
  if (tolaIdx !== -1) {
    const window = text.slice(tolaIdx, tolaIdx + 200);
    const m = window.match(/(?:[\u0930\u0941]+|Nrs)\s*\*{0,2}(\d{4,7})(?:\.\d+)?\*{0,2}/i);
    if (m) {
      const price = parseInt(m[1], 10);
      if (price > 100000 && price < 600000) {
        console.log('✅ Anchored per-1-tola price found:', price);
        return price;
      }
    }
  }

  // Fallback 1: explicit "FINE GOLD (9999)" label followed by "per 1 tola" within a wide window
  const fine = text.match(/FINE GOLD[\s\S]{0,600}?per\s*1\s*tola[\s\S]{0,200}?(?:[\u0930\u0941]+|Nrs)\s*\*{0,2}(\d{4,7})/i);
  if (fine) {
    const price = parseInt(fine[1], 10);
    if (price > 100000 && price < 600000) {
      console.log('✅ Fine Gold (9999) per tola found:', price);
      return price;
    }
  }

  // Fallback 2: any रु-prefixed 5-7 digit number in the gold price range
  const ruMatches = [...text.matchAll(/[\u0930\u0941]+\s*\*{0,2}(\d{5,7})\*{0,2}/g)];
  for (const m of ruMatches) {
    const price = parseInt(m[1], 10);
    if (price > 100000 && price < 600000) {
      console.log('✅ रु pattern price found:', price);
      return price;
    }
  }

  // Fallback 3: any Nrs-prefixed 5-7 digit number in range
  const nrsMatches = [...text.matchAll(/Nrs\s*\*{0,2}(\d{5,7})\*{0,2}/gi)];
  for (const m of nrsMatches) {
    const price = parseInt(m[1], 10);
    if (price > 100000 && price < 600000) {
      console.log('✅ Nrs pattern price found:', price);
      return price;
    }
  }

  return null;
}

// ── Write to Firestore via REST API ──
function writeToFirestore(price) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({
      fields: {
        rate:        { integerValue: String(price) },
        updatedBy:   { stringValue: 'auto@github-actions' },
        lastUpdated: { stringValue: new Date().toISOString() },
        source:      { stringValue: 'fenegosida.org' },
        note:        { stringValue: 'Fine Gold (9999) per tola' }
      }
    });

    const urlObj = new URL(
      `https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/(default)/documents/global_data/gold_info?key=${API_KEY}`
    );

    const req = https.request({
      hostname: urlObj.hostname,
      path:     urlObj.pathname + urlObj.search,
      method:   'PATCH',
      headers: {
        'Content-Type':   'application/json',
        'Content-Length': Buffer.byteLength(body)
      }
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        if (res.statusCode === 200) resolve(JSON.parse(data));
        else reject(new Error('Firestore error ' + res.statusCode + ': ' + data));
      });
    });

    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ── Also log to gold_history collection ──
function logToHistory(price) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({
      fields: {
        rate:      { integerValue: String(price) },
        updatedBy: { stringValue: 'auto@github-actions' },
        timestamp: { stringValue: new Date().toISOString() },
        source:    { stringValue: 'fenegosida.org' },
        note:      { stringValue: 'Fine Gold (9999) per tola' }
      }
    });

    const urlObj = new URL(
      `https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/(default)/documents/gold_history?key=${API_KEY}`
    );

    const req = https.request({
      hostname: urlObj.hostname,
      path:     urlObj.pathname + urlObj.search,
      method:   'POST',
      headers: {
        'Content-Type':   'application/json',
        'Content-Length': Buffer.byteLength(body)
      }
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(data));
    });

    req.on('error', e => resolve('history log skipped: ' + e.message));
    req.write(body);
    req.end();
  });
}

// ── Main ──
async function main() {
  console.log('🕘 Gold price auto-fetch starting...');
  console.log('📅 Time (UTC):', new Date().toISOString());
  console.log('🌐 Source: fenegosida.org (Nepal Gold & Silver Dealers Federation)');

  let html = null;
  try {
    console.log('🌐 Fetching https://fenegosida.org/');
    html = await get('https://fenegosida.org/');
    console.log('📄 Page size:', Math.round(html.length / 1024) + 'KB');

    if (!html.includes('FINE GOLD') && !html.includes('tola')) {
      throw new Error('Page fetched but no gold data found — site structure may have changed.');
    }
  } catch (e) {
    console.error('❌ Fetch failed:', e.message);
    process.exit(1);
  }

  const price = parseGoldPrice(html);
  if (!price) {
    console.error('❌ Could not parse gold price from fenegosida.org');
    process.exit(1);
  }

  console.log('💰 Fine Gold price (per tola): NPR', price.toLocaleString());

  try {
    await writeToFirestore(price);
    console.log('🔥 Successfully written to Firestore!');
  } catch (e) {
    console.error('❌ Firestore write failed:', e.message);
    process.exit(1);
  }

  try {
    await logToHistory(price);
    console.log('📋 Logged to gold_history collection');
  } catch (e) {
    console.log('⚠️  History log skipped:', e.message);
  }

  console.log('✅ Done. gold_info.rate =', price);
}

main();
