# Unreleased changes

Each change a pull request makes is one file in `changes/unreleased/` (issue #122, W-40), so two pull requests never conflict on CHANGELOG.md. The release cut assembles them into CHANGELOG.md.

## The name

`<n>-<slug>.<kind>.md`, for example `014-ledger-fragments.changed.md`:

- `<n>`: the change's place among the changes of its kind. Take the number after the highest in the folder, zero-padded to three digits. Two pull requests that take the same number do not conflict: their slugs differ, and the slug breaks the tie.
- `<slug>`: a few lowercase words joined by hyphens.
- `<kind>`: `added`, `changed` or `fixed`, the section the change is listed under.

## The text

The bullet's text, without its leading `- `. A sub-list or a second paragraph starts at the left margin; the cut indents it under the bullet. Write links as they will read in CHANGELOG.md, from the repository's root (`docs/RECOVERY.md`, not `../../docs/RECOVERY.md`): the docs check reads them from there.

## The release cut

`uv run python scripts/release_notes.py --cut X.Y.Z` writes the new `## [X.Y.Z] - <date>` section right below `[Unreleased]`: `### Added`, `### Changed` and `### Fixed`, each in number order. Then it deletes the fragments. A released section is never edited after its tag (`check_released_section_frozen`, I-089).
