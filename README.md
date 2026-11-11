# makefun-werewolf-agent-01

[![make.fun · werewolf-agent-01](https://make.fun/badge/werewolf-agent-01.svg)](https://make.fun/bounty/werewolf-agent-01?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)
[![rank](https://make.fun/badge/werewolf-agent-01/agniakhrli/makefun-werewolf-agent-01/rank.svg)](https://make.fun/bounty/werewolf-agent-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)
[![reward](https://make.fun/badge/werewolf-agent-01/agniakhrli/makefun-werewolf-agent-01/reward.svg)](https://make.fun/bounty/werewolf-agent-01?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)

# werewolf-agent-01

A deterministic, model-free Werewolf player for the make.fun competition
`werewolf-agent-01`. Standard library only, no model calls, no API keys.

## How it plays

One seat in 30 games of 8-player Werewolf against house agents, through the
env at `MAKEFUN_ENV_URL`. On every decision it rebuilds the game's state
from the view log — revealed roles, deaths, public votes, speeches and
claims — and scores each living seat's suspicion from vote history (voting
for a revealed villager is suspicious; voting for a revealed wolf clears),
from claims contradicted by revealed roles (a false seer claim is
near-proof of a wolf), and from speech signals.

- **Werewolf:** night kills need unanimity, so it always matches the
  partner's pick when they voted first; when voting first it names the best
  target (claimed seer, then whoever accuses it or its partner, then town
  leaders; never tomorrow's exile). By day it blends in: it echoes the
  village's leading suspect with a real log fact, votes with the plurality,
  and never votes its partner.
- **Seer:** inspects the most suspicious uninspected seat; once a wolf is
  found it claims promptly, with the night and the evidence.
- **Doctor:** protects the claimed seer, else the player most likely to be
  the night's target; never itself, never last night's seat.
- **Villager:** quotes vote history and pushes the single highest-suspicion
  seat, so the village does not split its vote.

Every decision is a pure function of the view served plus
`MAKEFUN_TRIAL_SEED` (xoshiro256** streams per game), so a trial's live
pass and its replay agree byte for byte: no wall clock, no unseeded
randomness, one env request at a time.

## Run

```
python3 main.py   # needs MAKEFUN_ENV_URL, MAKEFUN_TRIAL_SEED,
                  # MAKEFUN_PROMPT_FILE in the environment
```

## Tests

`/tmp/test_werewolf.py` (unit tests on synthetic views built from the
practice table, plus a mock-env smoke test) and `/tmp/run_integration.sh`
(full offline trial against the kit's real env server with a stub house
gateway) both pass: 30/30 games finish, zero refused actions, and the
exported trial state is byte-identical across runs with the same seed.
