# Radicale Manager TODO

This file is the working checklist for Phase 2 and Phase 3. Keep items small enough that a single coding pass can implement and verify them.

## Current Focus

- [x] Saved connection profiles
- [x] Address book discovery and cached counts
- [x] Contact browse/detail/edit/delete
- [x] VCF import from the main menu
- [x] Contact list sort and live filter
- [x] Contact operations polish
- [x] Address book management
- [x] Bulk operations
- [x] Duplicate detection and contact quality engine

## Phase 2 - Contact Operations

Goal: turn the app from a CardDAV browser into a complete contact management platform.

### Connection Management

- [x] Multiple saved connection profiles
- [x] Connection deletion
- [x] Connection testing
- [x] Connection health/status
- [x] Connection editing UI
- [x] Credential encryption verification and setup guidance
- [x] Profile export/import
- [x] Connection grouping
- [x] Connection tags

### Address Book Management

- [x] Create address book
- [x] Rename address book
- [x] Delete address book
- [x] Refresh address books
- [x] Address book statistics
- [x] Contact counts
- [x] Last modified information

### Contact Management

- [x] Summary detail view
- [x] Advanced detail view
- [x] Raw vCard view
- [x] Basic field editing
- [x] Raw vCard editing
- [x] Preserve unknown fields during form-based editing
- [x] Multi-value phone editing
- [x] Multi-value email editing
- [x] Copy contact
- [x] Move contact
- [x] Delete contact
- [x] Duplicate contact action

### Bulk Operations

- [x] Multi-select contacts in list
- [x] Bulk delete
- [x] Bulk move
- [x] Bulk copy
- [x] Bulk export

### Import / Export

- [x] Single VCF import
- [x] Multi-contact VCF import
- [x] Address book import
- [x] Single contact export
- [x] Address book export
- [x] Entire connection export
- [x] Backup ZIP export with AddressBooks, Contacts, and Metadata folders

### Contact Search

- [x] Address-book live filter
- [x] Sort by contact list columns
- [x] Global contact view across all connections and books
- [x] Global search across all connections and books
- [x] Search by name, email, phone, and company
- [x] Filters by connection and address book
- [x] Filters for has email and has phone
- [x] Recently modified filter

### Dashboard Improvements

- [x] Total connections
- [x] Total address books
- [x] Total contacts
- [x] Recent imports
- [x] Recent exports
- [x] Recent moves
- [x] Recent deletes
- [x] App health
- [x] Connection health rollup
- [x] Backup health

## Phase 3 - Contact Intelligence

Goal: become a contact quality and migration platform.

### Contact Quality Engine

- [x] Add analysis service that can scan cached or live contacts
- [ ] Generate health score
- [x] Detect missing name
- [x] Detect missing email
- [x] Detect missing phone
- [ ] Detect empty contacts
- [ ] Detect deprecated fields
- [ ] Show warnings and recommendations without auto-changing contacts

### Duplicate Detection

- [x] Normalize comparison values for names, phones, and emails
- [x] Detect possible duplicates by email
- [x] Detect possible duplicates by phone
- [x] Detect possible duplicates by exact name
- [ ] Detect possible duplicates by similar name
- [ ] Assign match score
- [x] Show duplicate review page
- [ ] Support ignore
- [ ] Support manual review
- [ ] Support merge recommendation
- [ ] Support approved merge

### Contact Normalization

- [ ] Detect platform-specific phone labels
- [ ] Recommend normalized labels
- [ ] Show current value, recommended value, and impact
- [ ] Require user approval before changes

### Migration Assistant

- [ ] Detect Apple-specific fields
- [ ] Detect Outlook fields
- [ ] Detect Google fields
- [ ] Detect Nextcloud fields
- [ ] Summarize source platform hints
- [ ] Report compatibility issues

### Audit & History

- [ ] Add event log table
- [ ] Track contact created
- [ ] Track contact edited
- [ ] Track contact moved
- [ ] Track contact deleted
- [ ] Show timestamp, action, and user/system source

### Recovery

- [ ] Soft delete contacts
- [ ] Recycle bin page
- [ ] Restore contact
- [ ] Purge contact
- [ ] 30-day retention policy

### Scheduled Backups

- [ ] Daily backup option
- [ ] Weekly backup option
- [ ] Monthly backup option
- [ ] Store metadata
- [ ] Store checksums
- [ ] Store backup statistics

### System Monitoring

- [x] Version display
- [x] Git commit config support
- [x] Container uptime in health endpoint
- [x] Database readiness check
- [x] Health endpoint
- [x] Route explorer
- [x] Feature registry
- [ ] System page that combines version, uptime, database status, health, routes, and features

## Suggested Next Build Slices

1. Connection editing UI
2. Preserve unknown fields during form-based contact editing
3. Multi-value phone and email editing
4. Multi-select contact list with bulk delete/export
5. Global search page
6. Lightweight duplicate report page
7. Event log table for imports, exports, moves, deletes, and edits

## Definition Of Done

- The feature has a visible entry point in navigation or the relevant workflow.
- The feature works on desktop and phone widths.
- Existing unit tests pass.
- New risky parsing, storage, or merge behavior has focused tests.
- Dashboard or feature registry status is updated when appropriate.
