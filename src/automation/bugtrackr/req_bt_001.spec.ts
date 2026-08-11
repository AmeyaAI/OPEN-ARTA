import { test, expect } from '@playwright/test';

const apiBase = process.env.API_BASE_URL ?? process.env.BASE_URL ?? '';
const testUser = process.env.TEST_USER ?? 'test@example.com';

test.describe('Bug CRUD Operations', () => {
  test.beforeEach(async ({ page }) => {
    // Seed test data
    await page.request.post(`${apiBase}/v1/auth/login`, {
      data: {
        email: testUser,
        password: 'password'
      }
    });
  });

  test('AC-1: User can create a new bug with title, description, and priority', async ({ page }) => {
    // AC: AC-1
    await page.goto('/bugs/create');
    await expect(page.getByTestId('bug-title')).toBeVisible();
    await expect(page.getByTestId('bug-description')).toBeVisible();
    await expect(page.getByTestId('bug-priority')).toBeVisible();
    
    await page.getByTestId('bug-title').fill('Login Page Crash on Submit');
    await page.getByTestId('bug-description').fill('When submitting the login form, the page crashes with a JavaScript error.');
    await page.getByTestId('bug-priority').selectOption('Critical');
    
    await page.getByTestId('create-bug-button').click();
    
    await expect(page.getByTestId('confirmation-message')).toBeVisible();
    await expect(page.getByTestId('confirmation-message')).toHaveText('Bug created successfully');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).toContainText('Login Page Crash on Submit');
    await expect(page.getByTestId('bugs-table')).toContainText('When submitting the login form, the page crashes with a JavaScript error.');
    await expect(page.getByTestId('bugs-table')).toContainText('Critical');
  });

  test('AC-1: User can create a new bug with title, description, and priority with boundary conditions', async ({ page }) => {
    // AC: AC-1
    const testData = [
      { title: "Login Crash", description: "When submitting the login form, the page crashes with a JavaScript error.", priority: "Critical" },
      { title: "Test Bug", description: "This is a test bug created for the test scenario.", priority: "High" },
      { title: "Bug 123", description: "This bug is numbered 123 and should be tracked.", priority: "Medium" },
      { title: "Bug 001", description: "This bug is numbered 001 and should be tracked.", priority: "Low" },
      { title: "", description: "This is an empty title test case.", priority: "Low" },
      { title: "Title", description: "", priority: "Low" },
      { title: "Title", description: "Description with spaces and tabs.", priority: "Critical" }
    ];

    for (const data of testData) {
      await page.goto('/bugs/create');
      await expect(page.getByTestId('bug-title')).toBeVisible();
      await expect(page.getByTestId('bug-description')).toBeVisible();
      await expect(page.getByTestId('bug-priority')).toBeVisible();
      
      await page.getByTestId('bug-title').fill(data.title);
      await page.getByTestId('bug-description').fill(data.description);
      await page.getByTestId('bug-priority').selectOption(data.priority);
      
      await page.getByTestId('create-bug-button').click();
      
      await expect(page.getByTestId('confirmation-message')).toBeVisible();
      await expect(page.getBytextContent('Bug created successfully')).toBeVisible();
      
      await expect(page.getByTestId('bugs-table')).toBeVisible();
      await expect(page.getByTestId('bugs-table')).toContainText(data.title);
      await expect(page.getByTestId('bugs-table')).toContainText(data.description);
      await expect(page.getByTestId('bugs-table')).toContainText(data.priority);
    }
  });

  test('AC-1: User cannot create a bug with empty title', async ({ page }) => {
    // AC: AC-1
    await page.goto('/bugs/create');
    await expect(page.getByTestId('bug-title')).toBeVisible();
    await expect(page.getByTestId('bug-description')).toBeVisible();
    await expect(page.getByTestId('bug-priority')).toBeVisible();
    
    await page.getByTestId('bug-description').fill('This is a test description.');
    await page.getByTestId('bug-priority').selectOption('Low');
    
    await page.getByTestId('create-bug-button').click();
    
    await expect(page.getByTestId('error-message')).toBeVisible();
    await expect(page.getByTestId('error-message')).toHaveText('Title is required');
    
    await expect(page.getByTestId('bugs-table')).not.toBeVisible();
  });

  test('AC-1: User cannot create a bug with invalid priority', async ({ page }) => {
    // AC: AC-1
    await page.goto('/bugs/create');
    await expect(page.getByTestId('bug-title')).toBeVisible();
    await expect(page.getByTestId('bug-description')).toBeVisible();
    await expect(page.getByTestId('bug-priority')).toBeVisible();
    
    await page.getByTestId('bug-title').fill('Invalid Priority Bug');
    await page.getByTestId('bug-description').fill('This bug has an invalid priority.');
    
    await page.getByTestId('bug-priority').selectOption('Invalid');
    
    await page.getByTestId('create-bug-button').click();
    
    await expect(page.getByTestId('error-message')).toBeVisible();
    await expect(page.getByTestId('error-message')).toHaveText('Priority must be Low, Medium, High, or Critical');
    
    await expect(page.getByTestId('bugs-table')).not.toBeVisible();
  });

  test('AC-2: User can view bug details by clicking on a bug', async ({ page }) => {
    // AC: AC-2
    await page.goto('/bugs');
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    
    await page.getByTestId('bug-title').first().click();
    
    await expect(page.getByTestId('bug-details')).toBeVisible();
    await expect(page.getByTestId('bug-details')).toContainText('Login Page Crash on Submit');
    await expect(page.getByTestId('bug-details')).toContainText('When submitting the login form, the page crashes with a JavaScript error.');
    await expect(page.getByTestId('bug-details')).toContainText('Critical');
  });

  test('AC-2: User can view bug details by clicking on a bug with boundary conditions', async ({ page }) => {
    // AC: AC-2
    const testData = [
      { title: "Login Crash", description: "When submitting the login form, the page crashes with a JavaScript error.", priority: "Critical" },
      { title: "Test Bug", description: "This is a test bug created for the test scenario.", priority: "High" },
      { title: "Bug 123", description: "This bug is numbered 123 and should be tracked.", priority: "Medium" },
      { title: "Bug 001", description: "This bug is numbered 001 and should be tracked.", priority: "Low" }
    ];

    for (const data of testData) {
      await page.goto('/bugs');
      await expect(page.getByTestId('bugs-table')).toBeVisible();
      
      await page.getByTestId('bug-title').first().click();
      
      await expect(page.getByTestId('bug-details')).toBeVisible();
      await expect(page.getByTestId('bug-details')).toContainText(data.title);
      await expect(page.getByTestId('bug-details')).toContainText(data.description);
      await expect(page.getByTestId('bug-details')).toContainText(data.priority);
    }
  });

  test('AC-3: User can update bug fields', async ({ page }) => {
    // AC: AC-3
    await page.goto('/bugs');
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    
    await page.getByTestId('bug-title').first().click();
    
    await page.getByTestId('bug-title').fill('New Title');
    await page.getByTestId('bug-description').fill('New Description');
    await page.getByTestId('bug-priority').selectOption('High');
    
    await page.getByTestId('update-bug-button').click();
    
    await expect(page.getByTestId('confirmation-message')).toBeVisible();
    await expect(page.getByTestId('confirmation-message')).toHaveText('Bug updated successfully');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).toContainText('New Title');
    await expect(page.getByTestId('bugs-table')).toContainText('New Description');
    await expect(page.getByTestId('bugs-table')).toContainText('High');
  });

  test('AC-3: User can update bug fields with boundary conditions', async ({ page }) => {
    // AC: AC-3
    const testData = [
      { original_title: "Old Title", new_title: "New Title", original_description: "Old Description", new_description: "New Description", original_priority: "Low", new_priority: "High" },
      { original_title: "Bug 123", new_title: "Bug 123", original_description: "This bug is numbered 123.", new_description: "This bug is numbered 123.", original_priority: "Medium", new_priority: "Medium" },
      { original_title: "Test Bug", new_title: "Test Bug", original_description: "This is a test bug.", new_description: "This is a test bug.", original_priority: "High", new_priority: "High" },
      { original_title: "Bug 001", new_title: "Bug 001", original_description: "This bug is numbered 001.", new_description: "This bug is numbered 001.", original_priority: "Low", new_priority: "Low" }
    ];

    for (const data of testData) {
      await page.goto('/bugs');
      await expect(page.getByTestId('bugs-table')).toBeVisible();
      
      await page.getByTestId('bug-title').first().click();
      
      await page.getByTestId('bug-title').fill(data.new_title);
      await page.getByTestId('bug-description').fill(data.new_description);
      await page.getByTestId('bug-priority').selectOption(data.new_priority);
      
      await page.getByTestId('update-bug-button').click();
      
      await expect(page.getByTestId('confirmation-message')).toBeVisible();
      await expect(page.getByTestId('confirmation-message')).toHaveText('Bug updated successfully');
      
      await expect(page.getByTestId('bugs-table')).toBeVisible();
      await expect(page.getByTestId('bugs-table')).toContainText(data.new_title);
      await expect(page.getByTestId('bugs-table')).toContainText(data.new_description);
      await expect(page.getByTestId('bugs-table')).toContainText(data.new_priority);
    }
  });

  test('AC-3: User cannot update bug with empty title', async ({ page }) => {
    // AC: AC-3
    await page.goto('/bugs');
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    
    await page.getByTestId('bug-title').first().click();
    
    await page.getByTestId('bug-title').fill('');
    await page.getByTestId('bug-description').fill('New Description');
    await page.getByTestId('bug-priority').selectOption('High');
    
    await page.getByTestId('update-bug-button').click();
    
    await expect(page.getByTestId('error-message')).toBeVisible();
    await expect(page.getByTestId('error-message')).toHaveText('Title is required');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).toContainText('Old Title');
    await expect(page.getByTestId('bugs-table')).toContainText('Old Description');
    await expect(page.getByTestId('bugs-table')).toContainText('Low');
  });

  test('AC-3: User cannot update bug with invalid priority', async ({ page }) => {
    // AC: AC-3
    await page.goto('/bugs');
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    
    await page.getByTestId('bug-title').first().click();
    
    await page.getByTestId('bug-title').fill('New Title');
    await page.getByTestId('bug-description').fill('New Description');
    await page.getByTestId('bug-priority').selectOption('Invalid');
    
    await page.getByTestId('update-bug-button').click();
    
    await expect(page.getByTestId('error-message')).toBeVisible();
    await expect(page.getByTestId('error-message')).toHaveText('Priority must be Low, Medium, High, or Critical');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).toContainText('Old Title');
    await expect(page.getByTestId('bugs-table')).toContainText('Old Description');
    await expect(page.getByTestId('bugs-table')).toContainText('Low');
  });

  test('AC-4: User can delete a bug', async ({ page }) => {
    // AC: AC-4
    await page.goto('/bugs');
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    
    await page.getByTestId('bug-title').first().click();
    
    await page.getByTestId('delete-bug-button').click();
    
    await expect(page.getByTestId('confirmation-message')).toBeVisible();
    await expect(page.getByTestId('confirmation-message')).toHaveText('Bug deleted successfully');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).not.toContainText('Test Bug');
    await expect(page.getByTestId('bugs-table')).not.toContainText('This is a test bug.');
    await expect(page.getByTestId('bugs-table')).not.toContainText('Medium');
  });

  test('AC-4: User can delete a bug with boundary conditions', async ({ page }) => {
    // AC: AC-4
    const testData = [
      { title: "Login Crash", description: "When submitting the login form, the page crashes with a JavaScript error.", priority: "Critical" },
      { title: "Test Bug", description: "This is a test bug created for the test scenario.", priority: "High" },
      { title: "Bug 123", description: "This bug is numbered 123 and should be tracked.", priority: "Medium" },
      { title: "Bug 001", description: "This bug is numbered 001 and should be tracked.", priority: "Low" }
    ];

    for (const data of testData) {
      await page.goto('/bugs');
      await expect(page.getByTestId('bugs-table')).toBeVisible();
      
      await page.getByTestId('bug-title').first().click();
      
      await page.getByTestId('delete-bug-button').click();
      
      await expect(page.getByTestId('confirmation-message')).toBeVisible();
      await expect(page.getByTestId('confirmation-message')).toHaveText('Bug deleted successfully');
      
      await expect(page.getByTestId('bugs-table')).toBeVisible();
      await expect(page.getByTestId('bugs-table')).not.toContainText(data.title);
      await expect(page.getByTestId('bugs-table')).not.toContainText(data.description);
      await expect(page.getByTestId('bugs-table')).not.toContainText(data.priority);
    }
  });

  test('AC-4: User cannot delete a bug without confirmation', async ({ page }) => {
    // AC: AC-4
    await page.goto('/bugs');
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    
    await page.getByTestId('bug-title').first().click();
    
    await page.getByTestId('delete-bug-button').click();
    
    await expect(page.getByTestId('error-message')).toBeVisible();
    await expect(page.getByTestId('error-message')).toHaveText('Are you sure you want to delete this bug?');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).toContainText('Test Bug');
    await expect(page.getByTestId('bugs-table')).toContainText('This is a test bug.');
    await expect(page.getByTestId('bugs-table')).toContainText('Medium');
  });

  test('AC-1 @security: Unauthorized user cannot create a bug', async ({ page }) => {
    // AC: AC-1 @security
    await page.goto('/bugs/create');
    await expect(page.getByTestId('bug-title')).toBeVisible();
    await expect(page.getByTestId('bug-description')).toBeVisible();
    await expect(page.getByTestId('bug-priority')).toBeVisible();
    
    await page.getByTestId('bug-title').fill('Unauthorized Bug');
    await page.getByTestId('bug-description').fill('This is an unauthorized test.');
    await page.getByTestId('bug-priority').selectOption('Low');
    
    await page.getByTestId('create-bug-button').click();
    
    await expect(page.getByTestId('error-message')).toBeVisible();
    await expect(page.getByTestId('error-message')).toHaveText('You are not authorized to perform this action.');
    
    await expect(page.getByTestId('bugs-table')).not.toBeVisible();
  });

  test('AC-2 @security: Unauthorized user cannot view bug details', async ({ page }) => {
    // AC: AC-2 @security
    await page.goto('/bugs');
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    
    await page.getByTestId('bug-title').first().click();
    
    await expect(page.getByTestId('error-message')).toBeVisible();
    await expect(page.getByTestId('error-message')).toHaveText('You are not authorized to perform this action.');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).toContainText('Unauthorized Bug');
  });

  test('AC-3 @security: Unauthorized user cannot update a bug', async ({ page }) => {
    // AC: AC-3 @security
    await page.goto('/bugs');
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    
    await page.getByTestId('bug-title').first().click();
    
    await page.getByTestId('bug-title').fill('New Title');
    await page.getByTestId('bug-description').fill('New Description');
    await page.getByTestId('bug-priority').selectOption('High');
    
    await page.getByTestId('update-bug-button').click();
    
    await expect(page.getByTestId('error-message')).toBeVisible();
    await expect(page.getByTestId('error-message')).toHaveText('You are not authorized to perform this action.');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).toContainText('Unauthorized Bug');
    await expect(page.getByTestId('bugs3-table')).toContainText('This is an unauthorized test.');
    await expect(page.getByTestId('bugs-table')).toContainText('Low');
  });

  test('AC-4 @security: Unauthorized user cannot delete a bug', async ({ page }) => {
    // AC: AC-4 @security
    await page.goto('/bugs');
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    
    await page.getByTestId('bug-title').first().click();
    
    await page.getByTestId('delete-bug-button').click();
    
    await expect(page.getByTestId('error-message')).toBeVisible();
    await expect(page.getByTestId('error-message')).toHaveText('You are not authorized to perform this action.');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).toContainText('Unauthorized Bug');
    await expect(page.getByTestId('bugs-table')).toContainText('This is an unauthorized test.');
    await expect(page.getByTestId('bugs-table')).toContainText('Low');
  });

  test('AC-1 @security: XSS attack attempt via title field', async ({ page }) => {
    // AC: AC-1 @security
    await page.goto('/bugs/create');
    await expect(page.getByTestId('bug-title')).toBeVisible();
    await expect(page.getByTestId('bug-description')).toBeVisible();
    await expect(page.getByTestId('bug-priority')).toBeVisible();
    
    await page.getByTestId('bug-title').fill('<script>alert("XSS")</script>');
    await page.getByTestId('bug-description').fill('XSS test');
    await page.getByTestId('bug-priority').selectOption('Low');
    
    await page.getByTestId('create-bug-button').click();
    
    await expect(page.getByTestId('confirmation-message')).toBeVisible();
    await expect(page.getByTestId('confirmation-message')).toHaveText('Bug created successfully');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).toContainText('XSS Test Bug');
    await expect(page.getByTestId('bugs-table')).toContainText('XSS test');
    await expect(page.getByTestId('bugs-table')).toContainText('Low');
  });

  test('AC-1 @security: XSS attack attempt via description field', async ({ page }) => {
    // AC: AC-1 @security
    await page.goto('/bugs/create');
    await expect(page.getByTestId('bug-title')).toBeVisible();
    await expect(page.getByTestId('bug-description')).toBeVisible();
    await expect(page.getByTestId('bug-priority')).toBeVisible();
    
    await page.getByTestId('bug-title').fill('XSS Test Bug');
    await page.getByTestId('bug-description').fill('<script>alert("XSS")</script>');
    await page.getByTestId('bug-priority').selectOption('Low');
    
    await page.getByTestId('create-bug-button').click();
    
    await expect(page.getByTestId('confirmation-message')).toBeVisible();
    await expect(page.getByTestId('confirmation-message')).toHaveText('Bug created successfully');
    
    await expect(page.getByTestId('bugs-table')).toBeVisible();
    await expect(page.getByTestId('bugs-table')).toContainText('XSS Test Bug');
    await expect(page.getByTestId('bugs-table')).toContainText('XSS test');
    await expect(page.getByTestId('bugs-table')).toContainText('Low');
  });

  test('AC-1 @security: XSS attack attempt via priority field', async ({ page }) => {
    // AC: AC-1 @security
    await page.goto('/bugs/create');
    await expect(page.getByTestId('bug-title')).toBeVisible();
    await expect(page.getByTestId('bug-description')).toBeVisible();
    await expect(page.getByTestId('bug-priority')).toBeVisible();
    
    await page.getByTestId('bug-title').fill('XSS Test Bug');
    await page.getByTestId('bug-description').fill('XSS test');
    await page.getByTestId('bug-priority').selectOption('<script>alert("XSS")</script>');
    
    await page.getByTestId('create-bug-button').click();
    
    await expect(page.getByTestId('error-message')).toBeVisible();
    await expect(page.getByTestId('error-message')).toHaveText('Priority must be Low, Medium, High, or Critical');
    
    await expect(page.getByTestId('bugs-table')).not.toBeVisible();
  });
});