"""Chronological, offline defense-model comparison on frozen Sheets values.

Example: python -B tools/validate_defense.py --snapshot PATH --output REPORT
No application imports, network requests, or source-data writes.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from battlelog_storage import records_from_values
from battlelog_defense import OutcomeModel, prepare_dataset, _master_tags, suggest


def metrics(battles, predictions):
    y = np.asarray([b.win for b in battles])
    p = np.clip(np.asarray(predictions), 1e-8, 1-1e-8)
    return {'n': len(y), 'wins': int(y.sum()),
            'log_loss': float(-np.mean(y*np.log(p)+(1-y)*np.log(1-p))),
            'brier': float(np.mean((p-y)**2)), 'mean_prediction': float(p.mean())}


def baseline(train, test, kind):
    prior = (sum(b.win for b in train) + .5) / (len(train) + 1)
    counts = defaultdict(lambda: [0, 0])
    for b in train:
        key = b.defense if kind == 'team' else (b.attack, b.defense)
        counts[key][0] += 1
        counts[key][1] += b.win
    return np.asarray([(counts[k][1]+8*prior)/(counts[k][0]+8)
                       for b in test for k in [b.defense if kind == 'team' else (b.attack, b.defense)]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    snapshot = args.snapshot

    def load(name):
        return json.loads((snapshot/name).read_text(encoding='utf-8-sig'))['values']

    def master(name):
        table = load(name)
        return [dict(zip(table[0], row)) for row in table[1:]]

    strikers = [{'name': r['キャラ名'], '射程': r.get('射程', 0), '遮蔽': r.get('遮蔽', '')}
                for r in master('master-0.json') if r.get('キャラ名')]
    specials = [{'name': r['キャラ名']} for r in master('master-251899225.json') if r.get('キャラ名')]
    master_tags = _master_tags(strikers)
    datasets = {}
    report = {'source': '2026-11-02 general-sheet snapshot; not live',
              'split': 'posting-date boundaries near 60/80 percent; no random shuffle',
              'limitations': ['Snapshot master, not historical master versions',
                              'Observed submitted battles only; not counterfactual ranking validation'],
              'seasons': {}, 'development': {}, 'test': {}}
    inputs = []
    for season, filename in [('s10','output-1526992011.json'),('s11','output-1443432934.json')]:
        rows = records_from_values(load(filename))
        battles, excluded = prepare_dataset(rows, strikers, specials)
        dated = []
        for b in battles:
            try:
                dt = datetime.strptime(b.date, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue
            dated.append((dt, b))
        dated.sort(key=lambda entry: entry[0])
        cut1, cut2 = [dated[int(len(dated)*f)][0].date() for f in (.6, .8)]
        train = [b for dt,b in dated if dt.date()<cut1]
        dev = [b for dt,b in dated if cut1<=dt.date()<cut2]
        test = [b for dt,b in dated if dt.date()>=cut2]
        datasets[season] = (train, dev, test, battles, rows)
        inputs.append(filename)
        report['seasons'][season] = {'raw_rows':len(rows), 'valid_rows':len(battles),
            'excluded':excluded, 'train_n':len(train), 'dev_n':len(dev), 'test_n':len(test),
            'development_from':str(cut1), 'test_from':str(cut2)}

    configurations = {'main_32': (False, 32), 'interactions_32': (True, 32), 'interactions_128': (True,128)}
    # Hyperparameters are chosen on development sets, before evaluating test sets.
    for name,(interactions,reg) in configurations.items():
        report['development'][name] = {}
        for season,(train,dev,*_) in datasets.items():
            model = OutcomeModel(regularization=reg,interactions=interactions).fit(train,master_tags)
            report['development'][name][season] = metrics(dev,model.predict(dev))
    selected = min(configurations, key=lambda name: np.mean([
        report['development'][name][season]['log_loss'] for season in datasets]))
    report['selected_configuration'] = selected
    interactions,reg = configurations[selected]
    report['recommended_parameters'] = {'interactions':interactions,'regularization':reg}
    for season,(train,dev,test,battles,rows) in datasets.items():
        fitted = train+dev
        model = OutcomeModel(regularization=reg,interactions=interactions).fit(fitted,master_tags)
        prior = (sum(b.win for b in fitted)+.5)/(len(fitted)+1)
        p = model.predict(test)
        report['test'][season] = {'season_prior': metrics(test,[prior]*len(test)),
                                  'team_smoothed':metrics(test,baseline(fitted,test,'team')),
                                  'matchup_smoothed':metrics(test,baseline(fitted,test,'matchup')),
                                  'selected_model':metrics(test,p)}
        seen = {b.defense for b in fitted}
        for label, indexes in [('seen_defense',[i for i,b in enumerate(test) if b.defense in seen]),
                               ('unseen_defense',[i for i,b in enumerate(test) if b.defense not in seen])]:
            if indexes:
                subset = [test[i] for i in indexes]
                report['test'][season][label] = {'model':metrics(subset,p[indexes]),
                    'team_smoothed':metrics(subset,baseline(fitted,subset,'team'))}
        bins = []
        for low, high in [(0,.1),(.1,.2),(.2,.4),(.4,.6),(.6,1.00001)]:
            ids = [i for i,value in enumerate(p) if low<=value<high]
            if ids:
                bins.append({'range':[low,min(high,1)],**metrics([test[i] for i in ids],p[ids])})
        report['test'][season]['prediction_bins'] = bins
        # Structural and runtime checks on all valid data; no predictive score claimed here.
        start = time.perf_counter()
        result = suggest(rows,strikers,specials,season=season,scope='validation')
        candidates = result['candidates'][:5]
        observed = {b.defense for b in battles}
        report['seasons'][season]['blank_search'] = {'seconds':round(time.perf_counter()-start,3),
            'top5':len(candidates), 'all_observed':all(c['team'] in observed for c in candidates),
            'all_unique_characters':all(len(set(c['team']))==6 for c in candidates),
            'distinct_sp_pairs':len({c['team'][4:] for c in candidates}),
            'without_swimsuit_shiroko':sum('シロコ（水着）' not in c['team'] for c in candidates)}
    inputs += ['master-0.json','master-251899225.json']
    report['input_sha256'] = {f:hashlib.sha256((snapshot/f).read_bytes()).hexdigest() for f in inputs}
    report['implementation_sha256'] = hashlib.sha256((ROOT/'battlelog_defense.py').read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'selected':selected,'parameters':report['recommended_parameters'],
                      'test':report['test'],'seasons':report['seasons']},ensure_ascii=True,indent=2))


if __name__ == '__main__':
    main()
