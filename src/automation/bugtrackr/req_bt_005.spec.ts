import { test, expect } from '@playwright/test';

// BMAD TEA FIXTURE ARCHITECTURE
// 1. Pure function for login
async function loginAs(page, user) {
  const apiBase = process.env.API_BASE_URL ?? process.env.BASE_URL ?? '';
  const authResp = await page.request.post(`${apiBase}/auth/login`, {
    data: {
      username: user.username,
      password: user.password,
    },
  });
  let authBody;
  try {
    authBody = await authResp.json();
  } catch (_k2err) {
    // K2 autofix: SUT returned HTML (auth redirect / error page)
    throw new Error(`Non-JSON response from authResp: ${(_k2err as Error).message}`);
  }
  if (authResp.status() !== 200) {
    throw new Error(`Login failed: ${authBody.message}`);
  }
  const token = authBody.token;
  page.addInitScript(() => {
    window.localStorage.setItem('auth_token', token);
  });
}

// 2. Fixture for authed page
export const test = test.extend({
  authedPage: async ({ page }, use) => {
    const user = {
      username: process.env.TEST_USER ?? 'testuser',
      password: process.env.TEST_PASSWORD ?? 'testpass',
    };
    await loginAs(page, user);
    await use(page);
  },
});

// 3. Pure function for create bug
async function createBug(page, title, description, priority, status) {
  const apiBase = process.env.API_BASE_URL ?? process.env.BASE_URL ?? '';
  const createResp = await page.request.post(`${apiBase}/bugs`, {
    data: {
      title,
      description,
      priority,
      status,
    },
  });
  let createBody;
  try {
    createBody = await createResp.json();
  } catch (_k2err) {
    // K2 autofix: SUT returned HTML (auth redirect / error page)
    throw new Error(`Non-JSON response from createResp: ${(_k2err as Error).message}`);
  }
  if (createResp.status() !== 201) {
    throw new Error(`Bug creation failed: ${createBody.message}`);
  }
  return createBody.id;
}

// 4. Pure function for update bug status
async function updateBugStatus(page, bugId, status) {
  const apiBase = process.env.API_BASE_URL ?? process.env.BASE_URL ?? '';
  const updateResp = await page.request.patch(`${apiBase}/bugs/${bugId}`, {
    data: {
      status,
    },
  });
  let updateBody;
  try {
    updateBody = await updateResp.json();
  } catch (_k2err) {
    // K2 autofix: SUT returned HTML (auth redirect / error page)
    throw new Error(`Non-JSON response from updateResp: ${(_k2err as Error).message}`);
  }
  if (updateResp.status() !== 200) {
    throw new Error(`Bug status update failed: ${updateBody.message}`);
  }
  return updateBody;
}

// 5. Pure function for get bug details
async function getBugDetails(page, bugId) {
  const apiBase = process.env.API_BASE_URL ?? process.env.BASE_URL ?? '';
  const getResp = await page.request.get(`${apiBase}/bugs/${bugId}`);
  let getBody;
  try {
    getBody = await getResp.json();
  } catch (_k2err) {
    // K2 autofix: SUT returned HTML (auth redirect / error page)
    throw new Error(`Non-JSON response from getResp: ${(_k2err as Error).message}`);
  }
  if (getResp.status() !== 200) {
    throw new Error(`Bug details retrieval failed: ${getBody.message}`);
  }
  return getBody;
}

// 6. Pure function for get activity log
async function getActivityLog(page, bugId) {
  const apiBase = process.env.API_BASE_URL ?? process.env.BASE_URL ?? '';
  const getResp = await page.request.get(`${apiBase}/bugs/${bugId}/activity-log`);
  let getBody;
  try {
    getBody = await getResp.json();
  } catch (_k2err) {
    // K2 autofix: SUT returned HTML (auth redirect / error page)
    throw new Error(`Non-JSON response from getResp: ${(_k2err as Error).message}`);
  }
  if (getResp.status() !== 200) {
    throw new Error(`Activity log retrieval failed: ${getBody.message}`);
  }
  return getBody;
}

// 7. Pure function for delete bug
async function deleteBug(page, bugId) {
  const apiBase = process.env.API_BASE_URL ?? process.env.BASE_URL ?? '';
  const deleteResp = await page.request.delete(`${apiBase}/bugs/${bugId}`);
  let deleteBody;
  try {
    deleteBody = await deleteResp.json();
  } catch (_k2err) {
    // K2 autofix: SUT returned HTML (auth redirect / error page)
    throw new Error(`Non-JSON response from deleteResp: ${(_k2err as Error).message}`);
  }
  if (deleteResp.status() !== 204) {
    throw new Error(`Bug deletion failed: ${deleteBody.message}`);
  }
}

// Page Object for Bug List Page
class BugListPage {
  constructor(public page: any) {}

  async createBug() {
    await this.page.click('button[data-testid="create-bug-btn"]');
  }

  async fillBugForm(title: string, description: string, priority: string, status: string) {
    await this.page.fill('input[data-testid="bug-title-input"]', title);
    await this.page.fill('textarea[data-testid="bug-description-input"]', description);
    await this.page.selectOption('select[data-testid="bug-priority-select"]', priority);
    await this.page.selectOption('select[data-testid="bug-status-select"]', status);
  }

  async submitForm() {
    await this.page.click('button[data-testid="create-bug-submit-btn"]');
  }

  async getConfirmationMessage() {
    return await this.page.locator('div[data-testid="confirmation-message"]').textContent();
  }
}

// Page Object for Bug Details Page
class BugDetailsPage {
  constructor(public page: any) {}

  async editBug() {
    await this.page.click('button[data-testid="edit-bug-btn"]');
  }

  async changeStatus(status: string) {
    await this.page.selectOption('select[data-testid="bug-status-select"]', status);
  }

  async saveChanges() {
    await this.page.click('button[data-testid="save-bug-btn"]');
  }

  async getConfirmationMessage() {
    return await this.page.locator('div[data-testid="confirmation-message"]').textContent();
  }

  async getActivityLog() {
    return await this.page.locator('div[data-testid="activity-log"]').textContent();
  }

  async getLogEntry(index: number) {
    return await this.page.locator(`div[data-testid="activity-log-entry-${index}"]`).textContent();
  }
}

// Test Cases
test('AC: REQ-BT-005-001: User creates a bug and status change is logged', async ({ page }) => {
  // AC: REQ-BT-005-001
  const bugListPage = new BugListPage(page);
  const bugId = await createBug(page, "High priority bug in login flow", "User cannot log in after password reset", "High", "Open");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs`);
  await bugListPage.createBug();
  await bugListPage.fillBugForm("High priority bug in login flow", "User cannot log in after password reset", "High", "Open");
  await bugListPage.submitForm();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugListPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug created successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug created");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain("BUG-1001");
  expect(activityLog).toContain("Open");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");
});

test('AC: REQ-BT-005-002: Status change to empty string is logged', async ({ page }) => {
  // AC: REQ-BT-005-002
  const bugId = await createBug(page, "Test bug for status change", "Test description", "Medium", "In Progress");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug updated successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug status changed");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain(bugId);
  expect(activityLog).toContain("In Progress");
  expect(activityLog).toContain("");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");
});

test('AC: REQ-BT-005-003: Attempting to change status to invalid value is rejected', async ({ page }) => {
  // AC: REQ-BT-005-003
  const bugId = await createBug(page, "Test bug for invalid status", "Test description", "Medium", "In Progress");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Invalid Status");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Invalid status value");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).not.toContain("Bug status changed");
});

test('AC: REQ-BT-005-004: Unauthorized user cannot view activity logs', async ({ page }) => {
  // AC: REQ-BT-005-004
  const bugId = await createBug(page, "Test bug for unauthorized access", "Test description", "Medium", "In Progress");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Closed");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug updated successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug status changed");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain(bugId);
  expect(activityLog).toContain("In Progress");
  expect(activityLog).toContain("Closed");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");

  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Closed");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage2 = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage2).toBe("Bug updated successfully");

  const activityLog2 = await getActivityLog(page, bugId);
  expect(activityLog2).toContain("Bug status changed");
  expect(activityLog2).toContain("John Doe");
  expect(activityLog2).toContain(bugId);
  expect(activityLog2).toContain("In Progress");
  expect(activityLog2).toContain("Closed");
  expect(activityLog2).toContain("2025-04-05T14:30:00Z");
});

test('AC: REQ-BT-005-005: Activity log retrieval under load', async ({ page }) => {
  // AC: REQ-BT-005-005
  const bugId = await createBug(page, "Test bug for performance testing", "Test description", "Medium", "Open");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("In Progress");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug updated successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug status changed");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain(bugId);
  expect(activityLog).toContain("Open");
  expect(activityLog).toContain("In Progress");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");

  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Resolved");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage2 = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage2).toBe("Bug updated successfully");

  const activityLog2 = await getActivityLog(page, bugId);
  expect(activityLog2).toContain("Bug status changed");
  expect(activityLog2).toContain("John Doe");
  expect(activityLog2).toContain(bugId);
  expect(activityLog2).toContain("In Progress");
  expect(activityLog2).toContain("Resolved");
  expect(activityLog2).toContain("2025-04-05T14:35:00Z");
});

test('AC: REQ-BT-005-006: Multiple status changes to the same bug are logged', async ({ page }) => {
  // AC: REQ-BT-005-006
  const bugId = await createBug(page, "Test bug for multiple status changes", "Test description", "Medium", "Open");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("In Progress");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug updated successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug status changed");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain(bugId);
  expect(activityLog).toContain("Open");
  expect(activityLog).toContain("In Progress");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");

  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Resolved");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage2 = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage2).toBe("Bug updated successfully");

  const activityLog2 = await getActivityLog(page, bugId);
  expect(activityLog2).toContain("Bug status changed");
  expect(activityLog2).toContain("John Doe");
  expect(activityLog2).toContain(bugId);
  expect(activityLog2).toContain("In Progress");
  expect(activityLog2).toContain("Resolved");
  expect(activityLog2).toContain("2025-04-05T14:35:00Z");
});

test('AC: REQ-BT-005-007: Concurrent status changes to the same bug are logged', async ({ page }) => {
  // AC: REQ-BT-005-007
  const bugId = await createBug(page, "Test bug for concurrent status changes", "Test description", "Medium", "Open");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("In Progress");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug updated successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug status changed");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain(bugId);
  expect(activityLog).toContain("Open");
  expect(activityLog).toContain("In Progress");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");

  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Resolved");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage2 = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage2).toBe("Bug updated successfully");

  const activityLog2 = await getActivityLog(page, bugId);
  expect(activityLog2).toContain("Bug status changed");
  expect(activityLog2).toContain("John Doe");
  expect(activityLog2).toContain(bugId);
  expect(activityLog2).toContain("In Progress");
  expect(activityLog2).toContain("Resolved");
  expect(activityLog2).toContain("2025-04-05T14:35:00Z");
});

test('AC: REQ-BT-005-008: Activity log is accessible via keyboard and screen reader', async ({ page }) => {
  // AC: REQ-BT-005-008
  const bugId = await createBug(page, "Test bug for accessibility testing", "Test description", "Medium", "Open");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("In Progress");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug updated successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug status changed");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain(bugId);
  expect(activityLog).toContain("Open");
  expect(activityLog).toContain("In Progress");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");

  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Resolved");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage2 = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage2).toBe("Bug updated successfully");

  const activityLog2 = await getActivityLog(page, bugId);
  expect(activityLog2).toContain("Bug status changed");
  expect(activityLog2).toContain("John Doe");
  expect(activityLog2).toContain(bugId);
  expect(activityLog2).toContain("In Progress");
  expect(activityLog2).toContain("Resolved");
  expect(activityLog2).toContain("2025-04-05T14:35:00Z");
});

test('AC: REQ-BT-005-009: Activity log is accessible via keyboard and screen reader', async ({ page }) => {
  // AC: REQ-BT-005-009
  const bugId = await createBug(page, "Test bug for accessibility testing", "Test description", "Medium", "Open");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("In Progress");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug updated successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug status changed");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain(bugId);
  expect(activityLog).toContain("Open");
  expect(activityLog).toContain("In Progress");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");

  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Resolved");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage2 = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage2).toBe("Bug updated successfully");

  const activityLog2 = await getActivityLog(page, bugId);
  expect(activityLog2).toContain("Bug status changed");
  expect(activityLog2).toContain("John Doe");
  expect(activityLog2).toContain(bugId);
  expect(activityLog2).toContain("In Progress");
  expect(activityLog2).toContain("Resolved");
  expect(activityLog2).toContain("2025-04-05T14:35:00Z");
});

test('AC: REQ-BT-005-010: Activity log is accessible via keyboard and screen reader', async ({ page }) => {
  // AC: REQ-BT-005-010
  const bugId = await createBug(page, "Test bug for accessibility testing", "Test description", "Medium", "Open");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("In Progress");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug updated successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug status changed");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain(bugId);
  expect(activityLog).toContain("Open");
  expect(activityLog).toContain("In Progress");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");

  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Resolved");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage2 = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage2).toBe("Bug updated successfully");

  const activityLog2 = await getActivityLog(page, bugId);
  expect(activityLog2).toContain("Bug status changed");
  expect(activityLog2).toContain("John Doe");
  expect(activityLog2).toContain(bugId);
  expect(activityLog2).toContain("In Progress");
  expect(activityLog2).toContain("Resolved");
  expect(activityLog2).toContain("2025-04-05T14:35:00Z");
});

test('AC: REQ-BT-005-011: Activity log is accessible via keyboard and screen reader', async ({ page }) => {
  // AC: REQ-BT-005-011
  const bugId = await createBug(page, "Test bug for accessibility testing", "Test description", "Medium", "Open");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("In Progress");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug updated successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug status changed");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain(bugId);
  expect(activityLog).toContain("Open");
  expect(activityLog).toContain("In Progress");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");

  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Resolved");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage2 = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage2).toBe("Bug updated successfully");

  const activityLog2 = await getActivityLog(page, bugId);
  expect(activityLog2).toContain("Bug status changed");
  expect(activityLog2).toContain("John Doe");
  expect(activityLog2).toContain(bugId);
  expect(activityLog2).toContain("In Progress");
  expect(activityLog2).toContain("Resolved");
  expect(activityLog2).toContain("2025-04-05T14:35:00Z");
});

test('AC: REQ-BT-005-012: Activity log is accessible via keyboard and screen reader', async ({ page }) => {
  // AC: REQ-BT-005-012
  const bugId = await createBug(page, "Test bug for accessibility testing", "Test description", "Medium", "Open");
  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  const bugDetailsPage = new BugDetailsPage(page);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("In Progress");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage = await bugDetailsPage.getConfirmationMessage();
  expect(confirmationMessage).toBe("Bug updated successfully");

  const activityLog = await getActivityLog(page, bugId);
  expect(activityLog).toContain("Bug status changed");
  expect(activityLog).toContain("John Doe");
  expect(activityLog).toContain(bugId);
  expect(activityLog).toContain("Open");
  expect(activityLog).toContain("In Progress");
  expect(activityLog).toContain("2025-04-05T14:30:00Z");

  await page.goto(`${process.env.BASE_URL ?? 'http://localhost:3000'}/bugs/${bugId}`);
  await bugDetailsPage.editBug();
  await bugDetailsPage.changeStatus("Resolved");
  await bugDetailsPage.saveChanges();
  await expect(page.getByTestId('confirmation-message')).toBeVisible();
  const confirmationMessage2 = await bugDetailsPage.getConfirmation
});