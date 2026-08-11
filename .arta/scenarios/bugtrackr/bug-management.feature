Feature: Bug Management
  Scenario: Create bug with valid data
    Given a user is authenticated
    When they submit a POST request to /api/bugs with valid data
    Then the response status code is 201
    And the bug is created in the database

  Scenario: Create bug with missing required fields
    Given a user is authenticated
    When they submit a POST request to /api/bugs with missing required fields
    Then the response status code is 400

  Scenario: Update bug status by authorized user
    Given a user is authorized to update bugs
    When they send a PATCH request to /api/bugs/123/status
    Then the response status code is 200

  Scenario: Update bug status by unauthorized user
    Given a user is unauthorized
    When they attempt to update a bug status
    Then the response status code is 403