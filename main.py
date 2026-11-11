#!/usr/bin/env python3
"""werewolf-agent-01: a deterministic, model-free Werewolf player.

Plays one seat in 30 games of 8-player Werewolf against house agents
(claude-haiku-5-5) through the make.fun env. Standard library only, no model
calls, no API keys, no network except the env.

Every decision is a pure function of the view the env served plus
MAKEFUN_TRIAL_SEED, so a trial's live pass and its replay agree byte for
byte: no wall clock, no unseeded randomness, one env request at a time, in
the order they were made.

Strategy, in brief:
- Rebuild each game's state from the view log on every decision: revealed
  roles, deaths, public votes, speeches and claims.
- Score every living seat's suspicion from vote history (voted for a
  revealed villager: suspicious; voted for a revealed wolf: clears), from
  claims that contradict revealed roles (a false seer claim is near-proof
  of a wolf), and from speech signals.
- Werewolf: night kills need unanimity, so always match the partner's pick
  when they voted first; when voting first, name the best target and let
  the partner see it. By day, blend in: echo the village's leading suspect
  with a real log fact, vote with the plurality, never vote the partner.
- Seer: inspect the most suspicious uninspected seat; once a wolf is
  found, claim promptly with the night and the evidence.
- Doctor: protect the claimed seer, else the player most likely to be the
  night's target; never self, never last night's seat.
- Villager: be the village's memory -- quote vote history and push the
  single highest-suspicion seat; never split the vote.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MAX_CHARS = 500
OBSERVE_TIMEOUT_S = 60
ACT_TIMEOUT_S = 30

# ---------------------------------------------------------------- seeded RNG

_RNG_DOMAIN = "agent-fun/agent/rng/v1"
_MASK64 = (1 << 64) - 1


def _rotl(x, k):
    return ((x << k) | (x >> (64 - k))) & _MASK64


class Rng:
    """xoshiro256** seeded from sha256(domain, seed, label). Same stream for
    the same seed and label on every run, so tie-breaks replay exactly."""

    def __init__(self, seed, label=""):
        h = hashlib.sha256(
            _RNG_DOMAIN.encode() + b"\0" + str(seed).encode() + b"\0"
            + label.encode()
        ).digest()
        s = [int.from_bytes(h[i:i + 8], "big") for i in range(0, 32, 8)]
        if not any(s):
            s[0] = 1
        self._s = s

    def next_u64(self):
        s0, s1, s2, s3 = self._s
        result = (_rotl((s1 * 5) & _MASK64, 7) * 9) & _MASK64
        t = (s1 << 17) & _MASK64
        a2 = s2 ^ s0
        a3 = s3 ^ s1
        self._s = [s0 ^ a3, s1 ^ a2, a2 ^ t, _rotl(a3, 45)]
        return result

    def randint(self, lo, hi):
        span = hi - lo + 1
        limit = (1 << 64) - (1 << 64) % span
        while True:
            x = self.next_u64()
            if x < limit:
                return lo + x % span

    def choice(self, items):
        return items[self.randint(0, len(items) - 1)]


# ---------------------------------------------------------------- HTTP

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(url, payload, timeout):
    """One POST of a JSON object; returns (status, parsed body or None)."""
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with _OPENER.open(req, timeout=timeout) as res:
            raw = res.read()
            return res.status, _parse(raw)
    except urllib.error.HTTPError as err:
        try:
            return err.code, _parse(err.read())
        finally:
            err.close()
    except (urllib.error.URLError, OSError):
        return 0, None


def _parse(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------- text helpers

_CTRL_RUN = re.compile(r"[\t\n\v\f\r\x85\u2028\u2029]+")


def fold(text, max_chars=MAX_CHARS):
    """Fold a statement the env's way and cut it to max_chars code points."""
    t = _CTRL_RUN.sub(" ", text)
    t = "".join(ch for ch in t if ord(ch) >= 32 and ch != "\x7f")
    t = t.strip()
    if len(t) > max_chars:
        t = t[:max_chars].rstrip()
    return t


def seat_name(state, seat):
    p = state.players.get(seat)
    return f"seat {seat} ({p['name']})" if p else f"seat {seat}"


# ---------------------------------------------------------------- game state

VILLAGE_ROLES = ("villager", "seer", "doctor")


class State:
    """Everything the entry knows about one game, rebuilt from its view."""

    def __init__(self, view):
        self.day = view.get("day", 1)
        self.phase = view.get("phase", "")
        you = view.get("you") or {}
        self.me = you.get("seat")
        self.role = you.get("role")
        self.alive = bool(you.get("alive"))
        partner = you.get("partner") or {}
        self.partner = partner.get("seat") if isinstance(partner, dict) else None
        self.partner_alive = bool(partner.get("alive")) if isinstance(partner, dict) else False
        self.players = {}
        for p in view.get("players") or []:
            self.players[p["seat"]] = {
                "name": p.get("name", "?"),
                "alive": bool(p.get("alive")),
                "role": p.get("role"),
            }
        self.living = [s for s, p in self.players.items() if p["alive"]]
        self.revealed = {s: p["role"] for s, p in self.players.items()
                         if p["role"]}
        # Seer knowledge: certain wolves / certain villagers, from my own checks.
        self.known_wolf = set()
        self.known_villager = set()
        self.seer_nights = {}       # seat -> night I inspected it
        for r in view.get("seerResults") or []:
            self.seer_nights[r["seat"]] = r.get("night")
            if r.get("werewolf"):
                self.known_wolf.add(r["seat"])
            else:
                self.known_villager.add(r["seat"])
        for s, role in self.revealed.items():
            if role == "werewolf":
                self.known_wolf.add(s)
            elif role in VILLAGE_ROLES:
                self.known_villager.add(s)
        self.inspected = {r["seat"] for r in view.get("seerResults") or []}
        # Tonight's wolf votes, as seen by a werewolf (partner's pick included).
        self.wolf_votes = {}
        for v in view.get("wolfVotes") or []:
            if v.get("night") == self.day:
                self.wolf_votes[v["seat"]] = v.get("target")
        last = view.get("lastProtect") or {}
        self.last_protect = last.get("seat")
        self.log = view.get("log") or []
        self._parse_log()

    # -- log parsing ------------------------------------------------------
    def _parse_log(self):
        self.votes_by_day = {}      # day -> [(voter, target)]
        self.speeches = []          # {day, seat, round, text}
        self.my_speeches_today = []
        self.votes_today = []       # [(voter, target)] for the current day
        self.exiles = []            # {day, seat, role, tally}
        self.deaths = []            # {day, seat, role}
        self.claims = {}            # seat -> {"seer": bool, "wolf": seat|None,
                                    #          "clear": seat|None, "day": int}
        self.debate_order = []
        for e in self.log:
            kind = e.get("kind")
            day = e.get("day", 0)
            if kind == "dawn":
                if e.get("died") is not None:
                    self.deaths.append({"day": day, "seat": e["died"],
                                        "role": e.get("role")})
            elif kind == "debate":
                if day == self.day:
                    self.debate_order = list(e.get("order") or [])
            elif kind == "speech":
                sp = {"day": day, "seat": e.get("seat"),
                      "round": e.get("round", 1), "text": e.get("text", "")}
                self.speeches.append(sp)
                if day == self.day and sp["seat"] == self.me:
                    self.my_speeches_today.append(sp)
                self._parse_claim(sp)
            elif kind == "vote":
                v = (e.get("seat"), e.get("target"))
                self.votes_by_day.setdefault(day, []).append(v)
                if day == self.day:
                    self.votes_today.append(v)
            elif kind == "exile":
                self.exiles.append({"day": day, "seat": e.get("seat"),
                                    "role": e.get("role"),
                                    "tally": e.get("tally") or []})
        # Votes cast so far today, as a tally.
        self.tally_today = {}
        for _, t in self.votes_today:
            if t is not None:
                self.tally_today[t] = self.tally_today.get(t, 0) + 1

    def _parse_claim(self, sp):
        t = sp["text"].lower()
        if "seer" not in t:
            return
        if not any(k in t for k in ("i am", "i'm", "im the", "checked",
                                    "inspected", "i checked")):
            return
        seats = [int(x) for x in re.findall(r"seat\s+(\d+)", t)]
        wolf = None
        clear = None
        if "wolf" in t and seats:
            # "checked seat X: wolf" -- the first seat named is the claim.
            wolf = seats[0]
        if ("not a wolf" in t or "not wolf" in t or "clear" in t) and seats:
            clear = seats[0]
        c = self.claims.setdefault(sp["seat"], {"seer": True, "wolf": None,
                                                "clear": None, "day": sp["day"]})
        if wolf is not None:
            c["wolf"] = wolf
        if clear is not None:
            c["clear"] = clear

    # -- derived reads ----------------------------------------------------
    def is_claimed_seer(self, seat):
        return seat in self.claims

    def false_seer_claim(self, seat):
        """A seer claim contradicted by a revealed role: near-proof of wolf."""
        c = self.claims.get(seat)
        if not c:
            return False
        if c["wolf"] is not None and c["wolf"] in self.revealed:
            return self.revealed[c["wolf"]] != "werewolf"
        return False

    def credible_seer(self, seat):
        """A seer claim whose wolf call matches a revealed wolf."""
        c = self.claims.get(seat)
        if not c or c["wolf"] is None:
            return False
        return self.revealed.get(c["wolf"]) == "werewolf"

    def accused_me_or_partner(self, seat):
        """Did this seat's speeches accuse me or (as a wolf) my partner?"""
        targets = {self.me}
        if self.role == "werewolf" and self.partner is not None:
            targets.add(self.partner)
        for sp in self.speeches:
            if sp["seat"] != seat:
                continue
            t = sp["text"].lower()
            if "wolf" not in t and "vote" not in t:
                continue
            for tg in targets:
                if re.search(r"seat\s+%d\b" % tg, t):
                    return True
        return False

    def speech_count(self, seat):
        return sum(1 for sp in self.speeches if sp["seat"] == seat)

    def voted_for(self, seat, day=None):
        """(votes for revealed villagers, votes for revealed wolves, example)."""
        nv = nw = 0
        example = None
        days = [day] if day is not None else sorted(self.votes_by_day)
        for d in days:
            for voter, target in self.votes_by_day.get(d, []):
                if voter != seat or target is None:
                    continue
                role = self.revealed.get(target)
                if role in VILLAGE_ROLES:
                    nv += 1
                    if example is None:
                        example = (d, target)
                elif role == "werewolf":
                    nw += 1
        return nv, nw, example

    def led_exile(self, seat):
        """Seats this player helped exile (their vote target was exiled)."""
        out = []
        for ex in self.exiles:
            for voter, target in self.votes_by_day.get(ex["day"], []):
                if voter == seat and target == ex["seat"]:
                    out.append(ex["seat"])
        return out

    def i_claimed_seer(self):
        for sp in self.speeches:
            if sp["seat"] == self.me and "seer" in sp["text"].lower():
                return True
        return False


# ---------------------------------------------------------------- suspicion

def suspicion(state):
    """Village-perspective suspicion per living seat (not me, not partner).

    Higher = more likely a wolf. Built only from public facts, so a wolf
    can use the same scores to blend in.
    """
    scores = {}
    for s in state.living:
        if s == state.me:
            continue
        if state.role == "werewolf" and s == state.partner:
            scores[s] = -100.0
            continue
        sc = 0.0
        if s in state.known_wolf:
            sc += 100.0
        if s in state.known_villager:
            sc -= 100.0
        nv, nw, _ = state.voted_for(s)
        sc += 2.0 * nv
        sc -= 3.0 * nw
        # Never voted a revealed wolf while wolves were known dead.
        if state.known_wolf and nw == 0 and nv > 0:
            sc += 1.0
        # A seer claim contradicted by a reveal is near-proof of a wolf.
        if state.false_seer_claim(s):
            sc += 6.0
        if state.credible_seer(s):
            sc -= 4.0
        # Speech signals: accusing a revealed villager, or a revealed wolf.
        for sp in state.speeches:
            if sp["seat"] != s:
                continue
            t = sp["text"].lower()
            for m in re.findall(r"seat\s+(\d+)", t):
                tg = int(m)
                if tg not in state.revealed:
                    continue
                if "wolf" in t or "vote" in t:
                    if state.revealed[tg] in VILLAGE_ROLES:
                        sc += 1.0
                    elif state.revealed[tg] == "werewolf":
                        sc -= 2.0
        # Quiet players hide more easily than loud ones; a small nudge.
        if state.speeches and state.speech_count(s) == 0:
            sc += 0.5
        scores[s] = sc
    return scores


def pick_best(scores, rng, exclude=()):
    """Highest score; deterministic seeded tie-break among equals."""
    cands = [(sc, s) for s, sc in scores.items() if s not in exclude]
    if not cands:
        return None
    top = max(sc for sc, _ in cands)
    tied = sorted(s for sc, s in cands if sc == top)
    return tied[0] if len(tied) == 1 else rng.choice(tied)


def plurality_leader(state, exclude=()):
    """Today's vote leader so far (seat, votes), or (None, 0)."""
    best, n = None, 0
    for seat, votes in state.tally_today.items():
        if seat in exclude or seat not in state.living:
            continue
        if votes > n:
            best, n = seat, votes
    return best, n


# ---------------------------------------------------------------- decisions

def decide_kill(state, options, rng):
    """Night kill. Agreement is required, so match the partner's pick."""
    partner_pick = None
    if state.partner is not None:
        partner_pick = state.wolf_votes.get(state.partner)
    if partner_pick is not None and partner_pick in options:
        return partner_pick
    # I vote first (or partner abstained): name the best target.
    if not options:
        return None
    scored = {s: _kill_score(state, s) for s in options}
    return pick_best(scored, rng)


def _kill_score(state, s):
    sc = 0.0
    if state.is_claimed_seer(s):
        sc += 10.0 if state.credible_seer(s) else 4.0
    if state.accused_me_or_partner(s):
        sc += 5.0
    if state.led_exile(s):
        sc += 3.0
    if state.votes_by_day.get(state.day - 1):
        tally = {}
        for _, t in state.votes_by_day[state.day - 1]:
            if t is not None:
                tally[t] = tally.get(t, 0) + 1
        if tally and max(tally, key=tally.get) == s:
            sc -= 8.0
    sc += 0.2 * state.speech_count(s)
    return sc


def decide_protect(state, options, rng):
    """Doctor: the claimed seer first, else the likeliest night target."""
    if not options:
        return None
    # A credible claimed seer is the wolves' first target.
    for s in options:
        if state.credible_seer(s):
            return s
    for s in options:
        if state.is_claimed_seer(s):
            return s
    # Whoever accused a wolf or led a wolf's exile is next on the list.
    sc = suspicion(state)
    best, best_sc = None, None
    for s in options:
        val = -sc.get(s, 0.0)  # most town-read
        if state.led_exile(s):
            val += 2.0
        for ex in state.exiles:
            if ex["role"] == "werewolf" and s in [
                    v for v, _ in state.votes_by_day.get(ex["day"], [])]:
                val += 1.0
        if best_sc is None or val > best_sc:
            best, best_sc = s, val
    return best if best is not None else rng.choice(sorted(options))


def decide_inspect(state, options, rng):
    """Seer: the most suspicious seat I have not inspected yet."""
    if not options:
        return None
    cands = [s for s in options if s not in state.inspected
             and s not in state.known_wolf and s not in state.known_villager]
    if not cands:
        return None
    sc = suspicion(state)
    return pick_best({s: sc.get(s, 0.0) for s in cands}, rng)


def decide_vote(state, options, rng):
    """Day vote: the highest-suspicion seat, joining today's plurality
    when it is close, so the village does not split its vote."""
    if not options:
        return None
    sc = suspicion(state)
    exclude = set()
    if state.role == "werewolf" and state.partner is not None:
        exclude.add(state.partner)
    cands = [s for s in options if s not in exclude]
    if not cands:
        return None
    # A seer-known wolf is the vote, full stop.
    for s in cands:
        if s in state.known_wolf:
            return s
    top = pick_best({s: sc.get(s, 0.0) for s in cands}, rng)
    leader, n = plurality_leader(state, exclude=exclude | {state.me})
    if leader is not None and leader in cands and n >= 1:
        # Join the leader when it is a live suspect, not a known villager.
        if leader not in state.known_villager and sc.get(leader, 0.0) >= sc.get(top, 0.0) - 2.0:
            return leader
    return top


def _accusation(state, target):
    """One concrete vote-history reason to push `target`, or None."""
    nv, nw, ex = state.voted_for(target)
    name = seat_name(state, target)
    if ex is not None:
        d, vt = ex
        vname = state.players.get(vt, {}).get("name", "?")
        reason = (f"voted for {vname}, a revealed villager, on day {d}"
                  + (" and has never voted a wolf" if nw == 0 else ""))
        return f"{name} {reason}. Vote {name}."
    if nv > 0:
        return (f"{name} voted for {nv} revealed villager"
                + ("s" if nv > 1 else "")
                + (" and never voted a wolf" if nw == 0 else "")
                + f". Vote {name}.")
    if state.false_seer_claim(target):
        return (f"{name} claimed seer, but the seat they called a wolf "
                f"turned out a villager. A lying seer claim. Vote {name}.")
    if state.speech_count(target) == 0 and state.speeches:
        return (f"{name} has said nothing while villagers died. Silence "
                f"helps the wolves. Vote {name}.")
    return f"{name} is my strongest read for a wolf. Vote {name}."


def decide_speak(state, max_chars, rng):
    """A short, factual statement: one claim + one reason. Pass when there
    is nothing new to say (second debate cycle, no new fact)."""
    # Second time speaking today: pass, unless I am the seer and have not
    # yet made my wolf claim.
    already = len(state.my_speeches_today) > 0
    wolf_result = None
    if state.role == "seer":
        for r in state.known_wolf:
            if r in state.living and r not in state.revealed:
                wolf_result = r
                break
    if already and not (state.role == "seer" and wolf_result is not None
                        and not state.i_claimed_seer()):
        return None  # pass
    sc = suspicion(state)
    exclude = {state.me}
    if state.role == "werewolf" and state.partner is not None:
        exclude.add(state.partner)
    cands = [s for s in state.living if s not in exclude]

    if state.role == "seer" and wolf_result is not None:
        # Claim with the night and the evidence; the village needs it.
        night = state.seer_nights.get(wolf_result)
        when = f" on night {night}" if night is not None else ""
        text = (f"I am the seer. I checked {seat_name(state, wolf_result)}"
                f"{when}: a werewolf. Vote {seat_name(state, wolf_result)}.")
        return fold(text, max_chars)

    # The village's leading suspect: today's vote leader, else top suspicion.
    leader, n = plurality_leader(state, exclude=exclude)
    target = None
    if leader is not None and leader in cands \
            and leader not in state.known_villager:
        target = leader
    if target is None:
        target = pick_best({s: sc.get(s, 0.0) for s in cands}, rng)
    if target is None:
        return None
    # If someone is accusing me, counter-accuse with their vote history.
    for sp in state.speeches:
        if sp["day"] != state.day or sp["seat"] in exclude:
            continue
        t = sp["text"].lower()
        if re.search(r"seat\s+%d\b" % state.me, t) and "wolf" in t:
            acc = _accusation(state, sp["seat"])
            if acc:
                return fold(f"Seat {sp['seat']} accused me, but " +
                            acc[0].lower() + acc[1:], max_chars)
    # A claimed seer pushing their wolf: back them when the claim is live.
    for s in cands:
        c = state.claims.get(s)
        if c and c["wolf"] is not None and c["wolf"] in state.living \
                and c["wolf"] not in state.revealed \
                and not state.false_seer_claim(s):
            return fold(
                f"{seat_name(state, s)} says they checked "
                f"{seat_name(state, c['wolf'])}: a wolf. Their story holds "
                f"so far. Vote {seat_name(state, c['wolf'])}.", max_chars)
    return fold(_accusation(state, target), max_chars)


# ---------------------------------------------------------------- main loop

def decide(view, game_no, seed, decision, options, max_chars):
    """One decision's /act body, as a pure function of view + seed."""
    state = State(view)
    rng = Rng(seed, f"game-{game_no}")
    if decision == "speak":
        text = decide_speak(state, max_chars or MAX_CHARS, rng)
        if text:
            return {"game": game_no, "decision": "speak", "text": text}
        return {"game": game_no, "decision": "speak", "pass": True}
    if decision == "kill":
        return {"game": game_no, "decision": "kill",
                "target": decide_kill(state, options or [], rng)}
    if decision == "protect":
        return {"game": game_no, "decision": "protect",
                "target": decide_protect(state, options or [], rng)}
    if decision == "inspect":
        return {"game": game_no, "decision": "inspect",
                "target": decide_inspect(state, options or [], rng)}
    if decision == "vote":
        return {"game": game_no, "decision": "vote",
                "target": decide_vote(state, options or [], rng)}
    return {"game": game_no, "decision": decision, "target": None}


def _fallback(decision, game_no):
    if decision == "speak":
        return {"game": game_no, "decision": "speak", "pass": True}
    return {"game": game_no, "decision": decision, "target": None}


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    seed = os.environ.get("MAKEFUN_TRIAL_SEED", "")
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE", "")
    if not env_url:
        print("MAKEFUN_ENV_URL is not set", file=sys.stderr)
        return 2
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                f.read()
        except OSError as err:
            print(f"cannot read prompt file: {err}", file=sys.stderr)
            return 2
    while True:
        status, snap = _post(env_url + "/observe", {}, OBSERVE_TIMEOUT_S)
        if status == 0 or not isinstance(snap, dict):
            # No answer from the env: wait and ask again. A request the
            # env never saw leaves no record, so retrying is replay-safe.
            time.sleep(2)
            continue
        if snap.get("done"):
            return 0
        pending = snap.get("pending") or []
        for p in pending:
            game_no = p.get("game")
            decision = p.get("decision")
            if not isinstance(game_no, int) or decision not in (
                    "speak", "vote", "kill", "protect", "inspect"):
                continue
            view = p.get("view") or {}
            try:
                body = decide(view, game_no, seed, decision,
                              p.get("options"), p.get("max_chars"))
            except Exception:
                body = _fallback(decision, game_no)
            status, _ = _post(env_url + "/act", body, ACT_TIMEOUT_S)
            if status == 0:
                # The act may or may not have landed; try once more. If it
                # did land, the retry is refused as not_pending and changes
                # nothing.
                status, _ = _post(env_url + "/act", body, ACT_TIMEOUT_S)
            elif status in (400, 409):
                if status == 400:
                    # Refused (bad target / too long / empty): fall back to
                    # the safe answer once; a 409 means it already moved on.
                    _post(env_url + "/act", _fallback(decision, game_no),
                          ACT_TIMEOUT_S)


if __name__ == "__main__":
    sys.exit(main())
