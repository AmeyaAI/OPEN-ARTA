Feature: Data Integrity
  Scenario: Create bug with valid foreign key
    Given a valid project ID exists
    When a user creates a bug with this project ID
    Then the response status code is 201
    And the bug is associated with the project

  Scenario: Create bug with invalid foreign key
    Given a non-existent project ID
    When a user attempts to create a bug with this ID
    Then the response status code is 400

  Scenario: Delete project with dependent bugs
    Given there are bugs associated with project 789
    When a user attempts to delete project 789
    Then the response status code is 409