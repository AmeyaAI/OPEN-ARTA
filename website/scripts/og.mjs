// Generates public/og.png (1200×630) at build time from an inline SVG, so no
// binary is ever committed to the repo (the export gates keep the tree text-only).
import sharp from 'sharp';
import { mkdir } from 'node:fs/promises';

const svg = `
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630">
  <rect width="1200" height="630" fill="#0c0e12"/>
  <rect x="0" y="0" width="1200" height="4" fill="#e8a33d"/>
  <path d="M80 180 L118 96 L156 180" fill="none" stroke="#e8a33d" stroke-width="12" stroke-linecap="square"/>
  <path d="M99 152 L114 167 L139 133" fill="none" stroke="#f2f0eb" stroke-width="10" stroke-linecap="square"/>
  <text x="80" y="300" font-family="DejaVu Sans, sans-serif" font-size="76" font-weight="bold" fill="#f2f0eb">ARTA</text>
  <text x="80" y="380" font-family="DejaVu Sans, sans-serif" font-size="34" fill="#b8b5ad">Test automation that understands your software.</text>
  <text x="80" y="470" font-family="DejaVu Sans Mono, monospace" font-size="24" fill="#8a8880">grounded generation · six runtimes · truthful reports</text>
  <text x="80" y="560" font-family="DejaVu Sans Mono, monospace" font-size="24" fill="#e8a33d">open source · Apache-2.0</text>
</svg>`;

await mkdir(new URL('../public/', import.meta.url), { recursive: true });
await sharp(Buffer.from(svg)).png().toFile(new URL('../public/og.png', import.meta.url).pathname);
console.log('og.png generated');
