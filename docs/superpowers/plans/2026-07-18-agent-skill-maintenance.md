# Agent And Skill Maintenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align project, cloud, and runtime skill guidance while disabling unavailable Hivemind skills and removing stale agent metadata.

**Architecture:** Treat the cloud SKILL library as the custom-skill distribution source and install flat runtime copies through its transactional installer. Keep ambulance-specific invariants in `AGENTS.md` and `project_skills`, while vendored third-party Superpowers content records an exact upstream commit.

**Tech Stack:** PowerShell, Agent Skills Markdown/YAML, Git, Python unittest.

## Global Constraints

- Do not modify public-duty runtime source or user-owned working-tree changes.
- Do not edit generated `UPDATE\NAS包`.
- Do not modify OpenAI-managed `.system` skills.
- Do not deploy or restart NAS.

---

### Task 1: Add failing policy checks

**Files:**
- Modify: `I:\我的雲端硬碟\專案\SKILL\tests\install-skills-from-cloud.Tests.ps1`
- Create: a temporary read-only parity check executed from PowerShell

**Interfaces:**
- Consumes: cloud skill folders, runtime skill folders, repo `project_skills`.
- Produces: deterministic failures for disabled runtime copies, stale metadata, and content drift.

- [ ] Run installer tests and record the baseline.
- [ ] Run `-VerifyOnly` and confirm it rejects installed disabled skills.
- [ ] Run metadata/parity assertions and confirm the known stale copies fail.

### Task 2: Repair cloud skill sources

**Files:**
- Modify: `I:\我的雲端硬碟\專案\SKILL\01_custom\04_general-affairs\ambulance-return-workflow\`
- Modify: `I:\我的雲端硬碟\專案\SKILL\01_custom\02_coding\karpathy-guidelines\`
- Modify: `I:\我的雲端硬碟\專案\SKILL\01_custom\02_coding\hivemind-*\agents\openai.yaml`
- Modify: `I:\我的雲端硬碟\專案\SKILL\01_custom\05_notification-automation\nas-line-push\`
- Verify: `I:\我的雲端硬碟\專案\SKILL\DISABLED_SKILLS.txt`

**Interfaces:**
- Consumes: approved project workflow and current OpenAI metadata schema.
- Produces: canonical cloud sources ready for transactional installation.

- [ ] Update only the skills identified by the audit.
- [ ] Validate frontmatter names and metadata.
- [ ] Run installer unit tests.

### Task 3: Update project guidance and vendored Superpowers

**Files:**
- Modify: `AGENTS.md`
- Modify: `.gitignore`
- Modify: `project_skills/README.md`
- Modify: `project_skills/ambulance-return-workflow/`
- Modify: `project_skills/karpathy-guidelines/`
- Modify: `project_skills/nas-line-push/`
- Modify: `project_skills/superpowers/`

**Interfaces:**
- Consumes: canonical cloud custom skills and upstream Superpowers commit.
- Produces: repo guidance matching runtime boundaries.

- [ ] Update Superpowers from the exact upstream commit and record it in `SOURCE.md`.
- [ ] Remove stale SSH public-key material from `AGENTS.md`.
- [ ] Ignore local `.codex/` without deleting it.
- [ ] Verify repo/cloud custom-skill parity.

### Task 4: Install and verify runtime skills

**Files:**
- Modify through installer: `C:\Users\User\.codex\skills`

**Interfaces:**
- Consumes: canonical cloud library.
- Produces: flat runtime skills with disabled Hivemind copies absent.

- [ ] Run the cloud installer normally.
- [ ] Run `-VerifyOnly`.
- [ ] Confirm disabled skill folders are absent.
- [ ] Confirm custom skill fingerprints equal cloud sources.

### Task 5: Final verification

**Files:**
- Verify all changed guidance and tests.

**Interfaces:**
- Consumes: completed repo, cloud, and runtime state.
- Produces: evidence-backed completion report.

- [ ] Run `git diff --check`.
- [ ] Run complete repo tests.
- [ ] Run cloud installer tests and skill metadata checks.
- [ ] Check live NAS `/status`, admin page, and restricted SSH without changing NAS state.
- [ ] Report whether SSH authorization remains an external blocker and explicitly state that worker/NAS were not restarted.
