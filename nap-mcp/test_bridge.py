import base64
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlencode, urlsplit, parse_qs

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).with_name('bridge.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        b.ROOT = Path(cls.temp.name)
        b.initialize()
        cls.server = b.Server(('127.0.0.1', 0), b.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()
    def req(self, path, payload=None, token=None, method=None, origin=None, basic=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        headers = {'Content-Type': 'application/json'}
        if token: headers['Authorization'] = 'Bearer ' + token
        if origin: headers['Origin'] = origin
        if basic: headers['Authorization'] = 'Basic ' + base64.b64encode(basic.encode()).decode()
        conn.request(method or ('POST' if payload is not None else 'GET'), path,
                     json.dumps(payload) if payload is not None else None, headers)
        r = conn.getresponse()
        raw = (r.readline() if r.getheader('Content-Type') == 'text/event-stream' else r.read()).decode()
        result = (r.status, dict(r.getheaders()), raw)
        conn.close()
        return result
    def test_security_and_full_flow(self):
        self.assertEqual(self.req('/mcp')[0], 401)
        for method in ('initialize', 'tools/list'):
            self.assertEqual(self.req('/mcp', {'jsonrpc':'2.0','id':1,'method':method})[0], 200)
        self.assertEqual(self.req('/mcp', {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'nap_live_state'}})[0], 401)
        self.assertEqual(self.req('/api/state')[0], 404)
        self.assertEqual(self.req('/healthz', origin='https://evil.example')[0], 403)
        self.assertEqual(self.req('/healthz', origin='null')[0], 403)
        self.assertEqual(self.req('/mcp', {'jsonrpc':'2.0','id':1,'method':'tools/list'}, origin='null')[0], 403)
        self.assertEqual(self.req('/healthz', origin='https://nap-comma.tail86fdf3.ts.net:8443')[0], 200)
        self.assertEqual(self.req('/.well-known/oauth-protected-resource')[0], 200)
        self.assertEqual(self.req('/register', {'redirect_uris':['http://evil.example/cb']})[0], 400)
        client = json.loads(self.req('/register', {'redirect_uris':['https://example.com/callback'], 'client_name':'Test', 'token_endpoint_auth_method':'none'})[2])
        verifier = 'v' * 64
        args = dict(client_id=client['client_id'], redirect_uri='https://example.com/callback', response_type='code',
                    code_challenge_method='S256', code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),
                    state='test-state', scope='nap:read', resource=b.RESOURCE)
        self.assertEqual(self.req('/authorize?' + urlencode({**args,'redirect_uri':'https://evil.example'}))[0], 400)
        form = self.req('/authorize?' + urlencode(args))[2]
        ticket = re.search('name="ticket" value="([^"]+)"', form)[1]
        self.assertEqual(self.req('/authorize', {'ticket':ticket, 'password':'000000'})[0], 403)
        status, headers, _ = self.req('/authorize', {'ticket':ticket, 'password':(b.ROOT/'owner-password').read_text().strip()}, origin='null')
        self.assertEqual(status, 302)
        code = parse_qs(urlsplit(headers['Location']).query)['code'][0]
        exchange = dict(grant_type='authorization_code', client_id=client['client_id'], redirect_uri=args['redirect_uri'], code=code, code_verifier=verifier, resource=b.RESOURCE)
        self.assertEqual(self.req('/token', {**exchange,'code_verifier':'x'*64})[0], 400)
        tokens = json.loads(self.req('/token', exchange)[2])
        self.assertEqual(self.req('/token', exchange)[0], 400)
        access = tokens['access_token']
        def rpc(method, params=None):
            return json.loads(self.req('/mcp', {'jsonrpc':'2.0','id':1,'method':method,'params':params or {}}, access)[2])
        self.assertEqual(rpc('initialize', {'protocolVersion':'2025-03-26'})['result']['protocolVersion'], '2025-03-26')
        blocked=rpc('tools/call',{'name':'nap_set_driving_preference','arguments':{'setting':'follow_distance','value':6}})['result']
        self.assertFalse(blocked['isError'])
        self.assertEqual(json.loads(blocked['content'][0]['text'])['status'],'blocked')
        tools = rpc('tools/list')['result']['tools']
        self.assertEqual(len(tools), len(b.TOOLS) + len(b.features.NAMES) + len(b.reads.NAMES))
        self.assertTrue(all(t['annotations']['readOnlyHint'] for t in tools if t['name'] in b.TOOLS and t['name'] != 'nap_set_driving_preference'))
        self.assertFalse(next(t for t in tools if t['name']=='nap_set_driving_preference')['annotations']['readOnlyHint'])
        legacy=rpc('tools/call',{'name':'nap_propose_setting_change','arguments':{'setting':'follow_distance','value':4}})['result']
        self.assertTrue(legacy['isError'])
        self.assertIn('nap_set_driving_preference',legacy['content'][0]['text'])
        self.assertNotIn('approval_url',legacy['content'][0]['text'])
        self.assertEqual(rpc('tools/call', {'name':'nap_live_state','arguments':{'url':'http://evil'}})['error']['code'], -32602)
        self.assertEqual(rpc('tools/call', {'name':'reboot'})['error']['code'], -32602)
        original = b.telemetry
        b.telemetry = lambda name: {'data': {'ts':123}}
        self.assertFalse(rpc('tools/call', {'name':'nap_live_state'})['result']['isError'])
        b.telemetry = original
        self.assertEqual(self.req('/mcp', token=access)[0], 200)
        refresh = dict(grant_type='refresh_token', client_id=client['client_id'], refresh_token=tokens['refresh_token'], resource=b.RESOURCE)
        self.assertEqual(self.req('/token', {**refresh,'client_id':'wrong'})[0], 401)
        self.assertEqual(self.req('/token', refresh)[0], 200)
        self.assertEqual(self.req('/token', refresh)[0], 400)
        self.assertEqual(self.req('/revoke', {'token':access})[0], 200)
        self.assertEqual(self.req('/mcp', token=access)[0], 401)

    def test_persistent_permission_and_token_expiry(self):
        b.store('direct_config_client','permanent-test',{'settings':['follow_distance','personality']},None)
        self.assertIsNotNone(b.load('direct_config_client','permanent-test'))
        with b.sqlite3.connect(b.ROOT/'oauth.sqlite') as db:
            self.assertIsNone(db.execute('SELECT expires FROM records WHERE kind=? AND key=?',('direct_config_client','permanent-test')).fetchone()[0])
        b.store('access','expired-test',{'scope':b.SCOPE},-1)
        self.assertIsNone(b.load('access','expired-test'))
        self.assertIsNotNone(b.load('direct_config_client','permanent-test'))
        b.load('direct_config_client','permanent-test',consume=True)
        self.assertIsNone(b.load('direct_config_client','permanent-test'))

    def test_direct_driving_preferences(self):
        with self.assertRaises(ValueError): b.set_driving_preference({'setting':'follow_distance','value':4},'unauthorized')
        b.store('direct_config_client','enabled',{'settings':['follow_distance','personality','speed_trim']},3600)
        state={'settings':{'follow_distance':3,'speed_trim':0.0},'car':{'vEgo':20.0},'drive':{'enabled':True},'freshness':{k:{'available':True} for k in ('vehicle','engagement','settings')}}
        with mock.patch.object(b,'fresh_config_state',return_value=state),mock.patch.object(b.OPENER,'open') as upstream:
            upstream.return_value.__enter__.return_value.read.return_value=b'{"settings":{"follow_distance":4}}'
            result=b.set_driving_preference({'setting':'follow_distance','value':4},'enabled')
            self.assertTrue(result['applied'])
            self.assertEqual(json.loads(upstream.call_args.args[0].data),{'name':'follow_distance','value':4})
        with mock.patch.object(b,'fresh_config_state',return_value=dict(state,freshness={})),mock.patch.object(b.OPENER,'open') as upstream:
            with self.assertRaises(ValueError): b.set_driving_preference({'setting':'follow_distance','value':4},'enabled')
            upstream.assert_not_called()
        for args in ({'setting':'experimental','value':True},{'setting':'follow_distance','value':8},{'setting':'follow_distance','value':True}):
            with self.assertRaises(ValueError): b.set_driving_preference(args,'enabled')

    def test_feature_dispatch_and_client_permissions(self):
        b.store('access',b.digest('feature-test'),{'client_id':'features-owner','resource':b.RESOURCE,'scope':b.SCOPE},60)
        def call(name,args):
            return json.loads(self.req('/mcp',{'jsonrpc':'2.0','id':9,'method':'tools/call','params':{'name':name,'arguments':args}},token='feature-test')[2])['result']
        with mock.patch.object(b.features,'api',return_value={'places':[]}):
            self.assertFalse(call('nap_search_destination',{'query':'Chicago library'})['isError'])
        with mock.patch.object(b.features,'api') as upstream:
            self.assertTrue(call('nap_start_navigation',{'saved_place':'home'})['isError'])
            upstream.assert_not_called()
        b.store('assistant_features_client','features-owner',{'enabled':True},None)
        self.assertFalse(call('nap_remember',{'category':'preference','name':'test','note':'prefer quiet replies'})['isError'])
        self.assertTrue(call('nap_trip_context',{'unexpected':1})['isError'])

    def test_extended_reads_dispatch(self):
        b.store('access',b.digest('read-test'),{'client_id':'read-owner','resource':b.RESOURCE,'scope':b.SCOPE},60)
        payload={'jsonrpc':'2.0','id':19,'method':'tools/call','params':{'name':'nap_database_query','arguments':{'sql':'SELECT 1'}}}
        with mock.patch.object(b.reads,'dispatch',return_value={'read_only':True}) as upstream:
            result=json.loads(self.req('/mcp',payload,token='read-test')[2])['result']
            self.assertFalse(result['isError']);upstream.assert_called_once()
        self.assertEqual(self.req('/mcp',payload)[0],401)
        self.assertTrue(all(t['annotations']['readOnlyHint'] for t in b.reads.SCHEMAS))

    def test_live_connection_summary(self):
        state={'ts':123,'freshness':{'vehicle':{'status':'fresh','age_seconds':.05},'engagement':{'status':'fresh','age_seconds':.07},'settings':{'status':'fresh','age_seconds':.3},'navigation':{'status':'unavailable','age_seconds':None}}}
        b.CACHE.clear()
        with mock.patch.object(b.OPENER,'open') as upstream:
            upstream.return_value.__enter__.return_value.read.return_value=json.dumps(state).encode()
            result=b.telemetry('nap_live_state')
            self.assertTrue(result['connection']['nap_api_reachable'])
            self.assertEqual(result['connection']['feeds']['vehicle']['status'],'fresh')
            self.assertEqual(result['connection']['feeds']['navigation']['status'],'unavailable')
        b.CACHE.clear()

    def test_direct_speed_trim_bounds(self):
        b.store('direct_config_client','trim-enabled',{'settings':['speed_trim']},3600)
        b.store('direct_config_client','trim-disabled',{'settings':['follow_distance']},3600)
        state={'settings':{'speed_trim':0.0},'car':{'vEgo':20.0},'freshness':{k:{'available':True} for k in ('vehicle','engagement','settings')}}
        for value in (-6,6,1.5,True,'3',float('nan')):
            with self.assertRaises(ValueError): b.set_driving_preference({'setting':'speed_trim','value':value},'trim-enabled')
        with self.assertRaises(ValueError): b.set_driving_preference({'setting':'speed_trim','value':3},'trim-disabled')
        for value in (-5,1,5):
            with mock.patch.object(b,'fresh_config_state',return_value=state),mock.patch.object(b.OPENER,'open') as upstream:
                upstream.return_value.__enter__.return_value.read.return_value=json.dumps({'settings':{'speed_trim':value}}).encode()
                self.assertTrue(b.set_driving_preference({'setting':'speed_trim','value':value},'trim-enabled')['applied'])
                self.assertEqual(json.loads(upstream.call_args.args[0].data),{'name':'speed_trim','value':value})
        with mock.patch.object(b,'fresh_config_state',return_value=dict(state,freshness={})),mock.patch.object(b.OPENER,'open') as upstream:
            with self.assertRaises(ValueError): b.set_driving_preference({'setting':'speed_trim','value':3},'trim-enabled')
            upstream.assert_not_called()

    def test_owner_approved_settings(self):
        state = {'car':{'vEgo':0.0},'drive':{'enabled':False,'active':False},'settings':{'follow_distance':4},
                 'freshness':{k:{'available':True} for k in ('vehicle','engagement','settings')}}
        with mock.patch.object(b, 'fresh_config_state', return_value=state):
            proposal_result = b.propose_setting({'setting':'follow_distance','value':5})
            self.assertFalse(proposal_result['applied'])
            page = self.req(urlsplit(proposal_result['approval_url']).path+'?'+urlsplit(proposal_result['approval_url']).query)
            self.assertEqual(page[0],200)
            ticket = re.search('name="ticket" value="([^"]+)"',page[2])[1]
            password = (b.ROOT/'owner-password').read_text().strip()
            with mock.patch.object(b,'apply_setting',return_value={'applied':True}) as apply:
                self.assertEqual(self.req('/settings/approve',{'ticket':ticket,'password':'wrong'},origin='null')[0],403)
                apply.assert_not_called()
                self.assertEqual(self.req('/settings/approve',{'ticket':ticket,'password':password},origin='null')[0],200)
                apply.assert_called_once()
                self.assertEqual(self.req('/settings/approve',{'ticket':ticket,'password':password},origin='null')[0],410)
        for name,value in [('follow_distance',True),('speed_trim',float('nan')),('experimental','true'),('steering',1),('personality','fast')]:
            with self.assertRaises(ValueError): b.normalize_setting(name,value)
        proposal = {'setting':'follow_distance','before':4,'after':5}
        for bad in [dict(state,car={'vEgo':1.0}),dict(state,drive={'enabled':True,'active':True}),dict(state,freshness={}),dict(state,settings={'follow_distance':3})]:
            with mock.patch.object(b,'fresh_config_state',return_value=bad), mock.patch.object(b,'Path') as path, mock.patch.object(b.OPENER,'open') as upstream:
                path.return_value.read_bytes.return_value=b'0'
                with self.assertRaises(ValueError): b.apply_setting(proposal)
                upstream.assert_not_called()
        with mock.patch.object(b,'fresh_config_state',return_value=state), mock.patch.object(b,'Path') as path, mock.patch.object(b.OPENER,'open') as upstream:
            path.return_value.read_bytes.return_value=b'1'
            with self.assertRaises(ValueError): b.apply_setting(proposal)
            upstream.assert_not_called()
        with mock.patch.object(b,'fresh_config_state',return_value=state), mock.patch.object(b,'Path') as path, mock.patch.object(b.OPENER,'open') as upstream:
            path.return_value.read_bytes.return_value=b'0'
            upstream.return_value.__enter__.return_value.read.return_value=b'{"settings":{"follow_distance":5}}'
            self.assertTrue(b.apply_setting(proposal)['applied'])
            request=upstream.call_args.args[0]
            self.assertEqual(request.full_url,'http://127.0.0.1:7070/api/set')
            self.assertEqual(json.loads(request.data),{'name':'follow_distance','value':5})

    def test_chunked_registration(self):
        payload = json.dumps({'redirect_uris':['https://example.com/cb'], 'token_endpoint_auth_method':'none'}).encode()
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.request('POST', '/register', [payload[:20], payload[20:]], {'Content-Type':'application/json'}, encode_chunked=True)
        response = conn.getresponse()
        self.assertEqual(response.status, 201)
        self.assertIn('client_id', json.loads(response.read()))
        conn.close()

    def test_confidential_clients(self):
        for method in ('client_secret_basic', 'client_secret_post'):
            registered = json.loads(self.req('/register', {'redirect_uris':['https://example.com/cb' + str(i) for i in range(6)], 'token_endpoint_auth_method':method})[2])
            secret = registered['client_secret']
            verifier = 'p' * 64
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            grant = dict(client_id=registered['client_id'], redirect_uri=registered['redirect_uris'][0], code_challenge=challenge, resource=b.RESOURCE, scope=b.SCOPE)
            code = b.secrets.token_urlsafe(32)
            b.store('code', b.digest(code), grant, 120)
            exchange = dict(grant_type='authorization_code', client_id=registered['client_id'], redirect_uri=grant['redirect_uri'], code=code, code_verifier=verifier)
            self.assertEqual(self.req('/token', exchange)[0], 401)
            if method == 'client_secret_basic':
                status, _, body = self.req('/token', exchange, basic=registered['client_id'] + ':' + secret)
            else:
                status, _, body = self.req('/token', {**exchange, 'client_secret':secret})
            self.assertEqual(status, 200)
            tokens = json.loads(body)
            self.assertEqual(self.req('/mcp', token=tokens['access_token'])[0], 200)
            saved = b.load('client', registered['client_id'])
            self.assertNotIn('client_secret', saved)
            self.assertEqual(saved['secret_hash'], b.digest(secret))

if __name__ == '__main__': unittest.main()
