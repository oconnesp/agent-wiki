---
type: overview
tags: [training, health, fitness]
created: 2026-09-14
updated: 2026-09-14
related:
  - "[[about-me]]"
  - "[[sub-20-5k]]"
---

# Training

Hub for Spencer's training. The personal trainer reads this page and
everything linked to it before coaching, and records goals, constraints and
reviews here. Raw numbers stay in the database; this part of the graph holds
the judgement.

## Current focus

Running: sub-20 min 5k build. 3 runs/week (intervals + long easy + moderate) alongside 3 gym sessions (2 upper, 1 lower). See [[sub-20-5k]] for the active plan.

## Goals

- [[sub-20-5k]] — break 20 min for 5k; current estimated ~27 min, structured 3×/week run programme

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

- Fitbit Air syncs twice a day through the Google Health API into
  `health.db` on the server: sleep, resting heart rate, HRV, steps, and
  workouts including runs.
- Gym sessions and weigh-ins are logged from Telegram with `infra/trainer.py`.
- `python3 infra/trainer.py summary --days 14` prints the combined view.
