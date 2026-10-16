#!/usr/bin/env bash
# Last step of every bot workflow: add this run to botlog.json (the site's Admin tab) and save it.
# Never fails the run: the admin log is a convenience.
set +e
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
for try in 1 2 3 4; do
  git fetch -q origin main && git reset -q --hard origin/main
  python3 scripts/botlog.py record || exit 0
  git add botlog.json
  git diff --cached --quiet && exit 0
  git commit -q -m "Admin log: ${BOT:-bot} run" && git push -q && exit 0
  echo "Push raced with another save; retrying ($try)"
  sleep $((try * 8))
done
exit 0
