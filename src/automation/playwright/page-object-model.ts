import type { Page } from '@playwright/test';

/**
 * Stub PageObjectModel — covers LLM-generated specs that hallucinate
 * `import { PageObjectModel } from './page-object-model'`. Forwards every
 * unknown method/property to the underlying Page via a Proxy so any
 * Playwright API the spec reaches for (page.goto, page.click, page.locator,
 * page.url, page.title, page.content, page.waitFor*, …) just works at runtime
 * without us having to enumerate every method.
 *
 * The cast at the bottom tells TypeScript that an instance of this class is
 * structurally identical to a Page (plus a `page` accessor) so strict-mode
 * compilation succeeds even when the spec calls Page methods we never
 * declared explicitly.
 */
class PageObjectModelImpl {
  constructor(public page: Page) {
    return new Proxy(this, {
      get(target: any, prop: string | symbol): any {
        if (prop in target) return target[prop];
        const value = (target.page as any)[prop];
        if (typeof value === 'function') return value.bind(target.page);
        return value;
      },
      has(target: any, prop: string | symbol): boolean {
        return prop in target || prop in (target.page as any);
      },
    }) as any;
  }

  /**
   * Best-effort accessor for the most recent response — generated specs use
   * `page.getLastResponse()` even though Playwright has no such API. We return
   * `null` so spec code at least continues to a meaningful assertion failure
   * rather than a TypeError on `.json()`.
   */
  async getLastResponse(): Promise<unknown | null> {
    return null;
  }
}

// Export with a type assertion so TS treats instances as full Page objects.
// This is intentional: the runtime behaviour is "Page + a few extras" thanks
// to the Proxy; the type system just needs to agree.
export const PageObjectModel = PageObjectModelImpl as unknown as new (page: Page) => Page & {
  page: Page;
  getLastResponse: () => Promise<unknown | null>;
};

export type PageObjectModel = InstanceType<typeof PageObjectModel>;
