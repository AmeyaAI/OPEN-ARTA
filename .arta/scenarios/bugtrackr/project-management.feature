Feature: Project Management
  Scenario: Create project with unique name
    Given a user has permission to create projects
    When they submit a POST request to /api/projects with a unique name
    Then the response status code is 201

  Scenario: Create project with duplicate name
    Given a project already exists with the same name
    When a user attempts to create another project with the same name
    Then the response status code is 409

  Scenario: List projects with pagination
    Given there are multiple projects in the system
    When a user requests /api/projects with page=2&limit=10
    Then the response returns the second page of projects
    And the status code is 200