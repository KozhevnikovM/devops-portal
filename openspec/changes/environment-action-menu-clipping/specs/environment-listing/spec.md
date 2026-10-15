## ADDED Requirements

### Requirement: Environment row action menus remain visible outside the table scroll area

When a user opens an environment row's action menu, every action available to that user SHALL remain visible and operable even when the menu extends beyond the horizontally scrollable environments table or the viewport edge. The menu SHALL continue to expose only the actions permitted for that user and environment.

#### Scenario: Menu opens in the Mine list
- **WHEN** a user who may manage an environment opens its action menu on `/environments?filter=mine`
- **THEN** the complete menu is visible and its available actions can be selected

#### Scenario: Menu opens on a row near a scroll-area edge
- **WHEN** a user opens an environment action menu on a row near any edge of the horizontally scrollable table
- **THEN** the menu remains visible and operable without requiring the user to scroll the table to reveal clipped menu content

#### Scenario: Menu is taller than a short viewport
- **WHEN** a user opens an environment action menu whose full height exceeds the available viewport height
- **THEN** the menu is bounded to the available viewport height and scrolls internally so every permitted action, including Release, remains reachable

#### Scenario: Menu keeps permission gating
- **WHEN** a user opens an environment row action menu
- **THEN** the menu contains only actions that user is permitted to perform for that environment
