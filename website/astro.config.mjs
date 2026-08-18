// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

const site = 'https://gangadharneeli.github.io';
const base = '/OPEN-ARTA';

// https://astro.build/config
export default defineConfig({
	site,
	base,
	trailingSlash: 'ignore',
	integrations: [
		starlight({
			title: 'ARTA',
			description:
				'Open-source AI test automation that understands your software — grounded test generation, six execution runtimes, and truthful, audit-grade reporting.',
			logo: { src: './public/favicon.svg', alt: 'ARTA' },
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/gangadharneeli/OPEN-ARTA' },
			],
			editLink: {
				baseUrl: 'https://github.com/gangadharneeli/OPEN-ARTA/edit/main/website/',
			},
			customCss: [
				'@fontsource-variable/bricolage-grotesque',
				'@fontsource/ibm-plex-mono/400.css',
				'@fontsource/ibm-plex-mono/500.css',
				'./src/styles/custom.css',
			],
			head: [
				{ tag: 'meta', attrs: { property: 'og:image', content: `${site}${base}/og.png` } },
				{ tag: 'meta', attrs: { name: 'twitter:card', content: 'summary_large_image' } },
				{ tag: 'meta', attrs: { name: 'twitter:image', content: `${site}${base}/og.png` } },
			],
			sidebar: [
				{
					label: 'Start here',
					items: [
						{ label: 'Documentation', slug: 'docs' },
						{ label: 'Getting started', slug: 'docs/getting-started' },
						{ label: 'Installation', slug: 'docs/installation' },
						{ label: 'Your first test run', slug: 'docs/quickstart' },
					],
				},
				{
					label: 'Understand ARTA',
					items: [
						{ label: 'Core concepts', slug: 'docs/concepts' },
						{ label: 'Architecture', slug: 'docs/architecture' },
						{ label: 'AI agents', slug: 'docs/agents' },
					],
				},
				{
					label: 'Configure',
					items: [
						{ label: 'Configuration', slug: 'docs/configuration' },
						{ label: 'Integrations', slug: 'docs/integrations' },
					],
				},
				{
					label: 'Contribute',
					items: [{ label: 'Contributing', slug: 'docs/contributing' }],
				},
			],
		}),
	],
});
