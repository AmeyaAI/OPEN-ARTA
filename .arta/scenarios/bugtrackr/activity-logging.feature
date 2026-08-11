Feature: Activity Logging
  Scenario: Create bug triggers activity log
    Given a user is authenticated
    When they create a new bug
    Then an activity log entry is created
    And the response includes activity ID

  Scenario: Update bug status triggers log
    Given a bug exists with ID 123
    When a user updates its status
    Then a new activity log entry is created
    And the activity type is "status_change"

  Scenario: List activities for specific bug
    Given a bug has multiple activities
    When a user requests /api/activities?bugId=123
    Then the response returns all activities for that bug
    And status code is 200