# The Logitech mouse haptics switch ships visible, and its Logi Options+ plugin stays in `packages/`

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-28

## Decision

The Logitech mouse haptics switch ships visible on Settings > Notifications > Sound, off by default and not behind Feature previews; the Logi Options+ plugin it sends alerts to stays in this repository under `packages/kirocrew-logi-plugin/`, and the plugin's CI workflow lands in its own pull request after the one that adds the switch.

## Why

- The pull request that adds the switch builds what the request asks for, and nothing is sent to the plugin until the user turns the switch on.
- The request places the switch on the Sound page.
- Until that workflow lands, the plugin's tests run by hand: on Ubuntu 24.04, at c030b903e2, its 58 tests passed twice.
- Options not chosen, both raised in review on the pull request: keeping the switch behind Feature previews or a developer-preview label until a packed plugin ships, and moving the plugin out of this repository.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/16349#issuecomment-7238273915 -- Bolin Chen's decision on the pull request that adds this entry (rulings 2 and 3).
- https://github.com/kirodotdev/KiroCrew/issues/16625 -- the request.
- https://github.com/kirodotdev/KiroCrew/issues/20229 -- the follow-up that carries the plugin's CI workflow.
