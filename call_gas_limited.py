"""GAS CLI entry point. Import has no network side effects."""
import sys
from battlelog_gas import run_conversion

if __name__ == "__main__":
    from Blue_Archive_Battlelog_collect.config_limited import CURRENT_SEASON, SEASON_LIST
    from battlelog_safety import validate_season
    season = validate_season(sys.argv[1] if len(sys.argv) > 1 else None, SEASON_LIST, CURRENT_SEASON)
    run_conversion(season, limited=True)
    print("GAS conversion completed.")
