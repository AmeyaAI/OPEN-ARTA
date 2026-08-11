Feature: API Edge Cases
  Scenario: Request with invalid UUID format
    Given a user sends a request to /api/bugs/invalid-uuid
    Then the response status code is 400

  Scenario: Excessively long string inputs
    Given a user submits a POST request with a 10,000 character description
    When they send it to /api/bugs
    Then the response status code is 413

  Scenario: Concurrent updates to same bug
    Given two users attempt to update the same bug simultaneously
    Then the second update receives a 409 Conflict response