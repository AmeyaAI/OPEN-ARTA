// Local declare so this file type-checks without `@types/node` installed
// at the repo root. The runtime is always Node (Playwright), where
// `process.env` is always present.
declare const process: { env: Record<string, string | undefined> };

/**
 * Stub API helper — covers LLM-generated specs that hallucinate
 * `import { API } from './api'` for setup/cleanup hooks. All methods are
 * no-ops that resolve to safe defaults so before/after hooks don't crash
 * the run. A Proxy returns no-op async functions for any unknown method
 * the LLM invents, so we never bail at "TypeError: api.fooBar is not a
 * function".
 *
 * Real backend calls should live in per-spec helpers or be wired via
 * project-specific conftest. If a spec actually needs seeded fixtures,
 * replace this stub or set `globalThis.__ARTA_API__` from a custom file.
 */
class APIImpl {
  constructor(public baseUrl: string = process.env.API_BASE_URL || process.env.BASE_URL || '') {
    return new Proxy(this, {
      get(target: any, prop: string | symbol): any {
        if (prop in target) return target[prop];
        // Any unknown method becomes a no-op async function returning {}.
        return async (..._args: unknown[]): Promise<Record<string, unknown>> => ({});
      },
    }) as any;
  }

  async seedTestEnvironment(): Promise<void> { /* no-op */ }
  async cleanupTestEnvironment(): Promise<void> { /* no-op */ }
  async resetState(): Promise<void> { /* no-op */ }
  async login(_user?: string, _password?: string): Promise<{ token: string }> {
    return { token: 'stub-token' };
  }
  async logout(): Promise<void> { /* no-op */ }
  async createUser(): Promise<{ id: string }> { return { id: 'stub-user-id' }; }
  async deleteUser(_id: string): Promise<void> { /* no-op */ }
}

export const API = APIImpl as unknown as new (baseUrl?: string) => APIImpl & {
  [key: string]: any;
};

export type API = InstanceType<typeof API>;
