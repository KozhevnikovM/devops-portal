## MODIFIED Requirements

### Requirement: Environment row action menus remain visible outside the table scroll area

When a user opens an environment row's action menu, every action available to that user SHALL remain visible and operable even when the menu extends beyond the horizontally scrollable environments table or the viewport edge. The open menu SHALL be anchored to the `⋮` button that opened it: it SHALL open directly below the button, or directly above it when there is not enough room below, it SHALL never appear at the viewport centre or anywhere detached from the button, including in the first rendered frame, and it SHALL follow the button when the page or table scrolls or the viewport is resized. The menu SHALL stay anchored even if the page's menu-placement script fails to load. The menu SHALL continue to expose only the actions permitted for that user and environment.

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

#### Scenario: Menu opens next to its trigger
- **WHEN** a user opens an environment row's action menu on a row with enough room below it
- **THEN** the menu's top edge is just below the `⋮` button and the menu is horizontally aligned with it
- **AND** at no point after the click is the menu shown at the viewport centre or detached from the button

#### Scenario: Menu flips above near the bottom edge
- **WHEN** a user opens an environment row's action menu on a row too close to the bottom of the viewport for the menu to fit below
- **THEN** the menu's bottom edge is just above the `⋮` button and the whole menu is on screen

#### Scenario: Menu follows its trigger on scroll and resize
- **WHEN** an environment action menu is open and the user scrolls the page, scrolls the table horizontally or resizes the viewport while the button stays visible
- **THEN** the menu stays adjacent to its button and fully on screen

#### Scenario: Menu stays anchored without the placement script
- **WHEN** the page's menu-placement script is not served and a user opens an environment row's action menu
- **THEN** the menu is still shown next to its `⋮` button, not at the viewport centre
