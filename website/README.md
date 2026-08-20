# ARTA website

The public website and documentation for ARTA, built with
[Astro](https://astro.build) + [Starlight](https://starlight.astro.build) and
deployed to GitHub Pages at https://ameyaai.github.io/OPEN-ARTA/.

## Local development

```bash
cd website
npm ci
npm run dev        # http://localhost:4321/OPEN-ARTA/
```

## Build

```bash
npm run build      # output in website/dist/
npm run preview    # serve the production build locally
```

`npm run build` first runs `scripts/og.mjs` (via `prebuild`), which generates
`public/og.png` for social cards at build time — the image is gitignored so
the committed tree stays text-only.

## Deployment

Deployment is automatic: `.github/workflows/deploy-pages.yml` builds and
publishes the site on every push to `main` that touches `website/` (or
manually via *Run workflow*). There is no manual deploy step.

## Layout

```
website/
├── astro.config.mjs          # site config, sidebar, SEO defaults
├── public/                   # favicon, robots.txt (og.png generated here)
├── scripts/og.mjs            # build-time social-card generator
└── src/
    ├── content/docs/         # all pages (Starlight content collection)
    │   ├── docs/             # documentation  → /docs/*
    │   ├── community.mdx     # → /community
    │   ├── roadmap.mdx       # → /roadmap
    │   └── enterprise.mdx    # → /enterprise
    ├── pages/index.astro     # custom landing page
    └── styles/custom.css     # theme (dark/light) + landing primitives
```

## Content rules

Website copy follows the same content guards as the rest of the repo (CI
scans every tracked file): no customer names, no real email addresses
(`@arta.dev` / `@example.com` placeholders only), no private IPs, and no
committed binaries. Claims about ARTA functionality must be grounded in the
repository — commands shown must exist, and illustrative output must be
labeled as such.
