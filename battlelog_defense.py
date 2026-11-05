"""Defense-team evaluation shared by both data scopes. No external I/O.

Models predict submitted battle outcomes, not population win rates. Candidates
are complete observed teams; SP order never changes identity or matching.
"""
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import threading
import unicodedata

import numpy as np

from battlelog_safety import validate_attacks
from battlelog_storage import canonical_rows

VERSION = 'six-person-v1'
_cache = OrderedDict()
_cache_lock = threading.Lock()


def normalize(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def team_key(row, side, names=None):
    raw = [row.get(f'{side}{i}', '') for i in range(1, 5)] + [row.get(f'{side}SP{i}', '') for i in (1, 2)]
    values = [names.get(normalize(v), '') if names is not None else normalize(v) for v in raw]
    return tuple(values[:4]) + tuple(sorted(values[4:]))


@dataclass(frozen=True)
class Battle:
    attack: tuple
    defense: tuple
    win: int
    date: str = ''


def prepare_dataset(records, strikers, specials):
    st = {normalize(c['name']): c['name'] for c in strikers}
    sp = {normalize(c['name']): c['name'] for c in specials}
    names = dict(st, **sp)
    excluded = Counter()
    battles = []
    unique = canonical_rows(records)
    excluded['duplicate'] = len(records) - len(unique)
    for row in unique:
        result = row.get('勝敗_2')
        if result not in ('Win', 'Lose'):
            excluded['outcome'] += 1
            continue
        if row.get('勝敗') in ('Win', 'Lose') and row['勝敗'] == result:
            excluded['conflicting_outcome'] += 1
            continue
        a, d = team_key(row, 'A', names), team_key(row, 'D', names)
        if not all(a + d):
            excluded['missing_or_unknown_character'] += 1
            continue
        if len(set(a)) != 6 or len(set(d)) != 6:
            excluded['repeated_character'] += 1
            continue
        if any(normalize(c) not in st for c in a[:4] + d[:4]) or any(normalize(c) not in sp for c in a[4:] + d[4:]):
            excluded['character_role'] += 1
            continue
        battles.append(Battle(a, d, int(result == 'Win'), str(row.get('日付', ''))))
    return battles, dict(excluded)


def query_key(query, strict_pos):
    return (tuple(query[:4]) if strict_pos else tuple(sorted(query[:4])), tuple(sorted(query[4:])))


def build_targets(battles, attacks=None, strict_pos=False):
    attacks = attacks or [[''] * 6]
    validate_attacks(attacks)
    normalized = []
    seen = set()
    for query in attacks:
        query = [normalize(c) for c in query]
        if not any(query):
            continue
        if len([c for c in query if c]) != len({c for c in query if c}):
            raise ValueError('同じ攻撃編成に同じ生徒を複数回指定できません。')
        key = query_key(query, strict_pos)
        if key not in seen:
            normalized.append(query)
            seen.add(key)
    normalized = normalized or [[''] * 6]
    all_attacks = sorted({b.attack for b in battles})
    occurrences = Counter(b.attack for b in battles)
    groups = []
    weights = defaultdict(float)
    for query in normalized:
        targets = []
        for attack in all_attacks:
            a = [normalize(c) for c in attack]
            st_match = (all(not c or a[i] == c for i, c in enumerate(query[:4])) if strict_pos
                        else all(not c or c in a[:4] for c in query[:4]))
            if st_match and all(not c or c in a[4:] for c in query[4:]):
                targets.append(attack)
        label = '・'.join(c for c in query[:4] + sorted(query[4:]) if c) or 'シーズン全体'
        groups.append({'label': label, 'targets': targets,
                       'games': sum(occurrences[a] for a in targets), 'attack_count': len(targets)})
        if targets:
            for attack in targets:
                weights[attack] += 1 / len(normalized) / len(targets)
    return groups, dict(weights)


def side_features(team, side, master):
    result = [(side, 'slot', i, name) for i, name in enumerate(team[:4])]
    result += [(side, 'sp', name) for name in team[4:]]
    result += [(side, 'sp_pair', team[4:]), (side, 'core', team[:4]), (side, 'team', team)]
    tags = tuple(master.get(name, (0, False)) for name in team[:4])
    result.append((side, 'layout', tags))
    return result


def members(team):
    return tuple(('st', c) for c in team[:4]) + tuple(('sp', c) for c in team[4:])


def cross_features(a, d):
    return ([('versus', x, y) for x in members(a) for y in members(d)]
            + [('sp_vs_sp', a[4:], d[4:]), ('core_vs_sp', a[:4], d[4:]),
               ('sp_vs_core', a[4:], d[:4])])


class OutcomeModel:
    """L2 logistic regression with sparse categorical features and line search.

    Uses existing NumPy only. Loss is summed over battles; intercept is free.
    Armijo search guarantees accepted steps decrease the penalized objective.
    """
    def __init__(self, regularization=128.0, interactions=True, max_iter=180):
        self.regularization = regularization
        self.interactions = interactions
        self.max_iter = max_iter

    def fit(self, battles, master=None):
        self.master = master or {}
        self.base_rate = (sum(b.win for b in battles) + .5) / (len(battles) + 1)
        self.intercept = math.log(self.base_rate / (1 - self.base_rate))
        vocabulary = {}
        row_ids, columns = [], []
        for i, b in enumerate(battles):
            features = side_features(b.attack, 'a', self.master) + side_features(b.defense, 'd', self.master)
            if self.interactions:
                features += cross_features(b.attack, b.defense)
            for feature in features:
                if feature not in vocabulary:
                    vocabulary[feature] = len(vocabulary)
                row_ids.append(i)
                columns.append(vocabulary[feature])
        self.coefficients = {}
        if len(battles) < 20 or len({b.win for b in battles}) < 2:
            return self
        rows, cols = np.asarray(row_ids), np.asarray(columns)
        y = np.asarray([b.win for b in battles], dtype=float)
        n, k = len(battles), len(vocabulary)
        w = np.zeros(k)
        penalty = float(self.regularization)
        diagonal = .25 * np.bincount(cols, minlength=k) + penalty
        bias_diagonal = max(.25 * n, 1)

        def objective(coef, bias):
            z = bias + np.bincount(rows, weights=coef[cols], minlength=n)
            loss = np.sum(np.logaddexp(0, z) - y * z) + .5 * penalty * np.dot(coef, coef)
            return float(loss), z

        loss, z = objective(w, self.intercept)
        for _ in range(self.max_iter):
            error = 1 / (1 + np.exp(-np.clip(z, -40, 40))) - y
            gradient = np.bincount(cols, weights=error[rows], minlength=k) + penalty * w
            bias_gradient = float(error.sum())
            direction = gradient / diagonal
            bias_direction = bias_gradient / bias_diagonal
            decrement = float(np.dot(gradient, direction) + bias_gradient * bias_direction)
            if decrement < 1e-8:
                break
            step = 1.0
            accepted = False
            for _ in range(22):
                next_w = w - step * direction
                next_bias = self.intercept - step * bias_direction
                next_loss, next_z = objective(next_w, next_bias)
                if next_loss <= loss - 1e-4 * step * decrement:
                    accepted = True
                    break
                step *= .5
            if not accepted:
                break
            improvement = loss - next_loss
            w, self.intercept, loss, z = next_w, next_bias, next_loss, next_z
            if improvement < 1e-7:
                break
        self.coefficients = {f: float(w[i]) for f, i in vocabulary.items()}
        return self

    def predict_matrix(self, attacks, defenses):
        coef = self.coefficients
        a_bias = [sum(coef.get(f, 0) for f in side_features(a, 'a', self.master)) for a in attacks]
        d_bias = [sum(coef.get(f, 0) for f in side_features(d, 'd', self.master)) for d in defenses]
        z = self.intercept + np.asarray(a_bias)[:, None] + np.asarray(d_bias)[None, :]
        if self.interactions and coef:
            av = {member: i for i, member in enumerate(dict.fromkeys(m for a in attacks for m in members(a)))}
            dv = {member: i for i, member in enumerate(dict.fromkeys(m for d in defenses for m in members(d)))}
            ax, dx = np.zeros((len(attacks), len(av))), np.zeros((len(defenses), len(dv)))
            for i, a in enumerate(attacks):
                for m in members(a):
                    ax[i, av[m]] = 1
            for i, d in enumerate(defenses):
                for m in members(d):
                    dx[i, dv[m]] = 1
            cross = np.zeros((len(av), len(dv)))
            for f, value in coef.items():
                if f[0] == 'versus' and f[1] in av and f[2] in dv:
                    cross[av[f[1]], dv[f[2]]] = value
            z += ax @ cross @ dx.T
            for family, a_slice, d_slice in [('sp_vs_sp', slice(4, 6), slice(4, 6)),
                                             ('core_vs_sp', slice(0, 4), slice(4, 6)),
                                             ('sp_vs_core', slice(4, 6), slice(0, 4))]:
                ac = list(dict.fromkeys(a[a_slice] for a in attacks))
                dc = list(dict.fromkeys(d[d_slice] for d in defenses))
                ai, di = {c: i for i, c in enumerate(ac)}, {c: i for i, c in enumerate(dc)}
                lookup = np.zeros((len(ac), len(dc)))
                for f, value in coef.items():
                    if f[0] == family and f[1] in ai and f[2] in di:
                        lookup[ai[f[1]], di[f[2]]] = value
                z += lookup[np.asarray([ai[a[a_slice]] for a in attacks])[:, None],
                            np.asarray([di[d[d_slice]] for d in defenses])[None, :]]
        return 1 / (1 + np.exp(-np.clip(z, -40, 40)))

    def predict(self, battles):
        # Pairwise validation avoids evaluating the entire Cartesian product.
        result = []
        for b in battles:
            fs = side_features(b.attack, 'a', self.master) + side_features(b.defense, 'd', self.master)
            if self.interactions:
                fs += cross_features(b.attack, b.defense)
            z = self.intercept + sum(self.coefficients.get(f, 0) for f in fs)
            result.append(1 / (1 + math.exp(-max(-40, min(40, z)))))
        return np.asarray(result)


def score_distribution(probabilities, weights, alpha=.2):
    p, q = np.asarray(probabilities), np.asarray(weights)
    total = q.sum()
    if total <= 0:
        return None
    q = q / total
    mean = float(np.dot(p, q))
    tail = 0.0
    remaining = alpha
    for i in np.argsort(p, kind='stable'):
        take = min(remaining, q[i])
        tail += take * p[i]
        remaining -= take
        if remaining <= 1e-12:
            break
    tail /= alpha
    return {'mean': mean, 'tail': float(tail), 'score': .75 * mean + .25 * tail}


def _master_tags(strikers):
    result = {}
    for c in strikers:
        try:
            distance = int(c.get('射程', 0))
        except (TypeError, ValueError):
            distance = 0
        cover = c.get('遮蔽', False)
        result[c['name']] = (distance, cover is True or str(cover).upper() == 'TRUE')
    return result


def _model_for(battles, master, scope, season):
    payload = [VERSION, scope, season, sorted(master.items()),
               [(b.attack, b.defense, b.win) for b in battles]]
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with _cache_lock:
        if digest in _cache:
            _cache.move_to_end(digest)
            return digest, _cache[digest]
        model = OutcomeModel().fit(battles, master)
        _cache[digest] = model
        while len(_cache) > 4:
            _cache.popitem(last=False)
    return digest, model


def suggest(records, strikers, specials, attacks=None, season=None, strict_pos=False, scope='general', template_tags=None):
    battles, excluded = prepare_dataset(records, strikers, specials)
    groups, weights = build_targets(battles, attacks, strict_pos)
    master = _master_tags(strikers)
    result = {'version': VERSION, 'season': season, 'groups': groups, 'candidates': [],
              'valid_games': len(battles), 'excluded': excluded,
              'unresolved_conditions': [g['label'] for g in groups if not g['targets']],
              'attack_count': len(weights), 'model_method': 'regularized_logistic',
              'limited_data': len(battles) < 20 or len({b.win for b in battles}) < 2}
    if result['limited_data']:
        result['model_method'] = 'smoothed_season_prior'
    # Do not let an unresolved query silently become an easier query.
    if not weights or result['unresolved_conditions']:
        result['groups'] = [{k: v for k, v in g.items() if k != 'targets'} for g in groups]
        return result
    defenses = sorted({b.defense for b in battles})
    if template_tags is not None:
        tags = tuple((int(t[0]), bool(t[1])) for t in template_tags)
        defenses = [d for d in defenses if tuple(master[c] for c in d[:4]) == tags]
    if not defenses:
        return result
    digest, model = _model_for(battles, master, scope, season)
    targets = sorted(weights)
    q = np.asarray([weights[a] for a in targets])
    probabilities = model.predict_matrix(targets, defenses)
    exact = defaultdict(lambda: [0, 0])
    defense_counts = Counter()
    for b in battles:
        exact[b.attack, b.defense][0] += 1
        exact[b.attack, b.defense][1] += b.win
        defense_counts[b.defense] += 1
    target_index = {a: i for i, a in enumerate(targets)}
    for j, d in enumerate(defenses):
        p = probabilities[:, j]
        metrics = score_distribution(p, q)
        direct = sum(weights[a] for a in targets if (a, d) in exact)
        per_group = []
        for g in groups:
            indexes = [target_index[a] for a in g['targets']]
            gs = score_distribution(p[indexes], np.ones(len(indexes)))
            games = sum(exact.get((a, d), [0, 0])[0] for a in g['targets'])
            wins = sum(exact.get((a, d), [0, 0])[1] for a in g['targets'])
            per_group.append(dict(gs, games=games, wins=wins, observed_rate=wins/games if games else None))
        weakest = []
        for i in np.argsort(p)[:3]:
            a = targets[i]
            games, wins = exact.get((a, d), [0, 0])
            weakest.append({'attack': a, 'estimate': float(p[i]), 'games': games, 'wins': wins})
        result['candidates'].append(dict(metrics, team=d, games=defense_counts[d],
            direct_coverage=direct, inferred_coverage=max(0., 1-direct), per_group=per_group,
            weak_matchups=weakest, reference=defense_counts[d] < 5 or direct < .5))
    result['candidates'].sort(key=lambda c: (-c['score'], -c['games'], c['team']))
    for i, candidate in enumerate(result['candidates'], 1):
        candidate['rank'] = i
    query_fingerprint = [digest, attacks, strict_pos, template_tags]
    result['analysis_id'] = hashlib.sha256(json.dumps(query_fingerprint, ensure_ascii=False).encode()).hexdigest()[:16]
    result['dataset_version'] = digest[:16]
    # Internal targets are unnecessary in rendered HTML / public responses.
    result['groups'] = [{k: v for k, v in g.items() if k != 'targets'} for g in groups]
    return result
