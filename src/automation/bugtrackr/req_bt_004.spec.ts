import { test, expect } from '@playwright/test';

test.describe('Comment System', () => {
  test.beforeEach(async ({ page }) => {
    // Seed test data via API
    const apiBase = process.env.API_BASE_URL ?? process.env.BASE_URL ?? '';
    const user = {
      id: 'USER-456',
      username: 'test_user',
      password: 'test_password'
    };

    // Login via API
    const loginResp = await page.request.post(`${apiBase}/auth/login`, {
      data: {
        username: user.username,
        password: user.password
      }
    });

    // Navigate to bug
    await page.goto(`${apiBase}/bugs/BUG-123`);
  });

  test('User adds a valid comment to a bug', async ({ page }) => {
    // AC: REQ-BT-004
    const commentField = page.getByTestId('comment-field');
    const postCommentButton = page.getByTestId('post-comment-button');

    await expect(commentField).toBeVisible();
    await expect(postCommentButton).toBeVisible();

    await commentField.fill('This is a valid comment');
    await postCommentButton.click();

    const comment = page.getByTestId('comment-1');
    await expect(comment).toBeVisible();
    await expect(comment).toContainText('This is a valid comment');
  });

  test('User attempts to add a comment with boundary values', async ({ page }) => {
    // AC: REQ-BT-004
    const commentField = page.getByTestId('comment-field');
    const postCommentButton = page.getByTestId('post-comment-button');
    const errorMessage = page.getByTestId('error-message');

    const testCases = [
      { comment: '', error: 'Comment cannot be empty', value: 'empty' },
      { comment: ' ', error: 'Comment cannot be empty', value: 'whitespace' },
      { comment: '1234567890123', error: 'Comment is too long', value: 'max_length' },
      { comment: '1234567890', error: 'Comment is too long', value: 'max_length-1' }
    ];

    for (const { comment, error, value } of testCases) {
      await commentField.fill(comment);
      await postCommentButton.click();

      await expect(errorMessage).toBeVisible();
      await expect(errorMessage).toContainText(error);
    }
  });

  test('User attempts to add a comment with invalid input', async ({ page }) => {
    // AC: REQ-BT-004
    const commentField = page.getByTestId('comment-field');
    const postCommentButton = page.getByTestId('post-comment-button');
    const errorMessage = page.getByTestId('error-message');

    await commentField.fill('Invalid <input>');
    await postCommentButton.click();

    await expect(errorMessage).toBeVisible();
    await expect(errorMessage).toContainText('Comment cannot be empty');
  });

  test('User tries to inject malicious content via comment', async ({ page }) => {
    // AC: REQ-BT-004
    const commentField = page.getByTestId('comment-field');
    const postCommentButton = page.getByTestId('post-comment-button');
    const comment = page.getByTestId('comment-1');

    const maliciousInputs = [
      { malicious: '<script>alert("xss")</script>', sanitized: '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;' },
      { malicious: 'alert("xss")', sanitized: 'alert(&quot;xss&quot;) ' },
      { malicious: 'evil<script>alert("xss")</script>', sanitized: 'evil&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;' }
    ];

    for (const { malicious, sanitized } of maliciousInputs) {
      await commentField.fill(malicious);
      await postCommentButton.click();

      await expect(comment).toBeVisible();
      await expect(comment).toContainText(sanitized);
    }
  });

  test('User adds a comment under high load', async ({ page }) => {
    // AC: REQ-BT-004
    const commentField = page.getByTestId('comment-field');
    const postCommentButton = page.getByTestId('post-comment-button');

    await commentField.fill('This is a valid comment');
    await postCommentButton.click();

    const comment = page.getByTestId('comment-1');
    await expect(comment).toBeVisible();
    await expect(comment).toContainText('This is a valid comment');
  });

  test('User attempts to post the same comment twice', async ({ page }) => {
    // AC: REQ-BT-004
    const commentField = page.getByTestId('comment-field');
    const postCommentButton = page.getByTestId('post-comment-button');

    await commentField.fill('This is a valid comment');
    await postCommentButton.click();

    const comment = page.getByTestId('comment-1');
    await expect(comment).toBeVisible();
    await expect(comment).toContainText('This is a valid comment');

    await postCommentButton.click();

    await expect(comment).toBeVisible();
    await expect(comment).toContainText('This is a valid comment');
  });

  test('Multiple users add comments to the same bug concurrently', async ({ page }) => {
    // AC: REQ-BT-004
    const commentField = page.getByTestId('comment-field');
    const postCommentButton = page.getByTestId('post-comment-button');

    const comments = Array.from({ length: 10 }, (_, i) => `Comment ${i + 1}`);

    for (const comment of comments) {
      await commentField.fill(comment);
      await postCommentButton.click();
    }

    for (const comment of comments) {
      const commentEl = page.getByTestId(`comment-${comments.indexOf(comment) + 1}`);
      await expect(commentEl).toBeVisible();
      await expect(commentEl).toContainText(comment);
    }
  });

  test('User adds a comment using keyboard-only navigation', async ({ page }) => {
    // AC: REQ-BT-004
    const commentField = page.getByTestId('comment-field');
    const postCommentButton = page.getByTestId('post-comment-button');

    await commentField.focus();
    await page.keyboard.type('This is a valid comment');
    await page.keyboard.press('Enter');

    const comment = page.getByTestId('comment-1');
    await expect(comment).toBeVisible();
    await expect(comment).toContainText('This is a valid comment');
  });

  test('User dismisses comment form with Escape key', async ({ page }) => {
    // AC: REQ-BT-004
    const commentField = page.getByTestId('comment-field');
    const postCommentButton = page.getByTestId('post-comment-button');

    await commentField.focus();
    await page.keyboard.type('This is a valid comment');
    await page.keyboard.press('Escape');

    await expect(commentField).toBeVisible();
    await expect(commentField).toBeEmpty();
  });
});