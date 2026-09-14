---
type: overview
tags: [training, health, fitness]
created: 2026-09-14
updated: 2026-09-14
related:
  - "[[about-me]]"
---

# Training

Hub for Spencer's training. The personal trainer reads this page and
everything linked to it before coaching, and records goals, constraints and
reviews here. Raw numbers stay in the database; this part of the graph holds
the judgement.

## Current focus

Not recorded yet. The trainer fills this in once Spencer describes the
current block, such as base building, a strength phase, or race preparation.

## Goals

Goal pages tagged `training` link here. None recorded yet.

```dataview
TABLE WITHOUT ID file.link AS Goal, updated AS Updated
FROM "wiki"
WHERE type = "goal" AND contains(tags, "training")
SORT updated DESC
```

## Constraints and injuries

Concept pages tagged `training` for injuries, niggles, schedule limits and
exercise preferences. None recorded yet.

```dataview
LIST
FROM "wiki"
WHERE type = "concept" AND contains(tags, "training")
SORT file.name ASC
```

## Reviews

Weekly and block reviews are `source` pages tagged `training-review`.

```dataview
TABLE WITHOUT ID file.link AS Review, created AS Date
FROM "wiki"
WHERE type = "source" AND contains(tags, "training-review")
SORT created DESC
```

## Data behind this page

- Fitbit Air, via the Google Health API, and Strava sync twice a day into
  `health.db` on the server.
- Gym sessions and weigh-ins are logged from Telegram with `infra/trainer.py`.
- `python3 infra/trainer.py summary --days 14` prints the combined view.
