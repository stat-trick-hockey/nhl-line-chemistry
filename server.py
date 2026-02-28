"""
NHL Line Chemistry - Local Backend Server
==========================================
Proxies NHL API calls to avoid browser CORS restrictions.

Install deps:
    pip install flask flask-cors requests

Run:
    python server.py

Then open http://localhost:5000 in your browser.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import time
from collections import defaultdict

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

WEB_API  = "https://api-web.nhle.com/v1"
STAT_API = "https://api.nhle.com/stats/rest/en"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "nhl-line-chemistry/1.0"})


def web_get(path):
    r = SESSION.get(f"{WEB_API}{path}", timeout=15)
    r.raise_for_status()
    return r.json()


def stat_get(path, params=None):
    r = SESSION.get(f"{STAT_API}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


# ── Utility ────────────────────────────────────────────────────────────────

def mmss_to_sec(mmss):
    if not mmss:
        return 0
    try:
        m, s = mmss.strip().split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return 0


def period_offset(period):
    return (period - 1) * 1200


def is_5v5(code):
    return code == "1551"


# ── Routes: schedule / roster ──────────────────────────────────────────────

@app.route("/api/games")
def get_games():
    team   = request.args.get("team", "BUF")
    season = request.args.get("season", "20242025")
    try:
        data = web_get(f"/club-schedule-season/{team}/{season}")
        ids  = [
            g["id"] for g in data.get("games", [])
            if g.get("gameType") == 2 and g.get("gameState") == "OFF"
        ]
        return jsonify({"gameIds": ids})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/roster")
def get_roster():
    team = request.args.get("team", "BUF")
    try:
        players = {}
        # Current roster (for goalie detection)
        data = web_get(f"/roster/{team}/current")
        for group in ("forwards", "defensemen", "goalies"):
            for p in data.get(group, []):
                first = p.get("firstName", {}).get("default", "")
                last  = p.get("lastName",  {}).get("default", "")
                players[str(p["id"])] = {
                    "name": f"{first} {last}".strip(),
                    "pos":  p.get("positionCode", ""),
                }
        return jsonify(players)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Route: process a single game ───────────────────────────────────────────

@app.route("/api/game/<int:game_id>")
def process_game(game_id):
    """
    Fetches shifts + PBP for one game and returns combination stats.

    mode param:
      "pairs"   - all skater pairs (default)
      "trios"   - forward lines (groups of 3 forwards)
      "d-pairs" - defensive pairs (groups of 2 defensemen)
    """
    mode = request.args.get("mode", "pairs")
    combo_size = 3 if mode == "trios" else 2

    try:
        # Shifts
        try:
            shift_data = stat_get("/shiftcharts", {"cayenneExp": f"gameId={game_id}"})
        except Exception as shift_err:
            return jsonify({"error": f"Shift chart fetch failed: {shift_err}"}), 500

        shift_records = shift_data.get("data", [])
        print(f"[game {game_id}] shifts: {len(shift_records)} records")

        intervals  = {}
        for s in shift_records:
            pid    = s.get("playerId")
            tid    = s.get("teamId")
            period = s.get("period", 1)
            if not pid or period > 4:
                continue
            pid = int(pid)  # force integer
            off   = period_offset(period)
            start = off + mmss_to_sec(s.get("startTime", "0:00"))
            end   = off + mmss_to_sec(s.get("endTime",   "0:00"))
            if end <= start:
                end = start + 1
            if pid not in intervals:
                intervals[pid] = []
            intervals[pid].append({"tid": tid, "start": start, "end": end})

        # Play-by-play
        pbp     = web_get(f"/gamecenter/{game_id}/play-by-play")
        home_id = pbp["homeTeam"]["id"]
        away_id = pbp["awayTeam"]["id"]

        # Extract all players who appeared in this game from rosterSpots
        game_players = {}
        for spot in pbp.get("rosterSpots", []):
            pid   = spot.get("playerId")
            first = spot.get("firstName", {}).get("default", "")
            last  = spot.get("lastName",  {}).get("default", "")
            pos   = spot.get("positionCode", "")
            if pid:
                game_players[str(pid)] = {"name": f"{first} {last}".strip(), "pos": pos}

        # Enrich game_players with roster data for any players missing positions
        # This catches players traded mid-season who may not appear in rosterSpots
        home_abbr = pbp.get("homeTeam", {}).get("abbrev", "")
        away_abbr = pbp.get("awayTeam", {}).get("abbrev", "")
        for abbr in (home_abbr, away_abbr):
            if not abbr:
                continue
            try:
                rd = web_get(f"/roster/{abbr}/current")
                for group in ("forwards", "defensemen", "goalies"):
                    for p in rd.get(group, []):
                        pid_str = str(p.get("id", ""))
                        if pid_str and pid_str not in game_players:
                            first = p.get("firstName", {}).get("default", "")
                            last  = p.get("lastName",  {}).get("default", "")
                            game_players[pid_str] = {
                                "name": f"{first} {last}".strip(),
                                "pos":  p.get("positionCode", ""),
                            }
                        elif pid_str and not game_players[pid_str].get("pos"):
                            game_players[pid_str]["pos"] = p.get("positionCode", "")
            except Exception:
                pass  # roster fetch failing shouldn't break the game

        from itertools import combinations as _combos

        pairs = {}

        # ── Helper: get position for a player ──────────────────────────────
        def get_pos(pid):
            return game_players.get(str(pid), {}).get("pos", "")

        # ── STEP 1: Extract 5v5 time windows from PBP ───────────────────────
        # Build a list of (start_sec, end_sec) intervals where the game was 5v5.
        # We do this by walking the plays in order and tracking situationCode changes.
        five_v_five_windows = []
        fvf_start = None
        last_game_sec = 0

        sorted_plays = sorted(
            pbp.get("plays", []),
            key=lambda p: (
                p.get("periodDescriptor", {}).get("number", 1),
                mmss_to_sec(p.get("timeInPeriod", "0:00"))
            )
        )

        for play in sorted_plays:
            period   = play.get("periodDescriptor", {}).get("number", 1)
            if period > 3:   # ignore OT for 5v5 purposes
                break
            t        = period_offset(period) + mmss_to_sec(play.get("timeInPeriod", "0:00"))
            sit_code = play.get("situationCode", "")
            is_fvf   = (sit_code == "1551")

            if is_fvf and fvf_start is None:
                fvf_start = t
            elif not is_fvf and fvf_start is not None:
                if t > fvf_start:
                    five_v_five_windows.append((fvf_start, t))
                fvf_start = None
            last_game_sec = t

        # Close any open window at end of regulation
        if fvf_start is not None:
            five_v_five_windows.append((fvf_start, last_game_sec))

        # ── STEP 2: Clip shift intervals to 5v5 windows only ─────────────────
        # For each player, compute their shifts clipped to 5v5 time only.
        def clip_to_5v5(shift_list):
            """Return shift list clipped to 5v5 windows."""
            clipped = []
            for (ss, se) in shift_list:
                for (ws, we) in five_v_five_windows:
                    lo = max(ss, ws)
                    hi = min(se, we)
                    if hi > lo:
                        clipped.append((lo, hi))
            return clipped

        # Group shifts by team, clipped to 5v5
        # Each player's shifts already carry their teamId per shift entry.
        team_shifts = {home_id: {}, away_id: {}}
        for pid, shift_list in intervals.items():
            # Group this player's raw shifts by team
            by_team = {home_id: [], away_id: []}
            for sh in shift_list:
                tid = sh["tid"]
                if tid in by_team:
                    by_team[tid].append((sh["start"], sh["end"]))
            # Clip each team's shifts to 5v5 windows and store
            for tid, raw in by_team.items():
                if raw:
                    clipped = clip_to_5v5(raw)
                    if clipped:
                        team_shifts[tid][pid] = clipped

        # Filter to eligible players per team based on mode
        def eligible_players(tid):
            result = []
            for pid in team_shifts.get(tid, {}):
                pos = get_pos(pid)
                if mode == "trios":
                    # Include if forward, OR if position unknown (exclude only D and G)
                    if pos in ("C", "L", "R", "F", "LW", "RW") or (pos not in ("D", "G") and pos != ""):
                        result.append(pid)
                    elif pos == "":
                        # Unknown position - include as forward candidate
                        # (goalies always have shifts marked, D is usually known)
                        result.append(pid)
                elif mode == "d-pairs":
                    if pos == "D":
                        result.append(pid)
                else:
                    if pos != "G":
                        result.append(pid)
            return result

        def combo_toi_seconds(combo, tid):
            """
            Compute seconds where ALL players in combo are on ice simultaneously,
            within 5v5 time only (already clipped in team_shifts).
            """
            current = list(team_shifts[tid].get(combo[0], []))
            for pid in combo[1:]:
                nxt = team_shifts[tid].get(pid, [])
                merged = []
                for (s1, e1) in current:
                    for (s2, e2) in nxt:
                        lo, hi = max(s1, s2), min(e1, e2)
                        if hi > lo:
                            merged.append((lo, hi))
                current = merged
                if not current:
                    return 0
            return sum(e - s for s, e in current)

        # ── STEP 3: Build combo records with exact 5v5 TOI ───────────────────
        for tid in (home_id, away_id):
            elig = eligible_players(tid)
            if len(elig) < combo_size:
                continue
            for combo in _combos(elig, combo_size):
                toi_sec = combo_toi_seconds(combo, tid)
                if toi_sec < 1:
                    continue
                key = "_".join(str(p) for p in sorted(combo))
                if key not in pairs:
                    pairs[key] = {
                        "players": [str(p) for p in sorted(combo)],
                        "p1": str(sorted(combo)[0]),
                        "p2": str(sorted(combo)[1]),
                        "teamId": tid,
                        "toi": toi_sec,
                        "gf": 0, "ga": 0,
                        "sf": 0, "sa": 0,
                        "ff": 0, "fa": 0,
                        "cf": 0, "ca": 0,
                    }

        # ── STEP 4: Count events via PBP (5v5 filter already in is_5v5 check) ──
        for play in pbp.get("plays", []):
            if not is_5v5(play.get("situationCode")):
                continue

            period   = play.get("periodDescriptor", {}).get("number", 1)
            time_str = play.get("timeInPeriod", "0:00")
            game_sec = period_offset(period) + mmss_to_sec(time_str)
            ev_type  = play.get("typeDescKey", "")
            ev_team  = play.get("details", {}).get("eventOwnerTeamId")

            # Resolve on-ice at this moment
            home_on, away_on = [], []
            for pid, shift_list in intervals.items():
                for sh in shift_list:
                    if sh["start"] <= game_sec < sh["end"]:
                        if sh["tid"] == home_id:
                            home_on.append(pid)
                        elif sh["tid"] == away_id:
                            away_on.append(pid)
                        break

            for team_ids, opp_id, team_id in [
                (home_on, away_id, home_id),
                (away_on, home_id, away_id),
            ]:
                if mode == "trios":
                    elig = sorted(p for p in team_ids if get_pos(p) in ("C", "L", "R", "F", "LW", "RW") or get_pos(p) not in ("D", "G"))
                elif mode == "d-pairs":
                    elig = sorted(p for p in team_ids if get_pos(p) == "D")
                else:
                    elig = sorted(p for p in team_ids if get_pos(p) != "G")

                if len(elig) < combo_size:
                    continue

                for combo in _combos(elig, combo_size):
                    key    = "_".join(str(p) for p in sorted(combo))
                    is_for = ev_team == team_id
                    is_agn = ev_team == opp_id

                    if key not in pairs:
                        continue  # skip combos with no TOI (shouldn't happen)

                    s = pairs[key]
                    if ev_type == "goal":
                        if is_for: s["gf"] += 1
                        if is_agn: s["ga"] += 1
                    elif ev_type == "shot-on-goal":
                        if is_for:  s["sf"] += 1; s["ff"] += 1; s["cf"] += 1
                        if is_agn:  s["sa"] += 1; s["fa"] += 1; s["ca"] += 1
                    elif ev_type == "missed-shot":
                        if is_for:  s["ff"] += 1; s["cf"] += 1
                        if is_agn:  s["fa"] += 1; s["ca"] += 1
                    elif ev_type == "blocked-shot":
                        if is_for:  s["cf"] += 1
                        if is_agn:  s["ca"] += 1

        print(f"[game {game_id}] intervals: {len(intervals)} players, plays: {len(pbp.get('plays', []))}, pairs: {len(pairs)}")
        return jsonify({
            "pairs":        list(pairs.values()),
            "homeId":       home_id,
            "awayId":       away_id,
            "players":      game_players,
            "debug": {
                "interval_player_count": len(intervals),
                "play_count":            len(pbp.get("plays", [])),
                "pair_count":            len(pairs),
            }
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Serve the frontend ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return app.send_static_file("nhl_line_chemistry.html")


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print(f"  NHL Line Chemistry Server")
    print(f"  Open: http://localhost:{port}")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=port)
