'use strict';
// Usage: node render.js stills 1,5,9   |  node render.js frames <fps> <scale>
const { chromium } = require('playwright-core');
const path = require('path');
const fs = require('fs');

(async () => {
  const [mode, a1, a2] = process.argv.slice(2);
  const scale = mode === 'frames' ? Number(a2 || 1) : 1;
  const browser = await chromium.launch({ channel: 'chrome' });
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: scale });
  await page.goto('file:///' + path.join(__dirname, 'promo.html').replace(/\\/g, '/'));
  await page.evaluate(() => window.ready);
  if (mode === 'stills') {
    fs.mkdirSync(path.join(__dirname, 'stills'), { recursive: true });
    for (const t of a1.split(',')) {
      await page.evaluate(x => window.seek(x), Number(t));
      await page.screenshot({ path: path.join(__dirname, 'stills', `t${t}.png`) });
    }
  } else {
    const fps = Number(a1 || 30);
    const dir = path.join(__dirname, 'frames');
    fs.rmSync(dir, { recursive: true, force: true });
    fs.mkdirSync(dir);
    const dur = await page.evaluate(() => window.DURATION);
    const n = Math.round(dur * fps);
    for (let i = 0; i < n; i++) {
      await page.evaluate(x => window.seek(x), i / fps);
      await page.screenshot({ path: path.join(dir, `f${String(i).padStart(5, '0')}.png`) });
      if (i % 150 === 0) console.log(`${i}/${n}`);
    }
    console.log('done', n);
  }
  await browser.close();
})();
