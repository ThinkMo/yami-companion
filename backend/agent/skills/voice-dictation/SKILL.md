---
name: voice-dictation
description: Use when the child or parent requests Chinese or English dictation by unit, lesson, vocabulary, or word list, or issues playback controls during an active dictation session.
---

# Voice Dictation (教材语音听写)

## Overview

Conduct hands-free, paper-and-pencil Chinese and English dictation directly from verified grade-and-textbook curriculum materials without ever revealing character shapes or spelling on screen.

## When to Use

- Child or parent says a natural-language dictation request (e.g., `听写第3单元第4课组词`, `听写英语Unit 3单词`).
- An active dictation session is running and the child says playback commands (`再读一遍`, `写好了`, `下一个`, `跳过`, `上一个`).

**When NOT to use:**
- General homework listing, math tutoring, or lesson preview (use `homework-companion`).
- Casual chat (use `chat-companion`).

## Core Rules

1. **Zero Spelling Spoilers**: Read ONLY the target word and its brief contextual phrase (e.g., `均匀，铺得很均匀的均匀`). Never describe stroke order, radicals, or letter-by-letter spelling during dictation.
2. **Verified Curriculum Only**: Always call `start_curriculum_dictation(utterance)`. If the requested unit/lesson is missing from the curriculum bank, say so honestly and invite the parent to add words — NEVER invent or hallucinate vocabulary lists.
3. **Pure Voice Pace Control**: During an active session, map child utterances directly via `control_dictation(command)` without extra chatter.
4. **Automatic Next-Day Review**: Any skipped or self-checked missed words are automatically queued into the next-day review bank — remind the child gently without shaming.

## Quick Reference

| Utterance Pattern | Tool to Call |
|---|---|
| `听写第3单元第4课组词` / `听写英语Unit 3` | `start_curriculum_dictation` |
| `再读一遍` / `重读` | `control_dictation("再读一遍")` |
| `写好了` / `下一个` / `下一词` | `control_dictation("下一个")` |
| `跳过` / `不会` | `control_dictation("跳过")` |
| `上一个` | `control_dictation("上一个")` |

## Common Mistakes

- Making up vocabulary words when a unit is not yet in the database.
- Adding long motivational speeches between dictation words (breaks writing rhythm).
- Printing or spelling out the word being dictated.
