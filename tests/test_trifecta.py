from contextlib import redirect_stderr, redirect_stdout
import copy
import datetime as dt
import http.server
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/orchestrate-trifecta'
spec = importlib.util.spec_from_file_location('trifecta_usage', SKILL / 'scripts/usage.py')
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)

NOW = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc)
CLAUDE = {'five_hour_used': 'Anthropic-Ratelimit-Unified-5h-Utilization',
          'five_hour_reset': 'Anthropic-Ratelimit-Unified-5h-Reset',
          'weekly_used': 'Anthropic-Ratelimit-Unified-7d-Utilization',
          'weekly_reset': 'Anthropic-Ratelimit-Unified-7d-Reset',
          'status': 'Anthropic-Ratelimit-Unified-Status'}


def epoch(minutes):
    return str(int((NOW + dt.timedelta(minutes=minutes)).timestamp()))


def observed(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat()


def claude_auth(five_hour, five_reset_min, weekly=0.2, age=5, status='allowed', weekly_reset_min=3 * 24 * 60):
    signals = {CLAUDE['five_hour_used']: str(five_hour), CLAUDE['five_hour_reset']: epoch(five_reset_min),
               CLAUDE['weekly_used']: str(weekly), CLAUDE['weekly_reset']: epoch(weekly_reset_min),
               CLAUDE['status']: status}
    return {'provider': 'claude', 'quota': {'signals': signals, 'observed_at': observed(age)}}


def codex_auth(percent, age=5):
    # An older account-level reading and a newer per-model one: the newer wins.
    return {'provider': 'codex',
            'quota': {'signals': {'X-Codex-Primary-Used-Percent': '1'}, 'observed_at': observed(age + 60)},
            'model_quotas': {'gpt': {'signals': {'X-Codex-Primary-Used-Percent': str(percent),
                                                 'X-Codex-Primary-Reset-At': epoch(2 * 24 * 60)},
                                     'observed_at': observed(age)}}}


def run(argv, environ, fetch=None, sleep=None):
    out = io.StringIO()
    fetch = fetch or (lambda url, key: (_ for _ in ()).throw(AssertionError('unexpected network request')))
    with redirect_stdout(out), redirect_stderr(out):
        code = usage.main(argv, environ=environ, fetch=fetch, now=lambda: NOW, sleep=sleep or (lambda s: None))
    return code, out.getvalue()


class FakeGateway:
    """A CLIProxyAPI-shaped management endpoint on 127.0.0.1."""

    def __init__(self, files):
        self.files, self.requests = files, []
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                gateway.requests.append((self.path, self.headers.get('Authorization')))
                body = json.dumps({'files': gateway.files}).encode()
                self.send_response(200 if self.headers.get('Authorization') == 'Bearer fixture-key' else 401)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TrifectaConfigTests(unittest.TestCase):
    def setUp(self):
        self.example = json.loads((SKILL / 'trifecta.example.json').read_text())

    def test_example_config_is_valid_and_matches_the_documented_defaults(self):
        config, errors = usage.validate(copy.deepcopy(self.example))
        self.assertEqual(errors, [])
        self.assertEqual([r['id'] for r in usage.roles(config)],
                         ['coordinator', 'builder1', 'builder2', 'reviewer1', 'reviewer2'])
        self.assertEqual([b['switch_at_percent'] for b in config['builders']], [85, 85])
        self.assertEqual([r['weight'] for r in config['reviewers']], [7, 3])
        self.assertEqual((config['max_parallel_builders'], config['stale_minutes']), (2, 20))

    def test_defaults_fill_in_omitted_fields(self):
        raw = copy.deepcopy(self.example)
        for key in ('max_parallel_builders', 'stale_minutes', 'never_use'):
            raw.pop(key)
        for item in raw['builders']:
            item.pop('switch_at_percent')
        for item in raw['reviewers']:
            item.pop('weight')
        config, errors = usage.validate(raw)
        self.assertEqual(errors, [])
        self.assertEqual([r['weight'] for r in config['reviewers']], [7, 3])
        self.assertEqual(config['builders'][0]['switch_at_percent'], 85)
        self.assertEqual((config['max_parallel_builders'], config['stale_minutes'], config['never_use']), (2, 20, []))

    def test_invalid_configs_are_reported(self):
        cases = {
            'builders: must be a non-empty list': lambda c: c.update(builders=[]),
            "config: unknown key 'max_parallel_builder'": lambda c: c.update(max_parallel_builder=3),
            'builders[0].providerInstanceId: listed in never_use': lambda c: c['never_use'].append('claudeAgent_builder'),
            "reviewers[0].usage: no usage source named 'missing'": lambda c: c['reviewers'][0].update(usage='missing'),
            'usage_sources.builder-claude.url: must be an http(s) URL': lambda c: c['usage_sources']['builder-claude'].update(url='ftp://x'),
            'reviewers[0].weight: required, a positive number': lambda c: c['reviewers'][0].update(
                providerInstanceId='other', model='other-model', weight=None) or c['reviewers'][0].pop('weight'),
            'builders[1].switch_at_percent: a number from 1 to 100': lambda c: c['builders'][1].update(switch_at_percent=150),
            'usage_sources.reviewer-codex.type: only "cliproxyapi" is supported': lambda c: c['usage_sources']['reviewer-codex'].update(type='other'),
            'builders[0].model: required': lambda c: c['builders'][0].pop('model'),
        }
        for message, change in cases.items():
            with self.subTest(message):
                raw = copy.deepcopy(self.example)
                change(raw)
                self.assertIn(message, usage.validate(raw)[1])

    def test_check_config_prints_the_role_table_without_network(self):
        code, output = run(['--check-config'], {'TRIFECTA_CONFIG': str(SKILL / 'trifecta.example.json')})
        self.assertEqual(code, 0)
        for text in ['builder1', 'claudeAgent_builder', 'switch at 85%', 'weight 7 (70% of reviews)',
                     'weight 3 (30% of reviews)', 'max_parallel_builders: 2', 'never_use: claudeAgent_direct']:
            self.assertIn(text, output)


class NoConfigTests(unittest.TestCase):
    def test_missing_config_reports_and_never_touches_the_network(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch('urllib.request.urlopen', side_effect=AssertionError('network')) as urlopen:
            environ = {'TRIFECTA_CONFIG': str(Path(temp) / 'trifecta.json'),
                       'TRIFECTA_BUILDER_GATEWAY_URL': 'http://127.0.0.1:9'}
            code, output = run(['--json'], environ, fetch=usage.fetch_auth_files)
            self.assertEqual(code, 0)
            report = json.loads(output)
            self.assertFalse(report['configured'])
            self.assertIn('copy trifecta.example.json', report['missing'][0])
            self.assertEqual(run([], environ, fetch=usage.fetch_auth_files)[0], 0)
            self.assertEqual(run(['--check-config'], environ, fetch=usage.fetch_auth_files)[0], 1)
            self.assertEqual(run(['--sleep-until-reset', 'builder1'], environ, fetch=usage.fetch_auth_files)[0], 1)
            urlopen.assert_not_called()

    def test_invalid_config_fails_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'trifecta.json'
            path.write_text(json.dumps({'coordinator': {'label': 'x'}}))
            code, output = run(['--json'], {'TRIFECTA_CONFIG': str(path)})
            self.assertEqual(code, 1)
            self.assertIn('builders: must be a non-empty list', output)


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'key.json').write_text(json.dumps({'management_key': 'fixture-key'}))
        self.config = json.loads((SKILL / 'trifecta.example.json').read_text())
        for source in self.config['usage_sources'].values():
            source['key_file'] = str(root / 'key.json')
        self.main = FakeGateway([claude_auth(0.4, 120), codex_auth(35)])
        self.builder = FakeGateway([claude_auth(0.9, 30, age=45)])
        for name, source in self.config['usage_sources'].items():
            source['url'] = (self.builder if name == 'builder-claude' else self.main).url
        self.path = root / 'trifecta.json'
        self.path.write_text(json.dumps(self.config))
        self.environ = {'TRIFECTA_CONFIG': str(self.path)}

    def tearDown(self):
        self.main.close()
        self.builder.close()
        self.temp.cleanup()

    def test_readings_per_role_from_a_local_gateway(self):
        code, output = run(['--json'], self.environ, fetch=usage.fetch_auth_files)
        self.assertEqual(code, 0)
        roles = json.loads(output)['roles']
        self.assertEqual(roles['coordinator']['five_hour']['used'], 0.4)
        self.assertEqual(roles['coordinator']['weekly']['used'], 0.2)
        self.assertEqual(roles['reviewer1']['weekly']['used'], 0.35)  # percent source, newest view
        self.assertIsNone(roles['reviewer1']['five_hour'])
        self.assertTrue(roles['builder1']['at_threshold'])
        self.assertFalse(roles['builder2']['at_threshold'])
        self.assertEqual(roles['builder1'], {**roles['reviewer2'], 'kind': 'builder', 'label': 'Builder seat',
                                             'at_threshold': True,
                                             'blocking_reset': roles['builder1']['five_hour']['resets_at']})
        self.assertIsNone(roles['reviewer2']['blocking_reset'])  # 90% blocks a builder, not a reviewer
        # One request per gateway, however many roles share it, with the key from the key file.
        self.assertEqual(len(self.main.requests), 1)
        self.assertEqual(self.main.requests[0], ('/v0/management/auth-files', 'Bearer fixture-key'))
        self.assertEqual(len(self.builder.requests), 1)

    def test_stale_readings_are_marked(self):
        code, output = run(['--json'], self.environ, fetch=usage.fetch_auth_files)
        roles = json.loads(output)['roles']
        self.assertEqual((roles['builder1']['age_minutes'], roles['builder1']['stale']), (45, True))
        self.assertEqual((roles['coordinator']['age_minutes'], roles['coordinator']['stale']), (5, False))
        code, text = run([], self.environ, fetch=usage.fetch_auth_files)
        builder_line = next(line for line in text.splitlines() if line.startswith('builder1'))
        self.assertIn('read 45 min ago, STALE', builder_line)
        self.assertIn('AT THRESHOLD', builder_line)
        self.config['stale_minutes'] = 60
        self.path.write_text(json.dumps(self.config))
        roles = json.loads(run(['--json'], self.environ, fetch=usage.fetch_auth_files)[1])['roles']
        self.assertFalse(roles['builder1']['stale'])

    def test_rejected_status_and_rolled_over_windows(self):
        self.builder.files = [claude_auth(0.3, 30, status='rejected')]
        roles = json.loads(run(['--json'], self.environ, fetch=usage.fetch_auth_files)[1])['roles']
        self.assertTrue(roles['builder1']['at_threshold'])
        # A rejection from before the reset no longer holds.
        self.builder.files = [claude_auth(1.0, -10, status='rejected')]
        roles = json.loads(run(['--json'], self.environ, fetch=usage.fetch_auth_files)[1])['roles']
        self.assertEqual(roles['builder1']['five_hour'], {'used': 0.0, 'resets_at': None, 'rolled_over': True})
        self.assertFalse(roles['builder1']['at_threshold'])
        # A rejection without window numbers still blocks.
        self.builder.files = [{'provider': 'claude', 'quota': {'signals': {CLAUDE['status']: 'rejected'},
                                                               'observed_at': observed(5)}}]
        roles = json.loads(run(['--json'], self.environ, fetch=usage.fetch_auth_files)[1])['roles']
        self.assertTrue(roles['builder1']['at_threshold'])

    def test_weekly_exhaustion_holds_after_the_five_hour_reset(self):
        self.builder.files = [claude_auth(0.3, -10, weekly=1.0, status='rejected')]
        roles = json.loads(run(['--json'], self.environ, fetch=usage.fetch_auth_files)[1])['roles']
        self.assertTrue(roles['builder1']['five_hour']['rolled_over'])
        self.assertTrue(roles['builder1']['at_threshold'])
        self.assertEqual(roles['builder1']['blocking_reset'], roles['builder1']['weekly']['resets_at'])
        self.assertEqual(roles['reviewer2']['blocking_reset'], roles['builder1']['weekly']['resets_at'])
        # Weekly use at the threshold blocks a builder without a rejection.
        self.builder.files = [claude_auth(0.1, 30, weekly=0.9)]
        roles = json.loads(run(['--json'], self.environ, fetch=usage.fetch_auth_files)[1])['roles']
        self.assertTrue(roles['builder1']['at_threshold'])

    def test_errors_never_print_the_key_url_credentials_or_response(self):
        key = 'fixture-secret-key'
        (Path(self.temp.name) / 'key.json').write_text(json.dumps({'management_key': key + '\n'}))
        environ = {**self.environ, 'TRIFECTA_COORDINATOR_GATEWAY_URL': f'http://fixture-user:fixture-pass@127.0.0.1:no-port'}
        self.builder.files = [{'provider': 'claude', 'quota': {'signals': {'x': '1'}, 'observed_at': 'fixture-body'}}]
        outputs = [run(['--json'], environ, fetch=usage.fetch_auth_files)[1], run([], environ, fetch=usage.fetch_auth_files)[1],
                   run(['--check-config'], environ)[1], run(['--sleep-until-reset', 'builder1'], environ, fetch=usage.fetch_auth_files)[1]]
        roles = json.loads(outputs[0])['roles']
        self.assertEqual(roles['builder1']['error'],
                         'request not sent, check the key file (ValueError) on usage source builder-claude')
        self.assertEqual(roles['coordinator']['error'],
                         'gateway URL is invalid (InvalidURL) on usage source coordinator-claude')
        # A response the parser cannot read is reported by class only.
        (Path(self.temp.name) / 'key.json').write_text(json.dumps({'management_key': 'fixture-key'}))
        outputs.append(run(['--json'], self.environ, fetch=usage.fetch_auth_files)[1])
        self.assertEqual(json.loads(outputs[-1])['roles']['builder1']['error'],
                         'unreadable reading (ValueError) on usage source builder-claude')
        # A refused key: the status code, never the body.
        (Path(self.temp.name) / 'key.json').write_text(json.dumps({'management_key': 'wrong-key'}))
        outputs.append(run(['--json'], self.environ, fetch=usage.fetch_auth_files)[1])
        self.assertEqual(json.loads(outputs[-1])['roles']['builder1']['error'],
                         'gateway answered HTTP 401 (HTTPError) on usage source builder-claude')
        for output in outputs:
            for secret in (key, 'wrong-key', 'fixture-user', 'fixture-pass', 'fixture-body', 'Bearer'):
                self.assertNotIn(secret, output)

    def test_down_gateway_is_reported_per_role(self):
        self.builder.close()
        roles = json.loads(run(['--json'], self.environ, fetch=usage.fetch_auth_files)[1])['roles']
        self.assertIn('error', roles['builder1'])
        self.assertIn('error', roles['reviewer2'])
        self.assertNotIn('error', roles['coordinator'])
        self.builder = FakeGateway([])  # for tearDown

    def test_environment_overrides_the_role_source(self):
        self.config['usage_sources']['builder-claude']['url'] = 'http://127.0.0.1:9'
        self.path.write_text(json.dumps(self.config))
        environ = {**self.environ, 'TRIFECTA_BUILDER_GATEWAY_URL': self.builder.url}
        roles = json.loads(run(['--json'], environ, fetch=usage.fetch_auth_files)[1])['roles']
        self.assertEqual(roles['builder1']['five_hour']['used'], 0.9)
        self.assertIn('TRIFECTA_BUILDER_GATEWAY_URL', run(['--check-config'], environ)[1])
        environ['TRIFECTA_BUILDER_GATEWAY_URL'] = 'not-a-url'
        self.assertEqual(run(['--json'], environ)[0], 1)


class SleepUntilResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'key.json').write_text(json.dumps({'management_key': 'fixture-key'}))
        config = json.loads((SKILL / 'trifecta.example.json').read_text())
        for source in config['usage_sources'].values():
            source['key_file'] = str(root / 'key.json')
        (root / 'trifecta.json').write_text(json.dumps(config))
        self.environ = {'TRIFECTA_CONFIG': str(root / 'trifecta.json')}
        self.naps = []

    def tearDown(self):
        self.temp.cleanup()

    def clock(self, builder_auth):
        def fetch(url, key):
            return [builder_auth] if url.endswith(':8318') else [claude_auth(0.1, 60), codex_auth(10)]
        return run(['--sleep-until-reset', 'builder1'], self.environ, fetch=fetch, sleep=self.naps.append)

    def test_reset_within_one_nap_exits_zero_after_sleeping_past_it(self):
        code, output = self.clock(claude_auth(1.0, 30, status='rejected'))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.naps), 1)
        self.assertGreaterEqual(self.naps[0], 30 * 60)
        self.assertLessEqual(self.naps[0], 31 * 60)
        self.assertIn('window has reopened', output)

    def test_weekly_only_block_waits_for_the_weekly_reset(self):
        code, output = self.clock(claude_auth(0.3, -10, weekly=1.0, status='rejected', weekly_reset_min=3 * 60))
        self.assertEqual((code, self.naps), (3, [usage.MAX_NAP_SECONDS]))
        self.naps.clear()
        code, output = self.clock(claude_auth(0.3, 30, weekly=1.0, status='rejected', weekly_reset_min=90))
        self.assertEqual(code, 0)
        self.assertTrue(90 * 60 <= self.naps[0] <= 91 * 60)  # past the weekly reset, not the 5-hour one

    def test_five_hour_only_block_waits_for_the_five_hour_reset(self):
        code, output = self.clock(claude_auth(1.0, 30, weekly=0.4, status='rejected', weekly_reset_min=90))
        self.assertEqual(code, 0)
        self.assertTrue(30 * 60 <= self.naps[0] <= 31 * 60)

    def test_both_blocking_waits_for_the_later_reset(self):
        code, output = self.clock(claude_auth(1.0, 60, weekly=1.0, status='rejected', weekly_reset_min=20))
        self.assertEqual(code, 0)
        self.assertTrue(60 * 60 <= self.naps[0] <= 61 * 60)
        self.naps.clear()
        code, output = self.clock(claude_auth(1.0, 30, weekly=1.0, status='rejected'))
        self.assertEqual((code, self.naps), (3, [usage.MAX_NAP_SECONDS]))

    def test_reset_beyond_one_nap_exits_three(self):
        code, output = self.clock(claude_auth(1.0, 4 * 60, status='rejected'))
        self.assertEqual(code, 3)
        self.assertEqual(self.naps, [usage.MAX_NAP_SECONDS])
        self.assertIn('run this again', output)

    def test_no_pending_reset_exits_zero_without_sleeping(self):
        code, output = self.clock(claude_auth(0.5, -5))
        self.assertEqual((code, self.naps), (0, []))
        self.assertIn('probe the account now', output)
        code, output = self.clock({'provider': 'claude'})
        self.assertEqual((code, self.naps), (0, []))
        self.assertIn('no reading yet', output)

    def test_unknown_role_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            run(['--sleep-until-reset', 'neither'], self.environ)
        self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
