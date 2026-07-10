// fetch-silver.js
// Fetches silver price (per tola) from fenegosida.org
// Runs via GitHub Actions twice daily — 10:30 AM + 11:10 AM NPT

const https = require('https');

const PROJECT_ID = process.env.FIREBASE_PROJECT_ID;
const API_KEY    = process.env.FIREBASE_API_KEY;

if (!PROJECT_ID || !API_KEY) {
  console.error('❌ Missing FIREBASE_PROJECT_ID or FIREBASE_API_KEY env vars.');
  process.exit(1);
}

// ── Fetch URL ──
function get(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept':     'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Cache-Control':   'no-cache',
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
    req.setTimeout(20000, () => { req.destroy(); reject(new Error('Timeout')); });
  });
}

// ── Multi-strategy fetch (bypass IP blocking) ──
async function fetchFenegosida() {
  const TARGET = 'https://fenegosida.org/';
  const strategies = [
    { name: 'Direct',      url: TARGET },
    { name: 'allorigins',  url: 'https://api.allorigins.win/raw?url=' + encodeURIComponent(TARGET) },
    { name: 'corsproxy',   url: 'https://corsproxy.io/?' + encodeURIComponent(TARGET) },
    { name: 'codetabs',    url: 'https://api.codetabs.com/v1/proxy?quest=' + encodeURIComponent(TARGET) },
    { name: 'thingproxy',  url: 'https://thingproxy.freeboard.io/fetch/' + TARGET },
  ];
  for (const s of strategies) {
    try {
      console.log('🌐 Trying:', s.name);
      const html = await get(s.url);
      if (html && html.includes('SILVER')) {
        console.log('✅', s.name, 'succeeded');
        return html;
      }
    } catch (e) {
      console.log('⚠️ ', s.name, 'failed:', e.message);
    }
  }
  throw new Error('All fetch strategies failed.');
}

// ── Strip HTML tags/scripts/styles down to plain text ──
// Key fix: the old parser depended on an exact "* tola" marker string and
// raw <b> tag positions, both of which are extremely sensitive to the
// site's markup. Parsing plain, tag-free text instead makes this resistant
// to markup changes as long as the visible wording stays roughly the same.
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

// ── Parse silver price (per tola) from fenegosida.org ──
//
// Page lists FINE GOLD, TEJABI GOLD, SILVER, each with a "per 10 grm" row
// and a "per 1 tola" row. We want SILVER's "per 1 tola" figure — anchor
// directly on the phrase "SILVER ... per 1 tola" (not "per 10 grm"), then
// grab the रु/Nrs-prefixed price shortly after it.
function parseSilverPrice(html) {
  const text = stripHtml(html);

  // Primary strategy: anchor on "SILVER" immediately followed by "per 1 tola"
  const m = text.match(/SILVER\s*per\s*1\s*tola[\s\S]{0,200}?(?:[\u0930\u0941]+|Nrs)\s*\*{0,2}(\d{3,6})(?:\.\d+)?\*{0,2}/i);
  if (m) {
    const price = parseInt(m[1], 10);
    if (price > 500 && price < 50000) {
      console.log('✅ Anchored SILVER per-1-tola price found:', price);
      return price;
    }
  }

  // Fallback 1: same anchor but with a wider window, in case more text sits between
  const wide = text.match(/SILVER\s*per\s*1\s*tola[\s\S]{0,600}?(?:[\u0930\u0941]+|Nrs)\s*\*{0,2}(\d{3,6})(?:\.\d+)?\*{0,2}/i);
  if (wide) {
    const price = parseInt(wide[1], 10);
    if (price > 500 && price < 50000) {
      console.log('⚠️  Wide-window SILVER per-1-tola price found:', price);
      return price;
    }
  }

  // Fallback 2: "per 1 tola" occurs 3x on the page (Fine Gold, Tejabi, Silver, in that
  // order) — Silver is always the third occurrence, so grab the price right after it.
  const tolaMatches = [...text.matchAll(/per\s*1\s*tola/gi)];
  if (tolaMatches.length >= 3) {
    const idx = tolaMatches[2].index + tolaMatches[2][0].length;
    const window = text.slice(idx, idx + 200);
    const wm = window.match(/(?:[\u0930\u0941]+|Nrs)\s*\*{0,2}(\d{3,6})(?:\.\d+)?\*{0,2}/i);
    if (wm) {
      const price = parseInt(wm[1], 10);
      if (price > 500 && price < 50000) {
        console.log('⚠️  Third per-1-tola occurrence price found:', price);
        return price;
      }
    }
  }

  // Fallback 3: any 3-6 digit number in silver range after the last "tola" keyword
  const tolaBlock = text.match(/tola[\s\S]{0,1000}$/i);
  if (tolaBlock) {
    const nums = [...tolaBlock[0].matchAll(/\b(\d{3,5})(?:\.\d+)?\b/g)].map(m => parseInt(m[1]));
    const valid = nums.filter(n => n > 500 && n < 50000);
    if (valid.length) {
      console.log('⚠️  Silver tola block fallback:', valid[valid.length - 1]);
      return valid[valid.length - 1];
    }
  }

  return null;
}

// ── Write to global_data/silver_info ──
function writeToFirestore(price) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({
      fields: {
        rate:        { integerValue: String(price) },
        updatedBy:   { stringValue: 'auto@github-actions' },
        lastUpdated: { stringValue: new Date().toISOString() },
        source:      { stringValue: 'fenegosida.org' },
        note:        { stringValue: 'Silver per tola' }
      }
    });
    const urlObj = new URL(
      `https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/(default)/documents/global_data/silver_info?key=${API_KEY}`
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

// ── Log to silver_history collection ──
function logToHistory(price) {
  return new Promise((resolve) => {
    const body = JSON.stringify({
      fields: {
        rate:      { integerValue: String(price) },
        updatedBy: { stringValue: 'auto@github-actions' },
        timestamp: { stringValue: new Date().toISOString() },
        source:    { stringValue: 'fenegosida.org' },
        note:      { stringValue: 'Silver per tola' }
      }
    });
    const urlObj = new URL(
      `https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/(default)/documents/silver_history?key=${API_KEY}`
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
  console.log('🥈 Silver price auto-fetch starting...');
  console.log('📅 Time (UTC):', new Date().toISOString());
  console.log('🌐 Source: fenegosida.org');

  let html = null;
  try {
    html = await fetchFenegosida();
    console.log('📄 Page size:', Math.round(html.length / 1024) + 'KB');
  } catch (e) {
    console.error('❌ Fetch failed:', e.message);
    process.exit(1);
  }

  const price = parseSilverPrice(html);
  if (!price) {
    console.error('❌ Could not parse silver price from fenegosida.org');
    process.exit(1);
  }

  console.log('🥈 Silver price (per tola): NPR', price.toLocaleString());

  try {
    await writeToFirestore(price);
    console.log('🔥 Silver written to Firestore → global_data/silver_info');
  } catch (e) {
    console.error('❌ Firestore write failed:', e.message);
    process.exit(1);
  }

  try {
    await logToHistory(price);
    console.log('📋 Silver logged to silver_history collection');
  } catch (e) {
    console.log('⚠️  History log skipped:', e.message);
  }

  console.log('');
  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  console.log('✅ Done! silver_info.rate =', price);
  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
}

main();
