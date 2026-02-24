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
    Fetches shifts + PBP for one game and returns pair stats.
    The frontend calls this once per game and accumulates results itself.
    """
    goalie_ids_param = request.args.get("goalies", "")
    goalie_ids = set(int(x) for x in goalie_ids_param.split(",") if x.strip())

    try:
        # Shifts
        shift_data = stat_get("/shiftcharts", {"cayenneExp": f"gameId={game_id}"})
        intervals  = {}
        for s in shift_data.get("data", []):
            pid    = s.get("playerId")
            tid    = s.get("teamId")
            period = s.get("period", 1)
            if not pid or period > 4:
                continue
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

        pairs      = {}
        toi_buckets = {}

        for play in pbp.get("plays", []):
            if not is_5v5(play.get("situationCode")):
                continue

            period    = play.get("periodDescriptor", {}).get("number", 1)
            time_str  = play.get("timeInPeriod", "0:00")
            game_sec  = period_offset(period) + mmss_to_sec(time_str)
            min_buck  = mmss_to_sec(time_str) // 60
            ev_type   = play.get("typeDescKey", "")
            ev_team   = play.get("details", {}).get("eventOwnerTeamId")

            # Resolve on-ice
            home_on, away_on = [], []
            for pid, shifts in intervals.items():
                for sh in shifts:
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
                skaters = sorted(p for p in team_ids if p not in goalie_ids)
                if len(skaters) < 2:
                    continue

                for i in range(len(skaters) - 1):
                    for j in range(i + 1, len(skaters)):
                        p1, p2  = skaters[i], skaters[j]
                        key     = f"{p1}_{p2}"
                        is_for  = ev_team == team_id
                        is_agn  = ev_team == opp_id

                        if key not in pairs:
                            pairs[key] = {
                                "p1": str(p1), "p2": str(p2), "teamId": team_id,
                                "toi": 0,
                                "gf": 0, "ga": 0,
                                "sf": 0, "sa": 0,
                                "ff": 0, "fa": 0,
                                "cf": 0, "ca": 0,
                            }
                            toi_buckets[key] = set()

                        bucket = f"{period}_{min_buck}"
                        if bucket not in toi_buckets[key]:
                            toi_buckets[key].add(bucket)
                            pairs[key]["toi"] += 60

                        s = pairs[key]
                        if ev_type == "goal":
                            if is_for: s["gf"] += 1
                            if is_agn: s["ga"] += 1
                        elif ev_type == "shot-on-goal":
                            if is_for: s["sf"] += 1; s["ff"] += 1; s["cf"] += 1
                            if is_agn: s["sa"] += 1; s["fa"] += 1; s["ca"] += 1
                        elif ev_type == "missed-shot":
                            if is_for: s["ff"] += 1; s["cf"] += 1
                            if is_agn: s["fa"] += 1; s["ca"] += 1
                        elif ev_type == "blocked-shot":
                            if is_for: s["cf"] += 1
                            if is_agn: s["ca"] += 1

        return jsonify({"pairs": list(pairs.values()), "homeId": home_id, "awayId": away_id, "players": game_players})

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
