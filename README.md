# pull-shark-lab

This repo exists to test GitHub automation: a script opens and squash-merges one
small pull request at a time to exercise the PR workflow end to end.

Every commit appends one line to [`activity.log`](./activity.log). Nothing here is
production code and nothing here is meant to be useful to anyone else.

## Why

GitHub awards contribution achievements (Pull Shark, Pair Extraordinaire) based on
merged pull request counts. Reaching the higher tiers through normal work takes
years. This repo generates that volume deliberately, on a first-party repo, paced
under GitHub's published secondary rate limits.

## How

`pull_shark.py` — see the header docstring for flags. It reads its token from
`gh auth token` at runtime; no token is stored in this repo.

## Rate-limit note

GitHub allows roughly 500 content-generating requests per hour. One PR cycle costs
four. This caps the tool near 125 PRs/hour, and it backs off rather than hammering.
