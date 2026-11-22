# UI & UX Design System

**English** | [简体中文](./UI_UX_CN.md)

Yami Learning Companion spans two complementary surfaces with distinct goals:
1. **Guardian Web Workspace (`/guardian`)** — calm, scannable, low-friction daily oversight for parents.
2. **ESP-Mosaico S31 (480×480 AMOLED)** — warm, playful, distraction-free voice & pet companion on the child's desk.

---

## 1. Guardian Web — "Morning Porcelain" 3-Column Workspace

### Design Tokens

| Token | Value | Usage |
|---|---|---|
| `--md-sys-color-surface` | `#FFFFFF` | Primary workspace canvas |
| `--md-sys-color-surface-container-low` | `#F7F9FC` | Left navigation rail & subtle panels |
| `--md-sys-color-surface-container` | `#F4F6FA` | Nested cards, KPI boxes, praise tiles |
| `--md-sys-color-outline-variant` | `#E3E8EF` | Crisp `1px` borders & column dividers |
| `--md-sys-color-primary` | `#1A73E8` | Primary actions, active nav, today ring |
| `--md-sys-color-primary-container` | `#E8F0FE` | Active nav pill, info badges |
| `--md-sys-color-secondary` | `#137333` / `#E6F4EA` | Completed homework, verified praise |
| `--md-sys-color-tertiary` | `#B06000` / `#FEF7E0` | Pending parent confirmations & todos |
| `--md-sys-color-on-surface` | `#1F1F1F` / `#5F6368` | High-contrast body & secondary text (WCAG AA ≥ 4.5:1) |
| **Typography** | `Plus Jakarta Sans`, `Noto Sans SC` | Clean geometric Latin + legible CJK |
| **Iconography** | 24×24 outline SVG (zero UI-chrome emojis) | Consistent visual rhythm |

### Responsive Layout Topology
- **Desktop (`≥ 1180px`)**: 3-column Google Workspace layout (`224px` left navigation rail + flexible main content + `368px` persistent right context rail showing live **Growth Calendar** & **Teacher Praise Board**).
- **Tablet / Mobile (`< 1180px`)**: Single-column main view with accessible bottom navigation bar (`role="tablist"`).
- **Top-Right Utilities**: Student switcher popover (`popover="auto"`), **Account** modal (`<dialog id="account-dialog">`), and **Help** modal (`<dialog id="help-dialog">` housing the Mosaico hardware key guide).

### 4-Tab Information Architecture

1. **Today (`今日`)** — Strict 5-section order:
   1. **Today's Homework** (1-click mark complete / undo, manual add disclosure; excludes school notices)
   2. **Todos** (signed school notices & items needing parent submission confirmation)
   3. **Review & Preview** (unified card for next-day consolidation + preview tasks)
   4. **Custom Tasks** (full lifecycle: create with `daily / weekdays / once` cadence, check-in streak, filter `All / Active / Paused`, pause/resume, edit, delete)
   5. **Today's Teacher Feedback** (class-group praise with 1-click verify)
2. **Growth (`成长`)** — Consolidated long-term view (no redundant flat tables):
   1. **Growth Calendar**: Monthly heatmap (`YYYY-MM`) with explicit **Today (`今`)** highlight, 1-click *Back to Today*, and click-any-day detail + *View day's conversations* shortcut
   2. **Teacher Praise & Classroom Performance Board**: KPI strip (Total / This Month / Top Subject), subject filter chips, detailed cards (subject, teacher, date, source, quote, parent note), and inline manual praise logger
   3. **Textbook Curriculum & Knowledge Mastery**: Synced with authoritative grade/edition textbook catalogs + unlocked Method Cards
   4. **Habit Auto-Stats**: Automatically aggregated from 7-day homework completion + Custom Task streaks
   5. **Emotion Barometer**: Passive non-clinical mood indicators + parenting communication tip
   6. **Learning Memory & Voice Conversations**: Plain-language transparent memory list (zero DB jargon) + timezone-aware per-day conversation filter (`Today / Yesterday / All` + date picker)
3. **Skills (`技能`)**: Per-student enable/pause lifecycle cards + quick textbook voice-dictation launcher.
4. **Settings (`设置`)**: Student profiles & per-subject textbooks, 6-digit Mosaico device pairing, DingTalk integration (`AppId / AppSecret / Class Group ID / Family Webhook` + 1-click `Sync DingTalk Now`), and 1:1 Polaroid Pet House sync.

---

## 2. ESP-Mosaico S31 (480×480 AMOLED) — Voice & Pet House UX

### Interaction & Copywriting Principles
- **Paper-First, Screen-Light**: The child writes on real paper; Mosaico speaks concise 1-sentence guidance and avoids distracting looping animations during study.
- **Process Praise, Never Labels**: Describe concrete actions (*"You drew a diagram first"*) rather than traits (*"You're so smart"*).
- **Socratic Escalation**: Clarify problem → child explains first step → 1 hint → visual cue → similar example → back to paper.

### Minimalist 2-Key Hardware Controls (Hardware Mutual-Exclusion Lock)
- **Long-press Right (`≥ 0.35s`)**: Push-to-talk (or hands-free wake word).
- **Long-press Left (`≥ 0.45s`)**: Enter / exit *Yami Pet House*.
- **Short-press Left / Right**: Navigate homework items or select & feed the 3 species-specific food capsules in Pet House.

### Yami Pet House Gamification Loop
- **9 Frame-by-Frame Animated Pets** (4×4 sprite atlases) across 3 families (Fantasy Spirits, Cute Companions, Cosmic Toys) with 4 upgradeable habitat levels.
- **Balanced Reward Economy**: Completing study tasks earns Pet Coins (`+20` coins/task); feeding & petting boost Hunger and Happiness stars (5-star display).
- **Gentle Responsibility Loop**: 3 days unattended → hungry/sad state; 7 days unattended → pet temporarily runs away (empty nest scene) and can be warmly recalled for `100` coins (~5 study tasks).
