## ADDED Requirements

### Requirement: Row action menu tests synchronize on menu readiness

A browser test that opens a row action menu and then interacts with or asserts on its contents SHALL first wait for the menu to be both open and visible, applying the same readiness condition whichever way the menu was opened (pointer click or keyboard activation), so the controller's hide-while-positioning window cannot silently swallow a focus or input action. A focus assertion tied to opening the menu SHALL wait for the focus condition to hold rather than sampling the focused element exactly once immediately after the interaction. A browser test that fails only intermittently on unchanged behavior SHALL be treated as a suite defect: it SHALL be repaired by correcting its synchronization or by fixing the product defect it exposes, and SHALL NOT be masked by disabling the test, weakening its assertions, or retrying it unconditionally.

#### Scenario: Menu opened by keyboard is interacted with only once visible
- **WHEN** a browser test opens a row action menu via the keyboard and then interacts with a control inside it
- **THEN** the interaction and assertions happen only after the menu is both open and visible — the same readiness condition the click-opened path applies — so controller-held invisibility cannot silently swallow a focus or input action

#### Scenario: Focus containment after opening is condition-based
- **WHEN** a browser test asserts that focus landed inside a just-opened row action menu
- **THEN** the assertion waits for the menu to contain the focused element rather than sampling once, and a genuine focus defect still fails within the wait's timeout

#### Scenario: An intermittently failing test is repaired, not masked
- **WHEN** a browser test fails intermittently on pull requests that do not change the behavior it covers
- **THEN** the repair keeps every existing assertion and scenario coverage, removes the race rather than papering over it with fixed delays, and the test remains enabled and selected by the required-mode suite

#### Scenario: A flakiness repair is proven stable before merging
- **WHEN** an intermittently failing browser test is repaired
- **THEN** the repair ships only after the repaired test passes at least 50 consecutive Chromium runs and the full browser suite passes in required mode
