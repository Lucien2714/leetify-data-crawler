import requests
import pandas as pd
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

# Target Rating System:
# 0 = Premier rating
# 1 = Faceit level
TARGET_RATING_SYSTEM = 0

MAX_PLAYERS = 1000

MATCH_LIMIT_PER_PLAYER = 10
MAX_WORKERS = 8
REQUEST_TIMEOUT = 15
CHECKPOINT_INTERVAL = 50

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
        return [
            m["id"]
            for m in data
            if isinstance(m, dict) and "id" in m
        ][:MATCH_LIMIT_PER_PLAYER]

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


def get_target_rating_from_profile(profile):
    """Return target rating based on TARGET_RATING_SYSTEM.

    TARGET_RATING_SYSTEM:
    0 = Premier rating
    1 = Faceit level
    """
    if not isinstance(profile, dict):
        return None

    ranks = profile.get("ranks", {})
    if not isinstance(ranks, dict):
        return None

    if TARGET_RATING_SYSTEM == 0:
        return ranks.get("premier")

    if TARGET_RATING_SYSTEM == 1:
        return ranks.get("faceit")

    raise ValueError(f"Invalid TARGET_RATING_SYSTEM: {TARGET_RATING_SYSTEM}")


def get_target_rating_name():
    """Return readable rating system name."""
    if TARGET_RATING_SYSTEM == 0:
        return "premier"

    if TARGET_RATING_SYSTEM == 1:
        return "faceit"

    return "unknown"


def meets_criteria(profile):
    """Check if a player's selected rating system meets the target range."""
    rating = get_target_rating_from_profile(profile)

    if rating is None:
        return False

    return TARGET_RATING_MIN <= rating <= TARGET_RATING_MAX


def build_player_row(player):
    """Build one player-level output row from full profile rating/stats."""
    steam_id = player.get("steamId")

    if not steam_id:
        return None

    profile = get_player_profile(steam_id)

    if not isinstance(profile, dict):
        return None

    ranks = profile.get("ranks", {})
    if not isinstance(ranks, dict):
        ranks = {}

    target_rating = get_target_rating_from_profile(profile)

    if target_rating is None:
        return None

    if not meets_criteria(profile):
        return None

    rating_data = profile.get("rating", {})
    stats_data = profile.get("stats", {})

    if not isinstance(rating_data, dict):
        rating_data = {}

    if not isinstance(stats_data, dict):
        stats_data = {}

    bans = profile.get("bans", [])
    if not isinstance(bans, list):
        bans = []

    return {
        # Basic player info
        "steamId": steam_id,
        "name": player.get("name"),

        # Profile metadata from /v3/profile
        "privacy_mode": profile.get("privacy_mode"),
        "winrate": profile.get("winrate"),
        "total_matches": profile.get("total_matches"),
        "first_match_date": profile.get("first_match_date"),
        "is_banned": len(bans) > 0,
        "ban_count": len(bans),

        # Target rating info
        "target_rating_system": get_target_rating_name(),
        "target_rating": target_rating,

        # All ranking systems available from profile
        "premier_rating": ranks.get("premier"),
        "faceit_level": ranks.get("faceit"),
        "faceit_elo": ranks.get("faceit_elo"),
        "leetify_rank": ranks.get("leetify"),
        "wingman_rank": ranks.get("wingman"),
        "renown_rank": ranks.get("renown"),


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


def make_output_filename(system_name, low_bound, high_bound, max_players, checkpoint=False):
    """Create a consistent output filename for final and checkpoint CSV files."""
    suffix = "checkpoint" if checkpoint else "dataset"
    return (
        f"cs2_{system_name}_{low_bound}_to_{high_bound}"
        f"_size_{max_players}_{suffix}.csv"
    )


def save_collected_data(output_file):
    """Save currently collected player rows to CSV."""
    df = pd.DataFrame(collected_data)
    df.to_csv(output_file, index=False)
    return len(df)


def start_crawler(low_bound, high_bound, seed, rating_system):
    global TARGET_RATING_MIN
    global TARGET_RATING_MAX
    global TARGET_RATING_SYSTEM

    TARGET_RATING_MIN = low_bound
    TARGET_RATING_MAX = high_bound
    TARGET_RATING_SYSTEM = rating_system

    system_name = get_target_rating_name()
    checkpoint_file = make_output_filename(
        system_name,
        low_bound,
        high_bound,
        MAX_PLAYERS,
        checkpoint=True,
    )

    print(f"Target rating system: {system_name}")
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
                    f"{sid} | {row['target_rating_system']}: {row['target_rating']}"
                )

                if len(collected_data) % CHECKPOINT_INTERVAL == 0:
                    rows_saved = save_collected_data(checkpoint_file)
                    print(f"Checkpoint saved: {checkpoint_file} ({rows_saved} rows)")

                if sid not in players_in_queue:
                    players_in_queue.add(sid)
                    queue.append(sid)

    # --- SAVE FINAL DATA ---
    output_file = make_output_filename(
        system_name,
        low_bound,
        high_bound,
        MAX_PLAYERS,
        checkpoint=False,
    )
    rows_saved = save_collected_data(output_file)

    print(f"\nSuccessfully collected {rows_saved} players.")
    print(f"Saved to {output_file}")


if __name__ == "__main__":
    maxPlayers = 1000
    args = sys.argv[1:]

    print(f"Received arguments: {args}")

    if len(args) < 4:
        print("Usage: uv run main.py <rating_system> <rating_min> <rating_max> <seed_steam_id> [max_players]")
        print("rating_system: 0 = Premier rating, 1 = Faceit level")
        print("Example Premier: uv run main.py 0 7500 15000 76561199039719492 1000")
        print("Example Faceit: uv run main.py 1 8 10 76561199039719492 1000")
        exit(1)

    ratingSystem = int(args[0])
    minRating = int(args[1])
    maxRating = int(args[2])
    seedID = args[3]

    if len(args) >= 5:
        maxPlayers = int(args[4])

    if ratingSystem not in (0, 1):
        raise ValueError("rating_system must be 0 for Premier or 1 for Faceit")

    systemName = "Premier" if ratingSystem == 0 else "Faceit"

    print(
        f"Starting crawler with {systemName} range {minRating}-{maxRating}, "
        f"seed ID: {seedID}, max players: {maxPlayers}"
    )

    init_crawler(seed_id=seedID, max_players=maxPlayers)
    start_crawler(minRating, maxRating, seedID, ratingSystem)
