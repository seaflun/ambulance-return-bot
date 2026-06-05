# Project Skills

This folder keeps project-local skill guidance for the ambulance return LINE bot.
These files are copied from the cloud skill store so future maintenance can follow
the same operational rules without relying on global Codex skill installation.

## Included Skills

- `nas-line-push`: NAS, Docker Compose, Selenium, `.env`, and LINE Messaging API rules.
- `karpathy-guidelines`: conservative coding and verification rules for incremental automation work.

## Usage

Before changing deployment, Selenium automation, LINE webhook behavior, or task
execution, read the relevant `SKILL.md` in this folder.

Do not put secrets, tokens, passwords, or real `.env` files in this folder.
