import requests
import pandas as pd
import time
import threading
from itertools import cycle
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from dotenv import load_dotenv
import sys


API_KEYS = []
# --- CONFIGURATION ---

SEED_STEAM_ID = ""
TARGET_RATING_MIN = 7500
TARGET_RATING_MAX = 15000
MAX_PLAYERS = 1000

MATCH_LIMIT_PER_PLAYER = 10
MAX_WORKERS = 8          # Increase if you have more API keys / stable rate limit
REQUEST_TIMEOUT = 15

base_url = "https://api-public.cs-prod.leetify.com"

# --- API KEY ROTATION ---
key_lock = threading.Lock()
key_cycle = None

def get_headers():
    """Rotate API keys safely across threads."""
    global key_cycle

    if key_cycle is None:
        raise RuntimeError("API keys are not initialized. Call init_crawler() first.")

    with key_lock:
        api_key = next(key_cycle)

    return {"_leetify_key": api_key}


# --- STATE TRACKING ---
queue = []
players_in_queue = set()
visited_players = set()
collected_steam_ids = set()
collected_data = []

profile_cache = {}
profile_cache_lock = threading.Lock()

def init_crawler(seed_id=None, max_players=1000):
    global SEED_STEAM_ID
    global queue
    global MAX_PLAYERS
    global players_in_queue
    global API_KEYS
    global key_cycle

    load_dotenv()

    API_KEYS = os.getenv("LEETIFY_API_KEYS", "").split(",")
    API_KEYS = [key.strip() for key in API_KEYS if key.strip()]

    if not API_KEYS:
        raise ValueError("Missing LEETIFY_API_KEYS in .env")

    # Rebuild key cycle after loading API keys
    key_cycle = cycle(API_KEYS)

    if seed_id is None:
        raise ValueError("Start seed Steam ID must be provided")

    SEED_STEAM_ID = seed_id
    MAX_PLAYERS = max_players

    # Reset crawler state
    queue.clear()
    players_in_queue.clear()
    visited_players.clear()
    collected_steam_ids.clear()
    collected_data.clear()
    profile_cache.clear()

    queue.append(seed_id)
    players_in_queue.add(seed_id)

    print(f"Initialized crawler with seed: {SEED_STEAM_ID} and max players: {MAX_PLAYERS}")
    print(f"Loaded {len(API_KEYS)} API keys for rotation.")

def request_json(url):
    """Generic GET request helper."""
    try:
        response = requests.get(
            url,
            headers=get_headers(),
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code == 200:
            return response.json()

        print(f"Request failed {response.status_code}: {url}")
        return None

    except Exception as e:
        print(f"Request error for {url}: {e}")
        return None


def get_player_matches(steam_id):
    """Fetch recent match IDs for a player."""
    url = f"{base_url}/v3/profile/matches?steam64_id={steam_id}"
    data = request_json(url)

    if isinstance(data, list):
        return [m["id"] for m in data if isinstance(m, dict) and "id" in m][:MATCH_LIMIT_PER_PLAYER]

    if isinstance(data, dict):
        return [
            m["id"]
            for m in data.get("matches", [])
            if isinstance(m, dict) and "id" in m
        ][:MATCH_LIMIT_PER_PLAYER]

    return []


def get_match_players(match_id):
    """Fetch all players involved in a specific match."""
    url = f"{base_url}/v2/matches/{match_id}"
    data = request_json(url)

    if not isinstance(data, dict):
        return []

    stats = data.get("stats", [])

    return [
        {
            "steamId": p.get("steam64_id"),
            "name": p.get("name"),
            "team": p.get("initial_team_number"),
            "kills": p.get("total_kills"),
            "deaths": p.get("total_deaths"),
            "kd_ratio": p.get("kd_ratio"),
            "leetify_rating": p.get("leetify_rating"),
            "damage": p.get("total_damage"),
            "rounds_count": p.get("rounds_count"),
            "accuracy_enemy_spotted": p.get("accuracy_enemy_spotted"),
            "accuracy_head": p.get("accuracy_head"),
            "spray_accuracy": p.get("spray_accuracy"),
            "counter_strafing_good_ratio": p.get("counter_strafing_shots_good_ratio"),
            "trade_kills_success_percentage": p.get("trade_kills_success_percentage"),
            "traded_deaths_success_percentage": p.get("traded_deaths_success_percentage"),
        }
        for p in stats
        if isinstance(p, dict) and p.get("steam64_id")
    ]


def get_player_profile(steam64_id):
    """Fetch full Leetify profile. Cached to avoid duplicate API calls."""
    with profile_cache_lock:
        if steam64_id in profile_cache:
            return profile_cache[steam64_id]

    url = f"{base_url}/v3/profile?steam64_id={steam64_id}"
    data = request_json(url)

    if not isinstance(data, dict):
        data = None

    with profile_cache_lock:
        profile_cache[steam64_id] = data

    return data


def get_player_premier_ranking(steam64_id):
    """Return player Premier ranking."""
    profile = get_player_profile(steam64_id)

    if not isinstance(profile, dict):
        return None

    ranks = profile.get("ranks", {})
    if not isinstance(ranks, dict):
        return None

    return ranks.get("premier")


def build_player_row(player):
    """Build one output row from match stats + full profile rating/stats."""
    steam_id = player.get("steamId")

    if not steam_id:
        return None

    profile = get_player_profile(steam_id)

    if not isinstance(profile, dict):
        return None

    ranks = profile.get("ranks", {})
    rating = ranks.get("premier") if isinstance(ranks, dict) else None

    if rating is None:
        return None

    if not (TARGET_RATING_MIN <= rating <= TARGET_RATING_MAX):
        return None

    rating_data = profile.get("rating", {})
    stats_data = profile.get("stats", {})

    if not isinstance(rating_data, dict):
        rating_data = {}

    if not isinstance(stats_data, dict):
        stats_data = {}

    return {
        # Basic player info
        "steamId": steam_id,
        "name": player.get("name"),
        "premier_rating": rating,

        # Match-level stats from /v2/matches/{match_id}
        "match_kills": player.get("kills"),
        "match_deaths": player.get("deaths"),
        "match_kd_ratio": player.get("kd_ratio"),
        "match_leetify_rating": player.get("leetify_rating"),
        "match_damage": player.get("damage"),
        "match_rounds_count": player.get("rounds_count"),

        # Profile rating fields
        "aim": rating_data.get("aim"),
        "positioning": rating_data.get("positioning"),
        "utility": rating_data.get("utility"),
        "clutch": rating_data.get("clutch"),
        "opening": rating_data.get("opening"),
        "ct_leetify": rating_data.get("ct_leetify"),
        "t_leetify": rating_data.get("t_leetify"),

        # Profile stats fields
        "accuracy_enemy_spotted": stats_data.get("accuracy_enemy_spotted"),
        "accuracy_head": stats_data.get("accuracy_head"),
        "counter_strafing_good_shots_ratio": stats_data.get("counter_strafing_good_shots_ratio"),
        "ct_opening_aggression_success_rate": stats_data.get("ct_opening_aggression_success_rate"),
        "ct_opening_duel_success_percentage": stats_data.get("ct_opening_duel_success_percentage"),
        "flashbang_hit_foe_avg_duration": stats_data.get("flashbang_hit_foe_avg_duration"),
        "flashbang_hit_foe_per_flashbang": stats_data.get("flashbang_hit_foe_per_flashbang"),
        "flashbang_hit_friend_per_flashbang": stats_data.get("flashbang_hit_friend_per_flashbang"),
        "flashbang_leading_to_kill": stats_data.get("flashbang_leading_to_kill"),
        "flashbang_thrown": stats_data.get("flashbang_thrown"),
        "he_foes_damage_avg": stats_data.get("he_foes_damage_avg"),
        "he_friends_damage_avg": stats_data.get("he_friends_damage_avg"),
        "preaim": stats_data.get("preaim"),
        "reaction_time_ms": stats_data.get("reaction_time_ms"),
        "spray_accuracy": stats_data.get("spray_accuracy"),
        "t_opening_aggression_success_rate": stats_data.get("t_opening_aggression_success_rate"),
        "t_opening_duel_success_percentage": stats_data.get("t_opening_duel_success_percentage"),
        "traded_deaths_success_percentage": stats_data.get("traded_deaths_success_percentage"),
        "trade_kill_opportunities_per_round": stats_data.get("trade_kill_opportunities_per_round"),
        "trade_kills_success_percentage": stats_data.get("trade_kills_success_percentage"),
        "utility_on_death_avg": stats_data.get("utility_on_death_avg"),
    }

def start_crawler(low_bound, high_bound, seed):
    global TARGET_RATING_MIN
    global TARGET_RATING_MAX

    TARGET_RATING_MIN = low_bound
    TARGET_RATING_MAX = high_bound

    print(f"Target rating range: {TARGET_RATING_MIN} - {TARGET_RATING_MAX}")
    print(f"Starting crawl from seed: {seed}...")
    
    while queue and len(collected_data) < MAX_PLAYERS:
        current_sid = queue.pop(0)

        if current_sid in visited_players:
            continue

        visited_players.add(current_sid)
        print(f"\nProcessing player: {current_sid}")

        # 1. Get recent matches for current player
        match_ids = get_player_matches(current_sid)
        print(f"Matches found: {len(match_ids)}")

        if not match_ids:
            continue

        # 2. Fetch match players concurrently
        all_players_from_matches = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [
                executor.submit(get_match_players, match_id)
                for match_id in match_ids
            ]

            for future in as_completed(futures):
                players = future.result()
                all_players_from_matches.extend(players)

        # 3. Deduplicate players from these matches
        candidate_players = {}
        for p in all_players_from_matches:
            sid = p.get("steamId")

            if not sid:
                continue

            if sid in visited_players:
                continue

            if sid in collected_steam_ids:
                continue

            candidate_players[sid] = p

        print(f"Candidate players found: {len(candidate_players)}")

        # 4. Fetch profiles / rankings concurrently
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(build_player_row, player): sid
                for sid, player in candidate_players.items()
            }

            for future in as_completed(futures):
                if len(collected_data) >= MAX_PLAYERS:
                    break

                sid = futures[future]

                try:
                    row = future.result()
                except Exception as e:
                    print(f"Error processing player {sid}: {e}")
                    continue

                if row is None:
                    continue

                if sid in collected_steam_ids:
                    continue

                collected_data.append(row)
                collected_steam_ids.add(sid)

                print(
                    f"Collected {len(collected_data)}/{MAX_PLAYERS}: "
                    f"{sid} | Rating: {row['premier_rating']}"
                )

                if sid not in players_in_queue:
                    players_in_queue.add(sid)
                    queue.append(sid)

    # --- SAVE DATA ---
    df = pd.DataFrame(collected_data)
    df.to_csv(f"cs2_{str(low_bound)}_to_{str(high_bound)}_size_{MAX_PLAYERS}_dataset.csv", index=False)

    print(f"\nSuccessfully collected {len(df)} players.")
    print(f"Saved to cs2_{str(low_bound)}_to_{str(high_bound)}_size_{MAX_PLAYERS}_dataset.csv")

if __name__ == "__main__":
    maxPlayers=1000
    args=sys.argv[1:]
    print(f"Received arguments: {args}")
    if len(args) < 3:
        print("Usage: python main.py <rating_min> <rating_max> <seed_steam_id>")
        print(f"Example: python main.py {TARGET_RATING_MIN} {TARGET_RATING_MAX} {SEED_STEAM_ID}")
        exit(1)
    if len(args) >= 4:
        maxPlayers = int(args[3])

    minRating = int(args[0])
    maxRating = int(args[1])
    seedID = args[2]
    print(f"Starting crawler with rating range {minRating}-{maxRating}, seed ID: {seedID}, max players: {maxPlayers}")
    init_crawler(seed_id=seedID, max_players=maxPlayers)
    start_crawler(minRating, maxRating, seedID)
    