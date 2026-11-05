"""Offline six-person defense tests: model, matching, scopes, and both pages."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'Blue_Archive_Battlelog_collect_public'),
                str(ROOT/'Blue_Archive_Battlelog_collect')]
os.environ['BATTLELOG_SKIP_DOTENV'] = '1'

from battlelog_defense import (Battle, OutcomeModel, build_targets, prepare_dataset,
                               score_distribution, suggest, _master_tags)


def fixture():
    strikers = [{'name': n, '射程': 650, '遮蔽': False, 'image': '/local-icon.png'}
                for n in ['A1','A2','A3','A4','B1','B2','B3','B4','D1','D2','D3','D4']]
    specials = [{'name': n, 'image': '/local-icon.png'} for n in ['AS1','AS2','BS1','BS2','DS1','DS2','ES1','ES2']]
    a = [('A1','A2','A3','A4','AS1','AS2'), ('B1','B2','B3','B4','BS1','BS2')]
    d = [('D1','D2','D3','D4','DS1','DS2'), ('D1','D2','D3','D4','ES1','ES2')]
    rows = []
    for i in range(160):
        ai, di = (i//2)%2, i%2
        row = {f'A{j+1}': n for j,n in enumerate(a[ai][:4])}
        row.update({f'D{j+1}': n for j,n in enumerate(d[di][:4])})
        row.update(dict(zip(('ASP1','ASP2','DSP1','DSP2'), a[ai][4:]+d[di][4:])))
        row.update({'ID':str(i), '勝敗_2':'Win' if ai==di else 'Lose',
                    '勝敗':'Lose' if ai==di else 'Win', 'プレイヤー名':'PRIVATE-NAME',
                    '日付':f'2026-10-{i%28+1:02d} 12:00:00'})
        rows.append(row)
    return strikers,specials,rows,a,d


class DefenseTests(unittest.TestCase):
    def setUp(self):
        self.st,self.sp,self.rows,self.a,self.d = fixture()

    def test_only_complete_valid_unique_teams_are_candidates(self):
        invalid = dict(self.rows[0], ID='bad', D2='D1')
        result = suggest(self.rows+[invalid],self.st,self.sp)
        self.assertEqual({c['team'] for c in result['candidates']},set(self.d))
        self.assertEqual(result['excluded']['repeated_character'],1)
        self.assertTrue(all(len(set(c['team']))==6 for c in result['candidates']))

    def test_sp_pair_and_opponent_change_the_recommendation(self):
        for ai in (0,1):
            result = suggest(self.rows,self.st,self.sp,attacks=[[self.a[ai][0]]+['']*5])
            self.assertEqual(result['candidates'][0]['team'],self.d[ai])
            self.assertNotIn('シロコ（水着）',result['candidates'][0]['team'])

    def test_sp_order_is_ignored_even_in_strict_mode(self):
        battles,_ = prepare_dataset(self.rows,self.st,self.sp)
        one = build_targets(battles,[list(self.a[0])],True)
        reversed_sp = list(self.a[0][:4])+list(reversed(self.a[0][4:]))
        self.assertEqual(one,build_targets(battles,[reversed_sp],True))
        reversed_rows = [dict(r,ASP1=r['ASP2'],ASP2=r['ASP1'],DSP1=r['DSP2'],DSP2=r['DSP1']) for r in self.rows]
        x = suggest(self.rows,self.st,self.sp)
        y = suggest(reversed_rows,self.st,self.sp)
        self.assertEqual(x['candidates'],y['candidates'])

    def test_striker_position_is_preserved(self):
        battles,_ = prepare_dataset(self.rows,self.st,self.sp)
        query = ['',self.a[0][0],'','','','']
        self.assertTrue(build_targets(battles,[query],False)[1])
        self.assertFalse(build_targets(battles,[query],True)[1])

    def test_duplicate_conditions_do_not_change_scores_or_sample_counts(self):
        q = [list(self.a[0])]
        x = suggest(self.rows,self.st,self.sp,attacks=q)
        y = suggest(self.rows,self.st,self.sp,attacks=q+q)
        self.assertEqual(x['candidates'],y['candidates'])
        self.assertEqual(len(y['groups']),1)
        self.assertEqual(x['valid_games'],y['valid_games'])

    def test_unmatched_condition_is_retained_and_blocks_combined_ranking(self):
        x = suggest(self.rows,self.st,self.sp,attacks=[list(self.a[0]),['unknown']+['']*5])
        self.assertEqual(x['unresolved_conditions'],['unknown'])
        self.assertFalse(x['candidates'])
        self.assertEqual(len(x['groups']),2)

    def test_invalid_outcomes_roles_and_duplicates_are_excluded(self):
        bad = [dict(self.rows[0],ID='empty',勝敗_2=''),
               dict(self.rows[0],ID='conflict',勝敗='Win'),
               dict(self.rows[0],ID='role',A1='AS1',ASP1='A1')]
        battles,excluded = prepare_dataset(self.rows+bad+[self.rows[0]],self.st,self.sp)
        self.assertEqual(len(battles),len(self.rows))
        self.assertEqual(excluded['duplicate'],1)
        self.assertEqual(excluded['outcome'],1)
        self.assertEqual(excluded['conflicting_outcome'],1)
        self.assertEqual(excluded['character_role'],1)

    def test_matrix_predictions_match_pairwise_predictions(self):
        battles,_ = prepare_dataset(self.rows,self.st,self.sp)
        model = OutcomeModel().fit(battles,_master_tags(self.st))
        matrix = model.predict_matrix(self.a,self.d)
        pairs = [Battle(a,d,0) for a in self.a for d in self.d]
        np.testing.assert_allclose(matrix.ravel(),model.predict(pairs),rtol=1e-12,atol=1e-12)
        self.assertGreater(matrix[0,0],matrix[0,1])
        self.assertGreater(matrix[1,1],matrix[1,0])

    def test_single_class_is_smoothed_instead_of_claiming_certainty(self):
        only_wins = [r for r in self.rows if r['勝敗_2']=='Win']
        result = suggest(only_wins,self.st,self.sp)
        self.assertTrue(result['limited_data'])
        self.assertEqual(result['model_method'],'smoothed_season_prior')
        self.assertTrue(all(0<c['mean']<1 for c in result['candidates']))

    def test_rounded_score_prefers_fewer_deep_weaknesses(self):
        a = score_distribution([.9,.9,.9,.1,.1],[1]*5)
        b = score_distribution([.6,.6,.6,.5,.5],[1]*5)
        self.assertGreater(a['mean'],b['mean'])
        self.assertLess(a['score'],b['score'])
        self.assertAlmostEqual(a['score'],.46)
        self.assertAlmostEqual(b['score'],.545)

    def test_scope_changes_cache_identity_and_no_private_fields_escape(self):
        one = suggest(self.rows,self.st,self.sp,scope='general')
        two = suggest(self.rows,self.st,self.sp,scope='limited')
        self.assertNotEqual(one['dataset_version'],two['dataset_version'])
        self.assertNotIn('PRIVATE-NAME',json.dumps(one))
        self.assertNotIn('プレイヤー名',json.dumps(two,ensure_ascii=False))

    def test_both_loaders_use_their_own_source(self):
        for directory,filename,manager in [
            ('Blue_Archive_Battlelog_collect_public','defense_suggester.py','spreadsheet_manager'),
            ('Blue_Archive_Battlelog_collect','defense_suggester_limited.py','spreadsheet_manager_limited')]:
            fake = types.ModuleType(manager)
            fake.get_output_sheet_cache = lambda season: self.rows
            fake.get_striker_list_from_sheet = lambda: self.st
            fake.get_special_list_from_sheet = lambda: self.sp
            spec = importlib.util.spec_from_file_location('loader_under_test',ROOT/directory/filename)
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules,{manager:fake}):
                spec.loader.exec_module(module)
            result = module.suggest_defense_teams(season='s11')
            self.assertEqual(len(result['candidates']),2)
            detail = module.suggest_team_for_template([[650,False]]*4,season='s11')
            self.assertEqual(len(detail['ピックキャラ']),4)
            self.assertEqual(len(detail['SP案']),2)


class DefensePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.public = importlib.import_module('Blue_Archive_Battlelog_collect_public.app')
        cls.limited = importlib.import_module('Blue_Archive_Battlelog_collect.app_limited')
        from flask import Flask
        cls.limited_app = Flask('defense-limited-test')
        cls.limited_app.register_blueprint(cls.limited.limited, url_prefix='/')
        cls.limited_app.config['TESTING'] = True

    def setUp(self):
        self.st,self.sp,self.rows,_,_ = fixture()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ,{'BATTLELOG_DATA_DIR':self.temp.name,'BATTLELOG_SKIP_DOTENV':'1'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = patch('requests.sessions.Session.request',side_effect=AssertionError('No network in tests'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def render_page(self,module,app,data):
        result = suggest(self.rows,self.st,self.sp,attacks=[['']*6],season='s11')
        with patch.object(module,'get_striker_list_from_sheet',return_value=self.st), \
             patch.object(module,'get_special_list_from_sheet',return_value=self.sp), \
             patch.object(module,'suggest_defense_teams',return_value=result):
            return app.test_client().post('/defense_suggest',data=data)

    def test_public_and_limited_pages_render_new_results_and_scope_details(self):
        public = self.render_page(self.public,self.public.app,{'season':'s11'})
        limited = self.render_page(self.limited,self.limited_app,{'season':'s11'})
        for response in (public,limited):
            self.assertEqual(response.status_code,200)
            self.assertIn(b'class="defense-best5-list"',response.data)
            self.assertIn(b'class="rank-badge"',response.data)
            self.assertIn(b'data-defense-rank="1"',response.data)
            self.assertNotIn('PRIVATE-NAME'.encode(),response.data)
            self.assertNotIn(b'class="defense-stats"',response.data)
            self.assertNotIn(b'class="defense-result"',response.data)
        self.assertNotIn(b'id="collapseContent"',public.data)
        self.assertIn(b'id="collapseContent"',limited.data)
        self.assertIn(b'data-preview-id="defense-preview-1"',limited.data)
        self.assertIn(b'<template id="defense-preview-1"',limited.data)
        self.assertNotIn(b'/api/template_detail',limited.data)

    def test_unmatched_query_is_escaped_on_both_pages(self):
        malicious = '<img src=x onerror=alert(1)>'
        result = suggest(self.rows,self.st,self.sp,attacks=[[malicious]+['']*5])
        for module,app in [(self.public,self.public.app),(self.limited,self.limited_app)]:
            with patch.object(module,'get_striker_list_from_sheet',return_value=self.st), \
                 patch.object(module,'get_special_list_from_sheet',return_value=self.sp), \
                 patch.object(module,'suggest_defense_teams',return_value=result):
                response = app.test_client().post('/defense_suggest',data={'season':'s11'})
            self.assertEqual(response.status_code,200)
            self.assertNotIn(malicious.encode(),response.data)
            self.assertIn(b'&lt;img',response.data)


if __name__ == '__main__':
    unittest.main()
