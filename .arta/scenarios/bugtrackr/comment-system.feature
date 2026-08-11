Feature: Comment System
  Scenario: Add comment to existing bug
    Given a bug exists with ID 456
    When a user submits a POST request to /api/comments with valid data
    Then the response status code is 201
    And the comment is associated with the bug

  Scenario: Delete comment by author
    Given a user is the author of comment 789
    When they send a DELETE request to /api/comments/789
    Then the response status code is 200
    And the comment is removed from the database

  Scenario: Non-author attempt to delete comment
    Given a user is not the author of comment 789
    When they attempt to DELETE /api/comments/789
    Then the response status code is 403