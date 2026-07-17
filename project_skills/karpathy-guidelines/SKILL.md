---
name: karpathy-guidelines
description: Use when non-trivial coding work needs careful assumptions, simple implementation, surgical edits, and verification discipline.
license: MIT
---

# Karpathy Guidelines

Behavioral guidelines adapted from `multica-ai/andrej-karpathy-skills`.

Use this skill for non-trivial coding tasks, especially when the task is ambiguous,
touches existing code, or has meaningful risk of overengineering.

## Think Before Coding

- State assumptions explicitly when ambiguity matters.
- If multiple interpretations exist, present them instead of silently choosing.
- Push back when a simpler or safer approach exists.
- If something is unclear enough to affect correctness, stop and ask.

## Simplicity First

- Implement the minimum code that solves the requested problem.
- Do not add features beyond what was asked.
- Do not add abstractions for single-use code.
- Do not add speculative configurability or extension points.
- If the implementation is much larger than the problem requires, simplify it.

## Surgical Changes

- Touch only the files and lines required by the user's request.
- Do not refactor unrelated code.
- Do not reformat or rewrite adjacent code just because it could be cleaner.
- Match the existing local style unless there is a clear reason not to.
- Remove only unused code introduced by the current change.
- Mention unrelated dead code or issues instead of deleting them.

## Goal-Driven Execution

- Convert broad requests into concrete success criteria.
- For fixes, reproduce or identify the failure before changing behavior when practical.
- Verify with focused tests, checks, or a clear explanation of why verification was not possible.
- Continue iterating until the stated success criteria are met or a concrete blocker is found.

## Tradeoff

These guidelines bias toward caution over speed. For trivial edits, simple one-liners,
or clearly mechanical tasks, use judgment and avoid unnecessary ceremony.
