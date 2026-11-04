import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import bridge as b
import assistant_features as f


class FeaturesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = b.ROOT
        b.ROOT = Path(self.tmp.name)
        b.initialize()
        b.store('assistant_features_client','owner',{'enabled':True},None)
        f.SAMPLES.clear()
        self.state = {'ts':123,'car':{'vEgo':10,'aEgo':-1},'drive':{'enabled':True,'active':True},
                      'bms':{'usable_rem':30,'ui_soc':50},'settings':{'follow_distance':4},
                      'freshness':{'vehicle':{'available':True},'engagement':{'available':True},
                                   'settings':{'available':True},'battery':{'ui_soc':{'available':True}}}}
        self.destination = {'label':'Public library, Springfield, Illinois','location':[-89.65,39.78]}
        self.nav = {'ok':True,'active':False,'enabled':True,'gps_ready':False,'destination':None,'navigation':{'route_state':'inactive'}}
        self.presets = []
        self.calls = []
        self.api = mock.patch.object(f,'api',side_effect=self.backend)
        self.api.start()

    def tearDown(self):
        self.api.stop()
        b.ROOT = self.old_root
        self.tmp.cleanup()

    def backend(self, bridge, path, payload=None):
        self.calls.append((path,copy.deepcopy(payload)))
        if path == '/phone-nav/api/search': return {'places':[self.destination]}
        if path == '/phone-nav/api/status': return copy.deepcopy(self.nav)
        if path == '/phone-nav/api/start':
            self.nav.update(destination=payload['destination'],active=True)
            # Waiting GPS is intentional: accepting a destination isn't active guidance.
            self.nav['navigation']={'route_state':'waiting_gps'}
            return copy.deepcopy(self.nav)
        if path == '/phone-nav/api/stop':
            self.nav.update(active=False,destination=None)
            return copy.deepcopy(self.nav)
        if path == '/phone-nav/api/presets': return {'presets':copy.deepcopy(self.presets)}
        if path == '/phone-nav/api/preset-save':
            self.presets=[dict(payload['destination'],name=payload['name'])]
            return {'presets':copy.deepcopy(self.presets)}
        if path == '/phone-nav/api/preset-remove':
            self.presets=[]
            return {'presets':[]}
        if path == '/api/assistant/telemetry': return copy.deepcopy(self.state)
        if path == '/api/assistant/state': return {'lead':{'available':False}}
        if path == '/api/lab/state': return {'recorder':{'last_sample_age_s':100},'trip':{'distance_mi':999},'trip_meters':{}}
        if path.startswith('/api/history'): return {'settings':[{'changed_at':10,'name':'follow_distance','before':3,'after':4}],'charging':[{'curve':[1]*5000,'ended_at':11}]}
        raise AssertionError(path)

    def selection(self, client='owner'):
        return f.dispatch(b,'nap_search_destination',{'query':'Public library Springfield Illinois'},client)['places'][0]['candidate_id']

    def test_navigation_permission_binding_readback_and_no_influence(self):
        ident = self.selection()
        with self.assertRaises(ValueError): f.dispatch(b,'nap_start_navigation',{'candidate_id':ident},'reader')
        b.store('assistant_features_client','other',{'enabled':True},None)
        with self.assertRaises(ValueError): f.dispatch(b,'nap_start_navigation',{'candidate_id':ident},'other')
        self.assertFalse(any(p.endswith('/start') for p,_ in self.calls))
        result=f.dispatch(b,'nap_start_navigation',{'candidate_id':ident},'owner')
        self.assertTrue(result['route_request_accepted'])
        self.assertTrue(result['destination_verified'])
        self.assertFalse(result['gps_ready'])
        self.assertEqual(result['route_state'],'waiting_gps')
        self.assertTrue(result['influence_unchanged'])
        f.dispatch(b,'nap_start_navigation',{'candidate_id':ident},'owner')
        self.assertEqual(sum(p.endswith('/start') for p,_ in self.calls),1)
        self.assertFalse(any('influence' in p or p.endswith('/set') for p,_ in self.calls))
        result=f.dispatch(b,'nap_cancel_navigation',{},'owner')
        self.assertTrue(result['cancelled'])
        with self.assertRaises(ValueError): f.dispatch(b,'nap_start_navigation',{'candidate_id':ident},'owner')

    def test_navigation_invalid_or_expired_selection_never_starts(self):
        for args in ({},{'candidate_id':'fake'},{'saved_place':'home'},{'saved_place':'home','candidate_id':'fake'},{'url':'https://example.com'}):
            with self.assertRaises(ValueError): f.dispatch(b,'nap_start_navigation',args,'owner')
        ident=self.selection()
        b.load('nav_candidate',b.digest(ident),consume=True)
        with self.assertRaises(ValueError): f.dispatch(b,'nap_start_navigation',{'candidate_id':ident},'owner')
        self.assertFalse(any(p.endswith('/start') for p,_ in self.calls))

    def test_saved_place_round_trip_uses_existing_presets(self):
        ident=self.selection()
        result=f.dispatch(b,'nap_save_place',{'name':'Home','candidate_id':ident},'owner')
        self.assertFalse(result['route_changed'])
        self.assertFalse(any(p.endswith('/start') for p,_ in self.calls))
        self.assertEqual(f.dispatch(b,'nap_saved_places',{},'reader')['places'][0]['name'],'Home')
        self.assertTrue(f.dispatch(b,'nap_start_navigation',{'saved_place':'home'},'owner')['destination_verified'])
        self.assertEqual(f.dispatch(b,'nap_remove_saved_place',{'name':'HOME'},'owner')['places'],[])
        with self.assertRaises(ValueError): f.dispatch(b,'nap_save_place',{'name':'X','candidate_id':ident},'reader')

    def test_memories_are_durable_bounded_and_do_not_apply_settings(self):
        args={'category':'preference','name':'Commute','note':'I prefer chill on the commute.'}
        with self.assertRaises(ValueError): f.dispatch(b,'nap_remember',args,'reader')
        result=f.dispatch(b,'nap_remember',args,'owner')
        self.assertFalse(result['settings_changed'])
        self.assertEqual(result['entries'][0]['note'],args['note'])
        self.assertEqual(self.calls,[])
        self.assertEqual(f.dispatch(b,'nap_owner_memory',{},'reader')['entries'][0]['name'],'commute')
        f.dispatch(b,'nap_remember',{'category':'bookmark','name':'Slowdown','note':'Investigate this moment'},'owner')
        entries=f.dispatch(b,'nap_owner_memory',{'category':'bookmark'},'reader')['entries']
        self.assertEqual(entries[0]['snapshot']['car']['aEgo'],-1)
        self.assertIsNone(entries[0]['snapshot']['battery']['usable_rem'])
        f.dispatch(b,'nap_forget_memory',{'category':'preference','name':'Commute'},'owner')
        self.assertEqual(f.dispatch(b,'nap_owner_memory',{'category':'preference'},'reader')['entries'],[])

    def test_context_discards_stale_values_and_has_no_false_prediction(self):
        self.state['freshness']['vehicle']['available']=False
        context=f.dispatch(b,'nap_trip_context',{},'reader')
        self.assertEqual(context['current']['car'],{})
        self.assertIsNone(context['trip_recorder']['data']['current_trip'])
        self.assertFalse(context['arrival_energy_estimate']['available'])
        self.assertEqual(context['current']['battery']['ui_soc'],50)
        self.assertIsNone(context['current']['battery']['usable_rem'])
        events=f.dispatch(b,'nap_recent_events',{},'reader')
        self.assertTrue(events['history_available'])
        self.assertNotIn('curve',events['recorded_events'][1]['record'])
        self.assertIn('no continuous',events['coverage'])
        self.assertEqual(len(events['assistant_observations']),2)

    def test_partial_outage_keeps_other_sources_usable(self):
        previous=self.backend
        def outage(bridge,path,payload=None):
            if path.startswith('/phone-nav/'): raise TimeoutError()
            return previous(bridge,path,payload)
        with mock.patch.object(f,'api',side_effect=outage):
            context=f.dispatch(b,'nap_trip_context',{},'reader')
        self.assertFalse(context['route']['available'])
        self.assertEqual(context['current']['car']['vEgo'],10)

    def test_arrival_energy_requires_fresh_route_energy_and_real_history(self):
        sample={'battery':{'usable_rem':30,'usable_full':60},'battery_freshness':{'usable_rem':{'available':True},'usable_full':{'available':True}}}
        route={'available':True,'data':{'route_active':True,'gps_ready':True,'route_state':'active','navigation':{'distance_remaining_m':160934.4}}}
        history={'efficiency':[{'wh_per_mi':250,'distance_mi':30,'ended_at':time.time()-60},
                               {'wh_per_mi':300,'distance_mi':40,'ended_at':time.time()-120},
                               {'wh_per_mi':1,'distance_mi':100,'ended_at':time.time()-100}]}
        with mock.patch.object(f,'api',return_value=history):
            result=f.arrival_energy(b,sample,route)
            self.assertTrue(result['available'])
            self.assertFalse(result['guaranteed'])
            self.assertEqual(result['reference_record_count'],2)
            self.assertEqual(result['estimated_energy_needed_kwh'],27.5)
            self.assertEqual(result['estimated_arrival_usable_kwh'],2.5)
            self.assertEqual(result['estimated_arrival_usable_energy_pct'],4.2)
            self.assertLess(result['sensitivity']['arrival_kwh_at_30_percent_higher_consumption'],0)
        bad=copy.deepcopy(sample); bad['battery_freshness']['usable_rem']['available']=False
        with mock.patch.object(f,'api') as api:
            self.assertFalse(f.arrival_energy(b,bad,route)['available'])
            api.assert_not_called()
        for field,value in [('gps_ready',False),('route_active',False),('route_state','waiting_gps')]:
            bad=copy.deepcopy(route); bad['data'][field]=value
            self.assertFalse(f.arrival_energy(b,sample,bad)['available'])
        with mock.patch.object(f,'api',return_value={'efficiency':[{'wh_per_mi':250,'distance_mi':30,'ended_at':time.time()+100}]}):
            self.assertFalse(f.arrival_energy(b,sample,route)['available'])

    def test_api_allowlist_and_no_credentials_in_response(self):
        self.api.stop()
        self.api=mock.patch.object(f,'api',wraps=f.api)
        self.api.start()
        with self.assertRaises(ValueError): f.api(b,'/api/set',{'name':'steering','value':1})
        with mock.patch.object(f,'PHONE_KEY',mock.Mock(read_text=mock.Mock(return_value='private-pair-key'))),mock.patch.object(b.OPENER,'open') as opened:
            opened.return_value.__enter__.return_value.read.return_value=b'{"places":[]}'
            result=f.api(b,'/phone-nav/api/search',{'query':'Library'})
            request=opened.call_args.args[0]
            self.assertEqual(request.get_header('X-nap-phone-key'),'private-pair-key')
            self.assertNotIn('private-pair-key',json.dumps(result))


if __name__=='__main__': unittest.main(verbosity=2)
