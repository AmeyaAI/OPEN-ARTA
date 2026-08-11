Feature: User Roles and Permissions
  Scenario: Admin create user
    Given a user has admin role
    When they submit a POST request to /api/users
    Then the response status code is 201

  Scenario: Tester attempt to delete user
    Given a user has tester role
    When they attempt to DELETE /api/users/123
    Then the response status code is 403

  Scenario: Developer create bug for assigned project
    Given a developer is authenticated
    When they submit a POST request to /api/bugs with valid project ID
    Then the response status code is 201

  Scenario: Developer access admin-only endpoint
    Given a developer is authenticated
    When they request /api/admin/settings
    Then the response status code is 403