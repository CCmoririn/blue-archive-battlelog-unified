"""Load this application's data and call the shared six-person evaluator."""
from spreadsheet_manager import (
    get_output_sheet_cache, get_striker_list_from_sheet, get_special_list_from_sheet,
)
from battlelog_defense import suggest


def suggest_defense_teams(attacks=None, season=None, strict_pos=False):
    return suggest(get_output_sheet_cache(season), get_striker_list_from_sheet(),
                   get_special_list_from_sheet(), attacks=attacks, season=season,
                   strict_pos=strict_pos, scope='general')


def suggest_team_for_template(template_tags, attacks=None, season=None, strict_pos=False):
    # Compatibility for callers of the previous template-detail endpoint.
    # Selection is now a complete observed team matching the requested layout.
    result = suggest(get_output_sheet_cache(season), get_striker_list_from_sheet(),
                     get_special_list_from_sheet(), attacks=attacks, season=season,
                     strict_pos=strict_pos, scope='general', template_tags=template_tags)
    candidate = result['candidates'][0] if result['candidates'] else None
    return {'テンプレタグ': template_tags,
            'ピックキャラ': [{'枠': f'D{i+1}', 'キャラ': name,
                          '参考': candidate['reference'], 'キャラ勝率': None,
                          '件数': candidate['games']}
                         for i, name in enumerate(candidate['team'][:4])] if candidate else [],
            'SP案': list(candidate['team'][4:]) if candidate else [],
            'SP詳細': [], '評価': candidate, 'analysis_id': result.get('analysis_id')}
