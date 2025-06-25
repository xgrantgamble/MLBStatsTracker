from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_caching import Cache  # NEW IMPORT
import requests
from datetime import datetime, timedelta
import json
from collections import defaultdict
import logging
import os
from functools import lru_cache

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this'

# NEW: Configure Flask-Caching
cache = Cache(app, config={
    'CACHE_TYPE': 'simple',  # Use simple in-memory cache
    'CACHE_DEFAULT_TIMEOUT': 300  # Default 5 minutes
})

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MLB Stats API base URL
MLB_API_BASE = "https://statsapi.mlb.com/api/v1"

class MLBStatsAPI:
    """MLB Stats API integration class"""
    
    @staticmethod
    @cache.memoize(timeout=180)  # NEW: Cache for 3 minutes (games change frequently)
    def get_todays_games():
        """Get today's MLB games"""
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            url = f"{MLB_API_BASE}/schedule"
            params = {
                'sportId': 1,  # MLB
                'date': today,
                'hydrate': 'team,venue'
            }
            
            logger.info(f"Fetching games for {today}")
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            games = []
            if 'dates' in data and data['dates']:
                for game in data['dates'][0].get('games', []):
                    game_info = {
                        'id': game['gamePk'],
                        'away_team': game['teams']['away']['team']['name'],
                        'home_team': game['teams']['home']['team']['name'],
                        'away_id': game['teams']['away']['team']['id'],
                        'home_id': game['teams']['home']['team']['id'],
                        'status': game['status']['detailedState'].lower(),
                        'game_time': game.get('gameDate', ''),
                        'venue': game.get('venue', {}).get('name', '')
                    }
                    games.append(game_info)
                    logger.info(f"Game: {game_info['away_team']} @ {game_info['home_team']}")
            
            return games
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching today's games: {e}")
            return []
    
    @staticmethod
    @cache.memoize(timeout=86400)  # NEW: Cache for 24 hours (rosters rarely change)
    def get_team_roster(team_id):
        """Get team roster"""
        try:
            url = f"{MLB_API_BASE}/teams/{team_id}/roster"
            params = {'hydrate': 'person'}
            
            logger.info(f"Fetching roster for team {team_id}")
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            roster = {
                'batters': [],
                'pitchers': []
            }
            
            for player in data.get('roster', []):
                player_info = {
                    'id': player['person']['id'],
                    'name': player['person']['fullName'],
                    'position': player['position']['abbreviation'],
                    'jersey_number': player.get('jerseyNumber', '')
                }
                
                # Categorize as batter or pitcher
                if player['position']['type'] == 'Pitcher':
                    roster['pitchers'].append(player_info)
                else:
                    roster['batters'].append(player_info)
            
            logger.info(f"Roster for team {team_id}: {len(roster['batters'])} batters, {len(roster['pitchers'])} pitchers")
            return roster
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching roster for team {team_id}: {e}")
            return {'batters': [], 'pitchers': []}
    
    @staticmethod
    @cache.memoize(timeout=21600)  # NEW: Cache for 6 hours (stats update daily)
    def get_player_stats(player_id, stat_type='hitting', days=7):
        """Get player stats for the last N days using game logs"""
        try:
            # Use game logs for rolling averages
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            
            url = f"{MLB_API_BASE}/people/{player_id}/stats"
            params = {
                'stats': 'gameLog',
                'group': stat_type,
                'startDate': start_date.strftime('%Y-%m-%d'),
                'endDate': end_date.strftime('%Y-%m-%d'),
                'season': 2025
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if not data.get('stats') or not data['stats'][0].get('splits'):
                logger.warning(f"No {days}-day stats found for player {player_id}")
                # Fallback to season stats if no recent games
                return MLBStatsAPI.get_season_stats(player_id, stat_type)
            
            # Aggregate stats from game logs
            if stat_type == 'hitting':
                return MLBStatsAPI._aggregate_hitting_stats(data['stats'][0]['splits'])
            else:
                return MLBStatsAPI._aggregate_pitching_stats(data['stats'][0]['splits'])
                
        except (requests.exceptions.RequestException, ValueError, TypeError) as e:
            logger.error(f"Error fetching {days}-day stats for player {player_id}: {e}")
            # Fallback to season stats
            return MLBStatsAPI.get_season_stats(player_id, stat_type)
    
    @staticmethod
    @cache.memoize(timeout=43200)  # NEW: Cache for 12 hours (season stats change slowly)
    def get_season_stats(player_id, stat_type='hitting'):
        """Get player season stats as fallback"""
        try:
            url = f"{MLB_API_BASE}/people/{player_id}/stats"
            params = {
                'stats': 'season',
                'group': stat_type,
                'season': 2025
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if not data.get('stats') or not data['stats'][0].get('splits'):
                return {}
            
            season_stats = data['stats'][0]['splits'][0]['stat']
            
            if stat_type == 'hitting':
                return MLBStatsAPI._format_hitting_stats(season_stats)
            else:
                return MLBStatsAPI._format_pitching_stats(season_stats)
                
        except Exception as e:
            logger.error(f"Error fetching season stats for player {player_id}: {e}")
            return {}
    
    @staticmethod
    def _aggregate_hitting_stats(game_logs):
        """Aggregate hitting stats from game logs"""
        try:
            totals = {
                'at_bats': 0,
                'hits': 0,
                'home_runs': 0,
                'rbis': 0,
                'walks': 0,
                'strikeouts': 0,
                'total_bases': 0
            }
            
            for game in game_logs:
                stats = game.get('stat', {})
                totals['at_bats'] += int(stats.get('atBats', 0))
                totals['hits'] += int(stats.get('hits', 0))
                totals['home_runs'] += int(stats.get('homeRuns', 0))
                totals['rbis'] += int(stats.get('rbi', 0))
                totals['walks'] += int(stats.get('baseOnBalls', 0))
                totals['strikeouts'] += int(stats.get('strikeOuts', 0))
                totals['total_bases'] += int(stats.get('totalBases', 0))
            
            # Calculate derived stats
            avg = totals['hits'] / totals['at_bats'] if totals['at_bats'] > 0 else 0
            obp = (totals['hits'] + totals['walks']) / (totals['at_bats'] + totals['walks']) if (totals['at_bats'] + totals['walks']) > 0 else 0
            slg = totals['total_bases'] / totals['at_bats'] if totals['at_bats'] > 0 else 0
            ops = obp + slg
            
            return {
                'avg': f"{avg:.3f}",
                'obp': f"{obp:.3f}",
                'slg': f"{slg:.3f}",
                'ops': f"{ops:.3f}",
                'ab': totals['at_bats'],
                'h': totals['hits'],
                'hr': totals['home_runs'],
                'rbi': totals['rbis'],
                'bb': totals['walks'],
                'so': totals['strikeouts']
            }
        except (ValueError, TypeError, ZeroDivisionError) as e:
            logger.error(f"Error aggregating hitting stats: {e}")
            return {
                'avg': '.000', 'obp': '.000', 'slg': '.000', 'ops': '.000',
                'ab': 0, 'h': 0, 'hr': 0, 'rbi': 0, 'bb': 0, 'so': 0
            }
    
    @staticmethod
    def _aggregate_pitching_stats(game_logs):
        """Aggregate pitching stats from game logs"""
        try:
            totals = {
                'innings_pitched': 0.0,
                'hits': 0,
                'earned_runs': 0,
                'walks': 0,
                'strikeouts': 0,
                'home_runs': 0,
                'saves': 0,
                'games_started': 0
            }
            
            for game in game_logs:
                stats = game.get('stat', {})
                # Convert innings pitched from string format (e.g., "6.1" means 6 and 1/3 innings)
                ip_str = str(stats.get('inningsPitched', '0'))
                if '.' in ip_str:
                    whole, third = ip_str.split('.')
                    totals['innings_pitched'] += int(whole) + (int(third) / 3.0)
                else:
                    totals['innings_pitched'] += float(ip_str)
                
                totals['hits'] += int(stats.get('hits', 0))
                totals['earned_runs'] += int(stats.get('earnedRuns', 0))
                totals['walks'] += int(stats.get('baseOnBalls', 0))
                totals['strikeouts'] += int(stats.get('strikeOuts', 0))
                totals['home_runs'] += int(stats.get('homeRuns', 0))
                totals['saves'] += int(stats.get('saves', 0))
                totals['games_started'] += int(stats.get('gamesStarted', 0))
            
            # Calculate derived stats
            era = (totals['earned_runs'] * 9) / totals['innings_pitched'] if totals['innings_pitched'] > 0 else 0
            whip = (totals['walks'] + totals['hits']) / totals['innings_pitched'] if totals['innings_pitched'] > 0 else 0
            
            return {
                'era': f"{era:.2f}",
                'whip': f"{whip:.2f}",
                'k': totals['strikeouts'],
                'bb': totals['walks'],
                'ip': f"{totals['innings_pitched']:.1f}",
                'h': totals['hits'],
                'hr': totals['home_runs'],
                'sv': totals['saves'],
                'gs': totals['games_started'],
                'er': totals['earned_runs']
            }
        except (ValueError, TypeError, ZeroDivisionError) as e:
            logger.error(f"Error aggregating pitching stats: {e}")
            return {
                'era': '0.00', 'whip': '0.00', 'k': 0, 'bb': 0, 'ip': '0.0',
                'h': 0, 'hr': 0, 'sv': 0, 'gs': 0, 'er': 0
            }
    
    @staticmethod
    def _format_hitting_stats(stats):
        """Format hitting stats from API response"""
        try:
            at_bats = int(stats.get('atBats', 0))
            hits = int(stats.get('hits', 0))
            total_bases = int(stats.get('totalBases', 0))
            walks = int(stats.get('baseOnBalls', 0))
            
            # Calculate derived stats
            avg = hits / at_bats if at_bats > 0 else 0
            obp = (hits + walks) / (at_bats + walks) if (at_bats + walks) > 0 else 0
            slg = total_bases / at_bats if at_bats > 0 else 0
            ops = obp + slg
            
            return {
                'avg': f"{avg:.3f}",
                'obp': f"{obp:.3f}",
                'slg': f"{slg:.3f}",
                'ops': f"{ops:.3f}",
                'ab': at_bats,
                'h': hits,
                'hr': int(stats.get('homeRuns', 0)),
                'rbi': int(stats.get('rbi', 0)),
                'bb': walks,
                'so': int(stats.get('strikeOuts', 0))
            }
        except (ValueError, TypeError, ZeroDivisionError) as e:
            logger.error(f"Error formatting hitting stats: {e}")
            return {
                'avg': '.000', 'obp': '.000', 'slg': '.000', 'ops': '.000',
                'ab': 0, 'h': 0, 'hr': 0, 'rbi': 0, 'bb': 0, 'so': 0
            }
    
    @staticmethod
    def _format_pitching_stats(stats):
        """Format pitching stats from API response"""
        try:
            innings_pitched = float(stats.get('inningsPitched', '0') or '0')
            hits = int(stats.get('hits', 0))
            earned_runs = int(stats.get('earnedRuns', 0))
            walks = int(stats.get('baseOnBalls', 0))
            
            # Calculate derived stats
            era = (earned_runs * 9) / innings_pitched if innings_pitched > 0 else 0
            whip = (walks + hits) / innings_pitched if innings_pitched > 0 else 0
            
            return {
                'era': f"{era:.2f}",
                'whip': f"{whip:.2f}",
                'k': int(stats.get('strikeOuts', 0)),
                'bb': walks,
                'ip': f"{innings_pitched:.1f}",
                'h': hits,
                'hr': int(stats.get('homeRuns', 0)),
                'sv': int(stats.get('saves', 0)),
                'gs': int(stats.get('gamesStarted', 0)),
                'er': earned_runs
            }
        except (ValueError, TypeError, ZeroDivisionError) as e:
            logger.error(f"Error formatting pitching stats: {e}")
            return {
                'era': '0.00', 'whip': '0.00', 'k': 0, 'bb': 0, 'ip': '0.0',
                'h': 0, 'hr': 0, 'sv': 0, 'gs': 0, 'er': 0
            }

    @staticmethod
    @cache.memoize(timeout=21600)  # NEW: Cache for 6 hours (team stats update daily)
    def get_team_stats(team_id, season=2025):
        """Get team season stats"""
        try:
            url = f"{MLB_API_BASE}/teams/{team_id}/stats"
            params = {
                'stats': 'season',
                'group': 'hitting,pitching',
                'season': season
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            team_stats = {
                'AVG': '.000',
                'OBP': '.000', 
                'SLG': '.000',
                'HR': '0',
                'ERA': '0.00',
                'WHIP': '0.00',
                'SV': '0'
            }
            
            for stat_group in data.get('stats', []):
                group_name = stat_group.get('group', {}).get('displayName', '')
                if group_name == 'hitting' and stat_group.get('splits'):
                    hitting_stats = stat_group['splits'][0]['stat']
                    # Safely convert to float then format
                    avg_val = float(hitting_stats.get('avg', 0))
                    obp_val = float(hitting_stats.get('obp', 0))
                    slg_val = float(hitting_stats.get('slg', 0))
                    
                    team_stats['AVG'] = f"{avg_val:.3f}"
                    team_stats['OBP'] = f"{obp_val:.3f}"
                    team_stats['SLG'] = f"{slg_val:.3f}"
                    team_stats['HR'] = str(hitting_stats.get('homeRuns', 0))
                elif group_name == 'pitching' and stat_group.get('splits'):
                    pitching_stats = stat_group['splits'][0]['stat']
                    # Safely convert to float then format
                    era_val = float(pitching_stats.get('era', 0))
                    whip_val = float(pitching_stats.get('whip', 0))
                    
                    team_stats['ERA'] = f"{era_val:.2f}"
                    team_stats['WHIP'] = f"{whip_val:.2f}"
                    team_stats['SV'] = str(pitching_stats.get('saves', 0))
            
            logger.info(f"Team {team_id} stats: {team_stats}")
            return team_stats
            
        except (requests.exceptions.RequestException, ValueError, TypeError) as e:
            logger.error(f"Error fetching team stats for {team_id}: {e}")
            return {
                'AVG': '.245', 'OBP': '.315', 'SLG': '.425', 'HR': '15',
                'ERA': '4.15', 'WHIP': '1.32', 'SV': '5'
            }

# Flask Routes (unchanged except for potential cache clearing)
@app.route('/')
def home():
    """Home page showing today's games"""
    games = MLBStatsAPI.get_todays_games()
    favorites = session.get('favorites', [])
    
    # Format current date
    current_date = datetime.now().strftime('%A, %B %d, %Y')
    
    # Format game times for display
    for game in games:
        if game['game_time']:
            try:
                dt = datetime.fromisoformat(game['game_time'].replace('Z', '+00:00'))
                game['formatted_time'] = dt.strftime('%I:%M %p ET')
            except:
                game['formatted_time'] = 'TBD'
        else:
            game['formatted_time'] = 'TBD'
        
        # Check if game is postponed
        if 'postponed' in game['status'] or 'suspended' in game['status']:
            game['formatted_time'] = 'Postponed'
            game['status'] = 'postponed'
    
    return render_template('home.html', games=games, favorites=favorites, current_date=current_date)

@app.route('/api/games/today')
def api_todays_games():
    """API endpoint for today's games"""
    games = MLBStatsAPI.get_todays_games()
    return jsonify(games)

# NEW: Optional cache clearing endpoint for debugging
@app.route('/admin/clear-cache')
def clear_cache():
    """Clear all cached data - useful for debugging"""
    cache.clear()
    return jsonify({"message": "Cache cleared successfully"})

@app.route('/details/<int:home_id>/<int:away_id>')
def view_details(home_id, away_id):
    """Team details page"""
    logger.info(f"Loading details for {away_id} @ {home_id}")
    
    # Format current date
    current_date = datetime.now().strftime('%A, %B %d, %Y')
    
    # Get team rosters
    home_roster = MLBStatsAPI.get_team_roster(home_id)
    away_roster = MLBStatsAPI.get_team_roster(away_id)
    
    # Get team names
    home_team_name = get_team_name(home_id)
    away_team_name = get_team_name(away_id)
    
    # Get team stats
    home_team_stats = MLBStatsAPI.get_team_stats(home_id)
    away_team_stats = MLBStatsAPI.get_team_stats(away_id)
    
    # Build team data
    home_team = build_team_data(home_team_name, home_roster, home_team_stats)
    away_team = build_team_data(away_team_name, away_roster, away_team_stats)
    
    favorites = session.get('favorites', [])
    
    return render_template('details.html', 
                         home_team=home_team, 
                         away_team=away_team, 
                         favorites=favorites,
                         current_date=current_date)

@app.route('/favorites', methods=['POST'])
def toggle_favorite():
    """Toggle team favorite status"""
    team_name = request.form.get('favorite')
    favorites = session.get('favorites', [])
    
    if team_name in favorites:
        favorites.remove(team_name)
    else:
        favorites.append(team_name)
    
    session['favorites'] = favorites
    return redirect(request.referrer or url_for('home'))

@app.route('/reset_favorites', methods=['POST'])
def reset_favorites():
    """Reset all favorites"""
    session['favorites'] = []
    return redirect(url_for('home'))

# Helper Functions (unchanged)
def get_team_name(team_id):
    """Get team name by ID"""
    team_names = {
        108: "Los Angeles Angels", 109: "Arizona Diamondbacks", 110: "Baltimore Orioles",
        111: "Boston Red Sox", 112: "Detroit Tigers", 113: "Kansas City Royals",
        114: "Milwaukee Brewers", 115: "Minnesota Twins", 116: "New York Yankees",
        117: "Oakland Athletics", 118: "Seattle Mariners", 119: "Los Angeles Dodgers",
        120: "Washington Nationals", 121: "New York Mets", 133: "Houston Astros",
        134: "Pittsburgh Pirates", 135: "San Diego Padres", 137: "San Francisco Giants",
        138: "St. Louis Cardinals", 139: "Tampa Bay Rays", 140: "Texas Rangers",
        141: "Toronto Blue Jays", 142: "Minnesota Twins", 143: "Philadelphia Phillies",
        144: "Atlanta Braves", 145: "Chicago White Sox", 146: "Miami Marlins",
        147: "New York Yankees", 158: "Milwaukee Brewers", 159: "Miami Marlins",
        160: "Chicago Cubs", 161: "Cincinnati Reds", 162: "Colorado Rockies"
    }
    return team_names.get(team_id, f"Team {team_id}")

def calculate_rolling_team_stats(batters, pitchers, period):
    """Calculate team rolling averages from player stats"""
    try:
        # Initialize totals for batting
        batting_totals = {
            'total_at_bats': 0,
            'total_hits': 0,
            'total_walks': 0,
            'total_total_bases': 0,
            'total_home_runs': 0,
            'valid_batters': 0
        }
        
        # Sum up batting stats from all players
        for batter in batters:
            batter_stats = batter.get('stats', {}).get(str(period), {})
            if batter_stats and batter_stats.get('ab', 0) > 0:  # Only count players with at-bats
                batting_totals['total_at_bats'] += batter_stats.get('ab', 0)
                batting_totals['total_hits'] += batter_stats.get('h', 0)
                batting_totals['total_walks'] += batter_stats.get('bb', 0)
                batting_totals['total_home_runs'] += batter_stats.get('hr', 0)
                # Calculate total bases from SLG if available
                slg = float(batter_stats.get('slg', '0').replace('.', '0.') if '.' not in batter_stats.get('slg', '0') else batter_stats.get('slg', '0'))
                total_bases = slg * batter_stats.get('ab', 0)
                batting_totals['total_total_bases'] += total_bases
                batting_totals['valid_batters'] += 1
        
        # Calculate team batting averages
        team_avg = batting_totals['total_hits'] / batting_totals['total_at_bats'] if batting_totals['total_at_bats'] > 0 else 0
        team_obp = (batting_totals['total_hits'] + batting_totals['total_walks']) / (batting_totals['total_at_bats'] + batting_totals['total_walks']) if (batting_totals['total_at_bats'] + batting_totals['total_walks']) > 0 else 0
        team_slg = batting_totals['total_total_bases'] / batting_totals['total_at_bats'] if batting_totals['total_at_bats'] > 0 else 0
        
        # Initialize totals for pitching
        pitching_totals = {
            'total_innings': 0.0,
            'total_earned_runs': 0,
            'total_hits_allowed': 0,
            'total_walks_allowed': 0,
            'valid_pitchers': 0
        }
        
        # Sum up pitching stats from all players
        for pitcher in pitchers:
            pitcher_stats = pitcher.get('stats', {}).get(str(period), {})
            if pitcher_stats and float(pitcher_stats.get('ip', '0')) > 0:  # Only count pitchers with innings
                ip_str = str(pitcher_stats.get('ip', '0.0'))
                innings = float(ip_str)
                pitching_totals['total_innings'] += innings
                pitching_totals['total_earned_runs'] += pitcher_stats.get('er', 0)
                pitching_totals['total_hits_allowed'] += pitcher_stats.get('h', 0)
                pitching_totals['total_walks_allowed'] += pitcher_stats.get('bb', 0)
                pitching_totals['valid_pitchers'] += 1
        
        # Calculate team pitching averages
        team_era = (pitching_totals['total_earned_runs'] * 9) / pitching_totals['total_innings'] if pitching_totals['total_innings'] > 0 else 0
        team_whip = (pitching_totals['total_hits_allowed'] + pitching_totals['total_walks_allowed']) / pitching_totals['total_innings'] if pitching_totals['total_innings'] > 0 else 0
        
        # Calculate average hits and strikeouts per game
        avg_hits = batting_totals['total_hits'] / batting_totals['valid_batters'] if batting_totals['valid_batters'] > 0 else 0
        
        # Calculate average strikeouts from pitchers
        total_strikeouts = 0
        for pitcher in pitchers:
            pitcher_stats = pitcher.get('stats', {}).get(str(period), {})
            if pitcher_stats and float(pitcher_stats.get('ip', '0')) > 0:
                total_strikeouts += pitcher_stats.get('k', 0)
        
        avg_k = total_strikeouts / pitching_totals['valid_pitchers'] if pitching_totals['valid_pitchers'] > 0 else 0
        
        return {
            'AVG': f"{team_avg:.3f}",
            'OBP': f"{team_obp:.3f}",
            'SLG': f"{team_slg:.3f}",
            'HR': str(batting_totals['total_home_runs']),
            'AVG_HITS': f"{avg_hits:.1f}",
            'AVG_K': f"{avg_k:.1f}"
        }
        
    except Exception as e:
        logger.error(f"Error calculating rolling team stats for {period} days: {e}")
        return {
            'AVG': '.000',
            'OBP': '.000', 
            'SLG': '.000',
            'HR': '0',
            'ERA': '0.00',
            'WHIP': '0.00'
        }

# Change your build_team_data function to load stats lazily
def build_team_data(team_name, roster, team_stats):
    """Build comprehensive team data with lazy loading"""
    logger.info(f"Building team data for {team_name}")
    
    team_data = {
        'name': team_name,
        'lineup': [],
        'fullRoster': {
            'batters': roster['batters'][:10],  # Limit to first 10 players initially
            'pitchers': roster['pitchers'][:10]
        },
        'starter': {},
        'teamStats': {
            '7': team_stats,
            '10': team_stats,
            '21': team_stats
        },
        'rollingTeamStats': {
            '7': {'AVG': '.250', 'OBP': '.320', 'SLG': '.400', 'HR': '10', 'AVG_HITS': '4.2', 'AVG_K': '3.8'},
            '10': {'AVG': '.248', 'OBP': '.318', 'SLG': '.395', 'HR': '12', 'AVG_HITS': '4.0', 'AVG_K': '4.1'},
            '21': {'AVG': '.245', 'OBP': '.315', 'SLG': '.390', 'HR': '15', 'AVG_HITS': '3.8', 'AVG_K': '4.3'}
        }
    }
    
    # Only process a few key players to avoid timeout
    for i, batter in enumerate(roster['batters'][:5]):  # Only first 5 batters
        logger.info(f"Getting stats for key batter: {batter['name']}")
        
        # Get stats for each rolling period
        stats_7d = MLBStatsAPI.get_player_stats(batter['id'], 'hitting', 7)
        stats_10d = MLBStatsAPI.get_player_stats(batter['id'], 'hitting', 10)
        stats_21d = MLBStatsAPI.get_player_stats(batter['id'], 'hitting', 21)
        
        # Add stats to player
        batter['stats'] = {
            '7': stats_7d,
            '10': stats_10d,
            '21': stats_21d
        }
        
        team_data['lineup'].append(batter)
    
    # Add remaining batters without stats (for display only)
    for batter in roster['batters'][5:10]:
        batter['stats'] = {
            '7': {'avg': '.000', 'obp': '.000', 'slg': '.000', 'hr': 0, 'rbi': 0, 'h': 0, 'ab': 0},
            '10': {'avg': '.000', 'obp': '.000', 'slg': '.000', 'hr': 0, 'rbi': 0, 'h': 0, 'ab': 0},
            '21': {'avg': '.000', 'obp': '.000', 'slg': '.000', 'hr': 0, 'rbi': 0, 'h': 0, 'ab': 0}
        }
    
    # Process only key pitchers
    for pitcher in roster['pitchers'][:3]:  # Only first 3 pitchers
        logger.info(f"Getting stats for key pitcher: {pitcher['name']}")
        
        stats_7d = MLBStatsAPI.get_player_stats(pitcher['id'], 'pitching', 7)
        stats_10d = MLBStatsAPI.get_player_stats(pitcher['id'], 'pitching', 10)
        stats_21d = MLBStatsAPI.get_player_stats(pitcher['id'], 'pitching', 21)
        
        pitcher['stats'] = {
            '7': stats_7d,
            '10': stats_10d,
            '21': stats_21d
        }
    
    # Add remaining pitchers without stats
    for pitcher in roster['pitchers'][3:10]:
        pitcher['stats'] = {
            '7': {'era': '0.00', 'whip': '0.00', 'k': 0, 'bb': 0, 'ip': '0.0', 'gs': 0, 'sv': 0, 'er': 0, 'h': 0, 'hr': 0},
            '10': {'era': '0.00', 'whip': '0.00', 'k': 0, 'bb': 0, 'ip': '0.0', 'gs': 0, 'sv': 0, 'er': 0, 'h': 0, 'hr': 0},
            '21': {'era': '0.00', 'whip': '0.00', 'k': 0, 'bb': 0, 'ip': '0.0', 'gs': 0, 'sv': 0, 'er': 0, 'h': 0, 'hr': 0}
        }
    
    # Set probable starter
    if roster['pitchers']:
        team_data['starter'] = roster['pitchers'][0]
    
    logger.info(f"Team data built for {team_name}: {len(team_data['lineup'])} key players loaded")
    return team_data

# Also update the port configuration at the bottom
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)

