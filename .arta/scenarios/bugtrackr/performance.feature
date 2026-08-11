Feature: Performance Requirements
  Scenario: Response time under load
    Given the system is under normal load
    When a user sends 100 concurrent requests to /api/bugs
    Then each response has status code 200 or 201
    And average response time is less than 500ms

  Scenario: High availability
    Given the system runs for 24 hours under stress
    When monitored every 5 minutes
    Then system availability is at least 99.9%