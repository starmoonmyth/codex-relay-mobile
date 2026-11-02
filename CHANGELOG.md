# Changelog

## 1.3.7

- Read native Codex, Claude and Grok subscription usage separately from compatible API provider balances and local token totals.
- Show available quota windows, reset times and credits, with account-scoped caching and Claude rate-limit backoff.
- Add processing and completed-unread indicators to the chat list; clear unread only when the final reply is visible near the bottom of a foreground chat.
- Preserve device-local read markers across refreshes and keep transcript reads separate from native conversation writes.

## 1.3.6

- Add a mobile usage and balance panel for the selected chat, including cumulative tokens, cache reads and the latest model call or Grok task.
- Read compatible provider balances, quotas and actual daily deductions using locally configured API keys, with a one-minute query cache.
- Identify CC Switch credential sources, distinguish recorded account limits from provider balances, and label expired snapshots or unavailable data.

## 1.3.5

- Add Grok CLI text sessions, per-chat model selection, original-session resume and verified CLI completion receipts.
- Protect running Grok sessions from overlapping submissions and keep CLI execution supervision independent of the web service.
- Add distinct status colors, SVG icons, readable labels, reduced motion support and mobile layouts down to 320 pixels.
- Show queued submissions, unanswered questions, uncertain delivery and disconnected states instead of retaining an older completed badge.

## 1.3.4

- Add Codex model selection to the shared mobile model dialog, using the local Codex model catalog and custom model IDs.
- Synchronize the original desktop chat's next-turn model through its owner and confirm it by reading settings back.
- Check the desktop model before submitting a selected-model task and require confirmation if it has changed.
- Keep existing queued tasks subject to the original chat's execution-time model settings.

## 1.3.3

- Add per-chat Claude model selection on mobile, including custom provider model IDs.
- Pass an explicit model to every Claude CLI run and preserve it when retrying the same request.
- Display requested models on sent messages and provider-reported models on Claude replies.
- Reuse the last recorded response model when no preference is saved; require a selection when no model is known.

## 1.3.2

- Fix Claude Code receipt matching when mobile image attachment instructions are appended to the CLI prompt.
- Mark completed Claude Code tasks as completed in the mobile view after the assistant response is written to the JSONL transcript.

## 1.3.1

- Add an optional Claude Code provider that reads local JSONL sessions and resumes them with the official CLI.
- Add a local provider selector for Codex, Claude Code, or both, with namespaced Claude session IDs.
- Pass uploaded images to Claude Code through a controlled attachment directory and document the Desktop IPC limitation.

## 1.2.3

- Publish Mac Apple Silicon and Windows desktop packages with a Chinese mobile PWA.
- Continue existing Codex chats with text, multiple images, draft recovery, and delivery receipts.
- Submit structured question replies through the desktop chat owner, including recovery of older queued replies.
- Match receipts across trailing newline differences and retain message and turn associations beyond recent history.
- Update notification workers, synchronize existing subscriptions, and track provider, device, and display receipts separately.
- Deduplicate task notifications, merge reminders by chat, and automatically dismiss page reminders.
- Adapt the chat layout to mobile visual viewport height and offset while preserving reading position.
- Start with an empty device list so each installation discovers and configures its own Tailscale address.
