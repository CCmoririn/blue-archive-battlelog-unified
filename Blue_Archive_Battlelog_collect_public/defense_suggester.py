import time
from collections import defaultdict
from spreadsheet_manager import (
    get_output_sheet_cache,
    get_striker_list_from_sheet,
    get_special_list_from_sheet
)

SUGGEST_CONFIG = {
    "FORCED_SP": "シロコ（水着）",
    "CHAR_MIN_GAMES": 30,
    "PRIOR_GAMES": 8
}
UNKNOWN_SAMPLE_WR = 0.3  # サンプル0の場合に使う勝率

def boolify(val):
    if isinstance(val, bool):
        return val
    return str(val).strip().upper() == "TRUE"

def safe_int(val):
    try: return int(val)
    except Exception: return 0

def load_striker_master():
    raw = get_striker_list_from_sheet()
    m = {}
    for c in raw:
        m[c["name"]] = {
            "射程": safe_int(c.get("射程")),
            "遮蔽": boolify(c.get("遮蔽", False)),
            "image": c.get("image")
        }
    return m

def load_special_master():
    raw = get_special_list_from_sheet()
    return {c["name"]: {"image": c.get("image")} for c in raw}

def load_battlelog(season=None):
    return get_output_sheet_cache(season)

def filter_records_by_attack_strikers(records, attack, strict_pos=False):
    if not attack:
        return []
    filtered = []
    for r in records:
        # --- ストライカー一致判定 ---
        if strict_pos:
            match = True
            for i in range(4):
                if attack[i]:
                    if r.get(f"A{i+1}", "") != attack[i]:
                        match = False
                        break
            if not match:
                continue
        else:
            atk_chars = [c for c in attack[:4] if c]
            row_chars = [r.get(f"A{i+1}", "") for i in range(4)]
            if any(c and c not in row_chars for c in atk_chars):
                continue
        # --- スペシャル一致判定 ---
        asp_input = [attack[4] if len(attack) > 4 else "", attack[5] if len(attack) > 5 else ""]
        asp_input_nonempty = [x for x in asp_input if x]
        row_sp = [r.get("ASP1", ""), r.get("ASP2", "")]
        if strict_pos:
            for i in range(2):
                if asp_input[i]:
                    if row_sp[i] != asp_input[i]:
                        break
            else:
                filtered.append(r)
                continue
            continue
        else:
            if any(sp and sp not in row_sp for sp in asp_input_nonempty):
                continue
        filtered.append(r)
    return filtered

def get_striker_templates(records, master):
    templates = defaultdict(lambda: {"games": 0, "wins": 0, "rows": []})
    for r in records:
        tags = []
        for i in range(4):
            info = master.get(r.get(f"D{i+1}", ""))
            if not info:
                tags = []
                break
            tags.append((info["射程"], info["遮蔽"]))
        if len(tags) != 4:
            continue
        tpl = tuple(tags)
        tpl_data = templates[tpl]
        tpl_data["games"] += 1
        if r.get("勝敗_2") == "Win":
            tpl_data["wins"] += 1
        tpl_data["rows"].append(r)
    return templates

def bayes_wr(wins, games, prior_games, prior_wr):
    return (wins + prior_games * prior_wr) / (games + prior_games) if (games + prior_games) > 0 else 0

def get_global_counts(records, fields):
    counts = defaultdict(int)
    for r in records:
        for f in fields:
            name = r.get(f, "")
            if name:
                counts[name] += 1
    return counts

def pick_strikers_for_template(tpl, tpl_data, striker_master, global_striker_counts, char_min_games, prior_games, overall_wr):
    picked_strikers = []
    slot_counts = defaultdict(int)
    for row in tpl_data["rows"]:
        for idx in range(4):
            name = row.get(f"D{idx+1}", "")
            if name:
                slot_counts[(idx, name)] += 1
    for idx, tag in enumerate(tpl):
        char_stats = defaultdict(lambda: {"games": 0, "wins": 0})
        for row in tpl_data["rows"]:
            name = row.get(f"D{idx+1}", "")
            info = striker_master.get(name)
            if info and (info["射程"], info["遮蔽"]) == tag:
                char_stats[name]["games"] += 1
                if row.get("勝敗_2") == "Win":
                    char_stats[name]["wins"] += 1
        candidates = {n: s for n, s in char_stats.items() if global_striker_counts[n] >= char_min_games}
        if candidates:
            def char_wr(s): return bayes_wr(s["wins"], s["games"], prior_games, overall_wr)
            best_char, stat = max(candidates.items(), key=lambda kv: char_wr(kv[1]))
            picked_strikers.append({
                "枠": f"D{idx+1}",
                "キャラ": best_char,
                "キャラ勝率": round(char_wr(stat), 3),
                "候補数": len(candidates),
                "件数": stat["games"],  # タグ内件数
                "参考": global_striker_counts[best_char] < char_min_games,
                "タグ": tag
            })
        else:
            if char_stats:
                best_char, stat = max(char_stats.items(), key=lambda kv: kv[1]["games"])
                picked_strikers.append({
                    "枠": f"D{idx+1}",
                    "キャラ": best_char,
                    "キャラ勝率": None,
                    "候補数": 0,
                    "件数": stat["games"],
                    "参考": global_striker_counts[best_char] < char_min_games,
                    "タグ": tag
                })
            else:
                picked_strikers.append({
                    "枠": f"D{idx+1}",
                    "キャラ": "候補なし",
                    "キャラ勝率": None,
                    "候補数": 0,
                    "件数": 0,
                    "参考": True,
                    "タグ": tag
                })
    return picked_strikers

def pick_sp_for_template(tpl_data_rows, forced_sp, char_min_games, prior_games, overall_wr, global_sp_counts):
    sp_stats = defaultdict(lambda: {"games": 0, "wins": 0})
    for row in tpl_data_rows:
        for f in ["DSP1", "DSP2"]:
            name = row.get(f, "")
            if name and name != forced_sp:
                sp_stats[name]["games"] += 1
                if row.get("勝敗_2") == "Win":
                    sp_stats[name]["wins"] += 1
    candidates = {n: s for n, s in sp_stats.items() if global_sp_counts.get(n, 0) >= char_min_games}
    sp_detail = []
    picked_sp = [forced_sp]
    if candidates:
        def sp_wr(s): return bayes_wr(s["wins"], s["games"], prior_games, overall_wr)
        best_sp, stat = max(candidates.items(), key=lambda kv: sp_wr(kv[1]))
        picked_sp.append(best_sp)
    elif sp_stats:
        best_sp, stat = max(sp_stats.items(), key=lambda kv: kv[1]["games"])
        picked_sp.append(best_sp)
    else:
        picked_sp.append("候補なし")
    for n, s in sorted(sp_stats.items(), key=lambda kv: kv[1]["games"], reverse=True):
        sp_detail.append({
            "name": n,
            "games": s["games"],
            "wins": s["wins"],
            "勝率": (s["wins"]/s["games"]) if s["games"] else None,
            "ベイズ勝率": bayes_wr(s["wins"], s["games"], prior_games, overall_wr),
            "参考": global_sp_counts.get(n, 0) < char_min_games
        })
    return picked_sp, sp_detail

def suggest_defense_teams(attacks=None, season=None, strict_pos=False):
    FORCED_SP = SUGGEST_CONFIG["FORCED_SP"]
    CHAR_MIN_GAMES = SUGGEST_CONFIG["CHAR_MIN_GAMES"]
    PRIOR_GAMES = SUGGEST_CONFIG["PRIOR_GAMES"]

    records = load_battlelog(season)
    striker_master = load_striker_master()
    global_striker_counts = get_global_counts(records, [f"D{i+1}" for i in range(4)])
    global_sp_counts = get_global_counts(records, ["DSP1", "DSP2"])

    attacks = attacks or []
    template_table = {}
    per_attack_templates = []
    per_attack_labels = []
    ignored_attacks = []

    for idx, attack in enumerate(attacks):
        filtered = filter_records_by_attack_strikers(records, attack, strict_pos)
        label = "・".join([c for c in attack[:4] if c])
        sp_label = ""
        if len(attack) > 4 and (attack[4] or (len(attack) > 5 and attack[5])):
            sp_label = " / SP:" + "・".join([x for x in attack[4:6] if x])
        per_attack_labels.append(label + sp_label)
        if not filtered:
            ignored_attacks.append(idx)
            per_attack_templates.append({})
            continue
        templates = get_striker_templates(filtered, striker_master)
        per_attack_templates.append(templates)
        for tpl, tpl_data in templates.items():
            if tpl not in template_table:
                template_table[tpl] = {}
            template_table[tpl][idx] = tpl_data

    candidate_tpls = list(template_table.keys())
    template_scores = []
    for tpl in candidate_tpls:
        wr_list = []
        attack_stats = []
        for idx in range(len(attacks)):
            tpl_data = template_table[tpl].get(idx)
            if tpl_data:
                games = tpl_data["games"]
                wins = tpl_data["wins"]
                wr = bayes_wr(wins, games, PRIOR_GAMES, 0.5)
                wr_list.append(wr)
                attack_stats.append({
                    "games": games,
                    "wins": wins,
                    "wr": wr,
                    "winrate": (wins/games) if games else None
                })
            else:
                wr_list.append(UNKNOWN_SAMPLE_WR)
                attack_stats.append(None)
        mean_wr = sum(wr_list) / len(wr_list) if wr_list else None
        template_scores.append({
            "タグセット": tpl,
            "mean_wr": mean_wr,
            "攻めごと勝率": attack_stats
        })

    template_scores = sorted(template_scores, key=lambda x: (x["mean_wr"] if x["mean_wr"] is not None else -1), reverse=True)
    best_tpls = template_scores[:5]

    top_template_results = []
    for rank, s in enumerate(best_tpls, 1):
        tpl = s["タグセット"]
        merged_tpl_data = {"rows": [], "games": 0, "wins": 0}
        for idx in range(len(attacks)):
            tpl_data = template_table[tpl].get(idx)
            if tpl_data:
                merged_tpl_data["rows"].extend(tpl_data["rows"])
                merged_tpl_data["games"] += tpl_data["games"]
                merged_tpl_data["wins"] += tpl_data["wins"]
        picked_strikers = pick_strikers_for_template(
            tpl, merged_tpl_data, striker_master, global_striker_counts,
            CHAR_MIN_GAMES, PRIOR_GAMES, 0.5
        )
        picked_sp, sp_detail = pick_sp_for_template(
            merged_tpl_data["rows"], FORCED_SP, CHAR_MIN_GAMES, PRIOR_GAMES, 0.5, global_sp_counts
        )
        top_template_results.append({
            "順位": rank,
            "テンプレタグ": tpl,
            "平均ベイズ勝率": s["mean_wr"],
            "攻めごと勝率": s["攻めごと勝率"],
            "ピックキャラ": picked_strikers,
            "SP案": picked_sp,
            "SP詳細": sp_detail
        })

    attack_labels = [l for i, l in enumerate(per_attack_labels) if i not in ignored_attacks]
    ignored_labels = [per_attack_labels[i] for i in ignored_attacks]

    template_judgement = []
    for s in template_scores:
        tpl = s["タグセット"]
        attack_stats = []
        for stat in s["攻めごと勝率"]:
            if stat:
                attack_stats.append({
                    "games": stat["games"],
                    "wins": stat["wins"],
                    "wr": stat["wr"],
                    "winrate": stat["winrate"]
                })
            else:
                attack_stats.append(None)
        template_judgement.append({
            "タグセット": tpl,
            "攻めごと勝率": attack_stats,
            "mean_wr": s["mean_wr"],
        })
    template_judgement = sorted(template_judgement, key=lambda x: (x["mean_wr"] if x["mean_wr"] is not None else -1), reverse=True)

    return {
        "season": season,
        "上位テンプレ詳細": top_template_results,
        "攻めラベル": attack_labels,
        "無視攻め": ignored_labels,
        "テンプレ決定過程": template_judgement
    }

# ▼▼▼ 任意テンプレ（タグセット）での編成案を返す関数（API用）
def suggest_team_for_template(template_tags, attacks=None, season=None, strict_pos=False):
    """
    任意テンプレ（タグセット）に対して、攻め編成・シーズン・モードを考慮した
    キャラ・SP案（ピックキャラ/SP/詳細）を返す
    """
    # --- 修正: 受信データの型を必ず tuple(int, bool)にする ---
    def to_native_tagset(tags):
        return tuple((int(tag[0]), bool(tag[1])) for tag in tags)
    template_tags = to_native_tagset(template_tags)

    FORCED_SP = SUGGEST_CONFIG["FORCED_SP"]
    CHAR_MIN_GAMES = SUGGEST_CONFIG["CHAR_MIN_GAMES"]
    PRIOR_GAMES = SUGGEST_CONFIG["PRIOR_GAMES"]

    records = load_battlelog(season)
    striker_master = load_striker_master()
    global_striker_counts = get_global_counts(records, [f"D{i+1}" for i in range(4)])
    global_sp_counts = get_global_counts(records, ["DSP1", "DSP2"])

    # 任意テンプレの母集団をつくる
    filtered_records = []
    if attacks:
        for attack in attacks:
            filtered = filter_records_by_attack_strikers(records, attack, strict_pos)
            for r in filtered:
                # 防衛側4枠のタグがテンプレ一致
                tags = []
                for i in range(4):
                    info = striker_master.get(r.get(f"D{i+1}", ""))
                    if not info:
                        tags = []
                        break
                    tags.append((info["射程"], info["遮蔽"]))
                if tuple(tags) == template_tags:
                    filtered_records.append(r)
    else:
        # 攻め編成条件がない場合は全件
        for r in records:
            tags = []
            for i in range(4):
                info = striker_master.get(r.get(f"D{i+1}", ""))
                if not info:
                    tags = []
                    break
                tags.append((info["射程"], info["遮蔽"]))
            if tuple(tags) == template_tags:
                filtered_records.append(r)

    tpl_data = {"rows": filtered_records}
    picked_strikers = pick_strikers_for_template(
        template_tags, tpl_data, striker_master, global_striker_counts,
        CHAR_MIN_GAMES, PRIOR_GAMES, 0.5
    )
    picked_sp, sp_detail = pick_sp_for_template(
        filtered_records, FORCED_SP, CHAR_MIN_GAMES, PRIOR_GAMES, 0.5, global_sp_counts
    )
    # 順位や平均勝率などはつけず、単一テンプレの提案用
    return {
        "テンプレタグ": template_tags,
        "ピックキャラ": picked_strikers,
        "SP案": picked_sp,
        "SP詳細": sp_detail
    }

if __name__ == "__main__":
    atk_sample = [
        ["アコ", "イロハ", "ハナエ", "ハルカ", "ヒビキ", ""],
        ["コハル", "シロコ（水着）", "シュン", "ヒマリ", "", "ヒビキ"]
    ]
    from pprint import pprint
    print("通常サジェスト：")
    pprint(suggest_defense_teams(attacks=atk_sample, season=None, strict_pos=False))
    print("\n単一テンプレ案：")
    tpl_example = ((650, False), (350, False), (350, False), (750, False))
    pprint(suggest_team_for_template(tpl_example, attacks=atk_sample, season=None, strict_pos=False))
