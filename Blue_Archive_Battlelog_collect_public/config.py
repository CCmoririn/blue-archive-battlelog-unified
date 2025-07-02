# config.py
# シーズン分割・シート/キャッシュ一元管理用

# 現行シーズン（シート名/キャッシュ名に完全一致）
CURRENT_SEASON = "s9"

# 利用可能な全シーズンリスト
SEASON_LIST = [
    {"key": "s9", "label": "s9"},
    # 必要に応じて追加
]

# キャッシュファイルのディレクトリ（ルートからの相対パス）
CACHE_DIR = "cache_general"

# スプレッドシートの「戦闘ログ」用シート名（＝シーズン名と一致が前提）
# 他に共通で使う名前・IDがあればここでまとめて定義

# 現行シーズンを初期選択するヘルパー
def get_current_season_label():
    for season in SEASON_LIST:
        if season["key"] == CURRENT_SEASON:
            return season["label"]
    return CURRENT_SEASON
