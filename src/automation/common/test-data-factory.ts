/**
 * ARTA Generic Test Data Factory
 * Creates and cleans up test data via API for any project.
 */
export class TestDataFactory {
  private baseUrl: string;
  private headers: Record<string, string>;

  constructor(baseUrl: string, authHeaders: Record<string, string> = {}) {
    this.baseUrl = baseUrl;
    this.headers = { 'Content-Type': 'application/json', ...authHeaders };
  }

  async create(endpoint: string, data: Record<string, any>): Promise<any> {
    const response = await fetch(`${this.baseUrl}${endpoint}`, {
      method: 'POST',
      headers: this.headers,
      body: JSON.stringify(data),
    });
    if (!response.ok) throw new Error(`Failed to create at ${endpoint}: ${response.status}`);
    return response.json();
  }

  async get(endpoint: string): Promise<any> {
    const response = await fetch(`${this.baseUrl}${endpoint}`, { headers: this.headers });
    return response.json();
  }

  async update(endpoint: string, data: Record<string, any>): Promise<any> {
    const response = await fetch(`${this.baseUrl}${endpoint}`, {
      method: 'PATCH',
      headers: this.headers,
      body: JSON.stringify(data),
    });
    return response.json();
  }

  async delete(endpoint: string): Promise<void> {
    await fetch(`${this.baseUrl}${endpoint}`, { method: 'DELETE', headers: this.headers });
  }

  /** Generate a unique test identifier */
  static uniqueId(prefix = 'arta'): string {
    return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
  }
}
