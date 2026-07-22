# Media Language Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prefer the highest available video quality with English audio and Russian subtitles across Sonarr, Radarr, and TG-arr, while keeping downloads on D: and the permanent media library on G:.

**Architecture:** Sonarr and Radarr remain the source of truth for automatic acquisition through quality profiles and custom formats. TG-arr mirrors the same language preference when ranking manual Prowlarr results. Existing media, root folders, and automatic-search state remain unchanged.

**Tech Stack:** Sonarr v4 API, Radarr v6 API, Python 3.12, pytest, Docker Compose.

## Global Constraints

- Keep qBittorrent staging at `D:\incompleted` and `D:\completed`.
- Keep 2160p Remux as the cutoff with 1080p fallback.
- Prefer English audio and Russian subtitles; penalize Russian dub only when English is absent.
- Keep new bot additions on the existing first root folder on G:; H: remains a reserve root.
- Do not bulk-move existing media or trigger automatic searches.
- Back up ARR profiles, custom formats, and bot database before changes.

### Task 1: TG-arr ranking

- [ ] Add failing language-policy tests.
- [ ] Confirm RED, implement compiled marker scoring, confirm GREEN.
- [ ] Run the full test suite and Ruff.

### Task 2: ARR policy

- [ ] Verify free space and save timestamped API backups.
- [ ] Upsert English Audio and make Russian Dub match only without English.
- [ ] Set scores: English `250`, Russian subtitles `250`, dub without English `-1000`, minimum `0`, cutoff `500`.
- [ ] Read resources back and compare persisted values.

### Task 3: Deployment and live verification

- [ ] Confirm bot root preferences remain unset so the existing first G: roots stay selected.
- [ ] Build and deploy under compose project `tgarr`.
- [ ] Verify health, preferences, ARR state, G: selection, and unchanged D: staging paths.
