# Project Skills

This folder keeps project-local operational guidance for the ambulance return LINE bot.
The engineering workflow is provided by the globally installed Matt Pocock skills;
this folder documents the project-specific constraints those skills must respect.

## Included Skills

- `nas-line-push`: NAS, Docker Compose, Selenium, `.env`, and LINE Messaging API rules.
- `karpathy-guidelines`: conservative coding and verification rules for incremental automation work.
- `ambulance-return-workflow`: project-specific NAS bundle, public-duty worker GUI, four-site entry, test, package, and restart workflow.

## Matt Pocock Engineering Skills

The canonical source is `C:\Users\seafl\.codex\skills`. The project does not keep a
local copy of these engineering skills, preventing version drift. When the global skill
set changes, update the list below in this README; do not copy the skill directories into
`project_skills/`.

- `ask-matt`, `implement`, `tdd`, `code-review`, `diagnosing-bugs`
- `grill-me`, `grill-with-docs`, `to-spec`, `to-tickets`, `triage`, `wayfinder`
- `handoff`, `research`, `codebase-design`

### Flow Selection

| Situation | Skill or flow |
| --- | --- |
| Clear, single-session change | `implement` |
| Unclear requirement in this codebase | `grill-with-docs` |
| Unclear requirement without an existing codebase | `grill-me` |
| Large, multi-session work | `grill-with-docs` → `to-spec` → `to-tickets` → `implement` |
| Untriaged external bug, request, or PR | `triage` → `implement` |
| Difficult, intermittent, or performance issue | `diagnosing-bugs` → regression test → `implement` |
| Unclear delivery path | `wayfinder` → `to-spec` → `to-tickets` |
| External trusted-source research | `research` |
| Module boundaries or duplicated responsibility | `codebase-design` |
| Unsure which flow applies | `ask-matt` |
| Session change or durable handoff | `handoff` |

### Examples

- UI small change: `implement` → confirm the public page and expected display → `tdd` for the smallest regression → change the template or component → run page tests → `code-review`.
- NAS or Worker release: `grill-with-docs` or an existing specification → `to-spec` and `to-tickets` for large work → `implement` → unit and integration tests → `code-review` → the project's Release State Ladder for Source, Build, Release, NAS, and Worker verification.

## Usage

Before changing deployment, Selenium automation, LINE webhook behavior, or task
execution, read the relevant `SKILL.md` in this folder. Project rules take priority over
the engineering workflow, including NAS and Worker deployment verification, data safety,
and explicit authorization for commits, releases, deployments, or deletion.

Do not put secrets, tokens, passwords, or real `.env` files in this folder.
