import contextlib
import copy
import email.header
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import fleetconfig
import fleetlib as fleet


class FakeHTTP:
    """Records each POST instead of touching the network."""
    def __init__(self, status=204):
        self.calls, self.status = [], status

    def __call__(self, url, data, headers):
        self.calls.append({'url': url, 'data': data, 'headers': headers})
        return self.status


class FleetCLITestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'FLEET_USER_HOME': str(self.root), 'FLEET_STATE_DIR': str(self.root / 'state'), 'FLEET_MACHINE': 'hub'})
        self.env.start()
        # The agent running these tests has its own session variables; none of them may leak into a fixture.
        for name in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
            os.environ.pop(name, None)
        self.config = {'schema_version': 1, 'owner': 'Example Owner', 'state_dir': '~/state',
                       'machines': {'hub': {'os': 'macos', 'user': 'example-user', 'address': '100.64.0.1', 'ssh': 'hub-alias', 'repo': '~/fleet', 'roles': ['development']},
                                    'peer': {'os': 'windows', 'user': 'example-user', 'address': '100.64.0.2', 'ssh': 'peer-alias', 'work_root': 'D:\\AgentWork', 'roles': ['windows']}},
                       'profiles': {'personal-claude': {'driver': 'claude', 'identity': 'personal', 'home': '~/.claude', 'managed': False},
                                    'work-codex': {'driver': 'codex', 'identity': 'work', 'home': '~/.codex_work', 'managed': False}},
                       'secrets': {'host': 'hub'},
                       'notify': {'host': 'hub', 'channels': [{'name': 'discord', 'type': 'discord-webhook', 'url': {'secret': 'personal/PAGE_WEBHOOK'}}]}}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def store(self, name, value, scope='personal', note='Bot token for the example app', replace=False):
        sender = {'agent': 'a', 'machine': 'hub', 'profile': 'personal-claude', 'folder': 'bot'}
        return fleet.secret_local(self.config, {'action': 'set', 'scope': scope, 'name': name, 'value': value, 'note': note, 'replace': replace, 'sender': sender})


class PageTests(FleetCLITestCase):
    def setUp(self):
        super().setUp()
        self.store('PAGE_WEBHOOK', 'https://hooks.example.com/discord-fixture')

    def page(self, text, **kwargs):
        with patch.object(fleet.sys, 'stdout', io.StringIO()) as out:
            fleet.deliver_page(self.config, fleet.page_sender(self.config), text, **kwargs)
        return out.getvalue()

    def test_text_limits_come_from_fleet_json(self):
        self.assertEqual(fleet.page_text(self.config, ' UAC is waiting.\n Approve it. '), 'UAC is waiting. Approve it.')
        for text in ['', 'One. Two. Three. Four.', 'x' * 501]:
            with self.assertRaisesRegex(ValueError, 'one to 3 sentences and at most 500'):
                fleet.page_text(self.config, text)
        self.config['notify'].update(max_sentences=1, max_chars=20)
        with self.assertRaisesRegex(ValueError, 'one to 1 sentences and at most 20'):
            fleet.page_text(self.config, 'One. Two.')
        with self.assertRaisesRegex(ValueError, 'at most 20 characters'):
            fleet.page_text(self.config, 'A single long sentence here.')

    def test_each_channel_type_sends_through_the_injected_sender(self):
        os.environ['PAGE_NTFY_TOPIC'] = 'fixture-topic'
        self.store('NTFY_TOKEN', 'ntfy-token')
        self.store('SLACK_URL', 'https://hooks.example.com/slack-fixture')
        channels = {
            'discord-webhook': {'type': 'discord-webhook', 'url': {'secret': 'personal/PAGE_WEBHOOK'}},
            'slack-webhook': {'type': 'slack-webhook', 'url': {'secret': 'personal/SLACK_URL'}},
            'ntfy': {'type': 'ntfy', 'server': 'https://ntfy.example.com/', 'topic': {'env': 'PAGE_NTFY_TOPIC'}, 'token': {'secret': 'personal/NTFY_TOKEN'}, 'priority': 'high'},
        }
        for kind, channel in channels.items():
            with self.subTest(kind):
                http = FakeHTTP()
                self.config['notify']['channels'] = [channel]
                os.environ['CLAUDE_CODE_SESSION_ID'] = 'agent-' + kind
                self.assertIn(fleet.PAGE_SENT, self.page('UAC is waiting.', http=http))
                [call] = http.calls
                if kind == 'discord-webhook':
                    self.assertEqual(call['url'], 'https://hooks.example.com/discord-fixture')
                    body = json.loads(call['data'])
                    self.assertTrue(body['content'].endswith('\nUAC is waiting.'))
                    self.assertEqual(body['allowed_mentions'], {'parse': []})
                elif kind == 'slack-webhook':
                    self.assertEqual(call['url'], 'https://hooks.example.com/slack-fixture')
                    self.assertTrue(json.loads(call['data'])['text'].startswith('*hub · personal-claude · '))
                else:
                    self.assertEqual(call['url'], 'https://ntfy.example.com/fixture-topic')
                    self.assertEqual(call['data'], b'UAC is waiting.')
                    self.assertEqual((call['headers']['Authorization'], call['headers']['Priority']), ('Bearer ntfy-token', 'high'))
                    title = call['headers']['Title']
                    self.assertTrue(title.isascii(), title)
                    self.assertEqual(str(email.header.make_header(email.header.decode_header(title))), 'hub · personal-claude · ' + Path.cwd().name)
        commands = {
            'openclaw': ({'type': 'openclaw', 'binary': '/opt/openclaw', 'channel': 'discord', 'account': 'helper', 'target': 'user:1'},
                         lambda argv, stdin: self.assertEqual(argv[:9], ['/opt/openclaw', 'message', 'send', '--channel', 'discord', '--target', 'user:1', '--account', 'helper'])
                         or self.assertTrue(argv[-1].endswith('\nUAC is waiting.'))),
            'command': ({'type': 'command', 'argv': ['/usr/local/bin/notify-owner', '--urgent']},
                        lambda argv, stdin: self.assertEqual((argv, stdin.splitlines()[-1]), (['/usr/local/bin/notify-owner', '--urgent'], 'UAC is waiting.'))),
        }
        for kind, (channel, check) in commands.items():
            with self.subTest(kind):
                run = unittest.mock.Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
                self.config['notify']['channels'] = [channel]
                os.environ['CLAUDE_CODE_SESSION_ID'] = 'agent-' + kind
                self.page('UAC is waiting.', http=FakeHTTP(), run=run)
                check(run.call_args.args[0], run.call_args.kwargs['input'])

    def test_ntfy_titles_outside_latin_1_go_as_an_encoded_word(self):
        title = 'hub · 工作 · 🚀'
        request = fleet.page_request(self.config, {'type': 'ntfy', 'topic': {'env': 'PAGE_NTFY_TOPIC'}}, title, 'x', resolve=False)
        header = request['headers']['Title']
        self.assertTrue(header.isascii(), header)
        self.assertEqual(str(email.header.make_header(email.header.decode_header(header))), title)

    def test_each_agent_has_its_own_limit_and_it_expires(self):
        http = FakeHTTP()
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'first'}):
            self.page('UAC is waiting.', http=http)
            with self.assertRaisesRegex(ValueError, 'already paged the owner in the last 30 minutes'):
                self.page('Still waiting.', http=http)
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'second'}):
            self.page('macOS wants a password.', http=http)
        self.assertEqual(len(http.calls), 2)
        record = next(p for p in (self.root / 'state/pages').glob('*.json') if fleet.read_json(p)['agent'] == 'first')
        earlier = fleet.dt.datetime.now(fleet.dt.timezone.utc) - fleet.dt.timedelta(minutes=31)
        fleet.atomic_write(record, fleet.encode({**fleet.read_json(record), 'sent_at': earlier.isoformat()}))
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'first'}):
            self.page('UAC is waiting again.', http=http)
        statuses = [json.loads(line)['status'] for line in (self.root / 'state/pages/log.jsonl').read_text().splitlines()]
        self.assertEqual(statuses, ['sent', 'refused', 'sent', 'sent'])

    def test_dry_run_sends_nothing_reads_no_secret_and_keeps_the_limit(self):
        http = FakeHTTP()
        os.environ['CLAUDE_CODE_SESSION_ID'] = 'agent'
        with patch.object(fleet, 'secret', side_effect=AssertionError('dry run read the vault')):
            shown = self.page('UAC is waiting.', dry_run=True, http=http)
        self.assertEqual(http.calls, [])
        self.assertIn('vault secret personal/PAGE_WEBHOOK', shown)
        self.assertIn('UAC is waiting.', shown)
        self.assertNotIn('hooks.example.com', shown)
        self.assertFalse((self.root / 'state/pages').exists())
        self.page('UAC is waiting.', http=http)
        self.assertEqual(len(http.calls), 1)

    def test_failed_channel_falls_through_and_failure_keeps_the_limit_free(self):
        os.environ['CLAUDE_CODE_SESSION_ID'] = 'agent'
        os.environ['PAGE_NTFY_TOPIC'] = 'fixture-topic'
        self.config['notify']['channels'].append({'name': 'phone', 'type': 'ntfy', 'topic': {'env': 'PAGE_NTFY_TOPIC'}})
        with self.assertRaisesRegex(ValueError, 'discord: HTTP 500; phone: HTTP 500'):
            self.page('UAC is waiting.', http=FakeHTTP(500))
        responses = iter([500, 200])
        http = unittest.mock.Mock(side_effect=lambda *a: next(responses))
        self.page('UAC is waiting.', http=http)
        self.assertEqual(http.call_count, 2)
        log = [json.loads(line) for line in (self.root / 'state/pages/log.jsonl').read_text().splitlines()]
        self.assertEqual([(e['status'], e.get('channel')) for e in log], [('failed', None), ('sent', 'phone')])

    def test_failures_never_show_a_secret_value(self):
        sentinel = 'SENTINEL-7f3a-not-a-url'
        os.environ['CLAUDE_CODE_SESSION_ID'] = 'agent'
        self.store('PAGE_WEBHOOK', sentinel, replace=True)
        self.store('NTFY_TOKEN', sentinel + '-token')
        self.config['notify']['channels'] += [
            {'name': 'phone', 'type': 'ntfy', 'server': 'https://ntfy.example.com', 'topic': {'secret': 'personal/PAGE_WEBHOOK'}, 'token': {'secret': 'personal/NTFY_TOKEN'}},
            {'name': 'script', 'type': 'command', 'argv': ['/usr/local/bin/notify-owner']}]

        def http(url, data, headers):
            # The real sender quotes a malformed URL in its error; a network failure can quote the request.
            if 'ntfy' not in url:
                return fleet.http_post(url, data, headers)
            raise OSError(f'could not reach {url} with {headers}')
        # A command can print its environment or arguments when it fails.
        run = unittest.mock.Mock(return_value=subprocess.CompletedProcess([], 1, f'url {sentinel}', f'token {sentinel}'))
        with patch.object(fleet.sys, 'stdout', io.StringIO()) as out, patch.object(fleet.sys, 'stderr', io.StringIO()) as err:
            with self.assertRaises(ValueError) as caught:
                fleet.deliver_page(self.config, fleet.page_sender(self.config), 'UAC is waiting.', http=http, run=run)
        self.assertEqual(str(caught.exception), 'No notify channel accepted the page: discord: ValueError; phone: OSError; script: exit status 1')
        log = (self.root / 'state/pages/log.jsonl').read_text()
        self.assertEqual(json.loads(log)['errors'], ['discord: ValueError', 'phone: OSError', 'script: exit status 1'])
        for shown in (str(caught.exception), log, out.getvalue(), err.getvalue()):
            self.assertNotIn('SENTINEL', shown)
        # The same through the command line, where the error reaches stderr.
        (self.root / 'state/pages/log.jsonl').unlink()
        self.config['notify']['channels'] = self.config['notify']['channels'][:1]
        config = self.root / 'fleet.json'
        config.write_text(json.dumps(self.config))
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/fleetlib.py'), 'page', 'UAC is waiting.'], capture_output=True, text=True,
                                env=dict(os.environ, FLEET_CONFIG=str(config), CLAUDE_CODE_SESSION_ID='cli-agent'))
        self.assertEqual((result.returncode, result.stderr), (1, 'fleet: No notify channel accepted the page: discord: ValueError\n'))
        self.assertNotIn('SENTINEL', result.stdout + (self.root / 'state/pages/log.jsonl').read_text())

    def test_concurrent_pages_from_one_agent_send_once(self):
        os.environ['CLAUDE_CODE_SESSION_ID'] = 'agent'
        http, barrier, outcomes = FakeHTTP(), threading.Barrier(2, timeout=0.5), []

        def slow(url, data, headers):
            # Without a lock, both pages are past the limit check by the time either is sent.
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
            return http(url, data, headers)

        def attempt():
            try:
                fleet.deliver_page(self.config, fleet.page_sender(self.config), 'UAC is waiting.', http=slow)
                outcomes.append('sent')
            except ValueError as error:
                outcomes.append(str(error))
        with patch.object(fleet.sys, 'stdout', io.StringIO()):
            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(sorted(outcomes), ['This agent already paged the owner in the last 30 minutes. Put the blocker in your report instead.', 'sent'])

    def test_other_machines_relay_over_the_hosts_ssh_alias(self):
        os.environ['FLEET_MACHINE'] = 'peer'
        os.environ['CODEX_THREAD_ID'] = 'thread'
        os.environ['CODEX_HOME'] = str(self.root / '.codex_work')
        with patch.object(fleet.subprocess, 'run') as run, patch.object(fleet.sys, 'stdout', io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'one to 3'):
                fleet.page(self.config, 'One. Two. Three. Four.')
            run.assert_not_called()
            run.return_value = subprocess.CompletedProcess([], 0, '', 'fleet: This agent already paged the owner in the last 30 minutes.')
            with self.assertRaisesRegex(ValueError, 'already paged'):
                fleet.page(self.config, 'UAC is waiting on peer.')
            run.return_value = subprocess.CompletedProcess([], 0, fleet.PAGE_SENT + '\n', '')
            fleet.page(self.config, 'UAC is waiting on peer.')
        self.assertEqual(run.call_args.args[0], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'hub-alias', '~/fleet/scripts/fleet', 'page', '--relay'])
        payload = json.loads(run.call_args.kwargs['input'])
        self.assertEqual((payload['sender']['agent'], payload['sender']['machine'], payload['sender']['profile']), ('thread', 'peer', 'work-codex'))
        os.environ['FLEET_MACHINE'] = 'hub'
        http = FakeHTTP()
        with patch.object(fleet.sys, 'stdin', io.StringIO(json.dumps(payload))), patch.object(fleet, 'http_post', http), \
                patch.object(fleet.sys, 'stdout', io.StringIO()) as out:
            fleet.page(self.config)
        self.assertIn(fleet.PAGE_SENT, out.getvalue())
        self.assertTrue(json.loads(http.calls[0]['data'])['content'].startswith('**peer · work-codex · '))

    def test_unconfigured_paging_says_so(self):
        del self.config['notify']
        with self.assertRaisesRegex(ValueError, 'not configured'):
            fleet.page(self.config, 'UAC is waiting.')


class SecretTests(FleetCLITestCase):
    def test_round_trip_and_listings_never_show_values(self):
        self.store('DISCORD_BOT_TOKEN', 'token-1\n')
        self.store('WORK_API_KEY', 'work-token', scope='work', note='Work API key')
        get = {'action': 'get', 'scope': 'personal', 'name': 'DISCORD_BOT_TOKEN'}
        self.assertEqual(fleet.secret_local(self.config, get), {'value': 'token-1'})
        listing = fleet.secret_local(self.config, {'action': 'list'})['secrets']
        self.assertEqual([(r['scope'], r['name']) for r in listing], [('personal', 'DISCORD_BOT_TOKEN'), ('work', 'WORK_API_KEY')])
        self.assertFalse({'token-1', 'work-token'} & {str(v) for r in listing for v in r.values()})
        self.assertEqual(len(fleet.secret_local(self.config, {'action': 'list', 'scope': 'work'})['secrets']), 1)
        with self.assertRaisesRegex(ValueError, 'No work secret named DISCORD_BOT_TOKEN'):
            fleet.secret_local(self.config, {**get, 'scope': 'work'})
        if os.name != 'nt':
            path = self.root / 'state/secrets/personal/DISCORD_BOT_TOKEN.json'
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_never_replaced_by_accident_and_old_copies_are_kept(self):
        self.store('DISCORD_BOT_TOKEN', 'token-1')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.store('DISCORD_BOT_TOKEN', 'token-2')
        self.store('DISCORD_BOT_TOKEN', 'token-2', note=None, replace=True)
        record = fleet.read_json(self.root / 'state/secrets/personal/DISCORD_BOT_TOKEN.json')
        self.assertEqual((record['value'], record['note']), ('token-2', 'Bot token for the example app'))
        old = list((self.root / 'state/secrets/personal/replaced').glob('DISCORD_BOT_TOKEN-*.json'))
        self.assertEqual([fleet.read_json(path)['value'] for path in old], ['token-1'])
        fleet.secret_local(self.config, {'action': 'remove', 'scope': 'personal', 'name': 'DISCORD_BOT_TOKEN'})
        self.assertEqual(fleet.secret_local(self.config, {'action': 'list'})['secrets'], [])
        self.assertEqual(len(list((self.root / 'state/secrets/personal/removed').glob('*.json'))), 1)

    def race(self, *writes):
        """Run vault writes at once. Each pauses just before it writes, so a separate check lets every one through."""
        barrier, encode, results = threading.Barrier(len(writes), timeout=0.5), fleet.encode, [None] * len(writes)

        def paused(value):
            if isinstance(value, dict) and 'value' in value:
                with contextlib.suppress(threading.BrokenBarrierError):
                    barrier.wait()
            return encode(value)

        def attempt(i, write):
            try:
                results[i] = self.store(**write)['saved']
            except ValueError as error:
                results[i] = str(error)
        with patch.object(fleet, 'encode', paused):
            threads = [threading.Thread(target=attempt, args=(i, write)) for i, write in enumerate(writes)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        return results

    def test_concurrent_writes_keep_the_overwrite_guard_and_every_old_value(self):
        first = self.race({'name': 'BOT', 'value': 'token-a'}, {'name': 'BOT', 'value': 'token-b'})
        self.assertEqual(sorted(r.split('.')[0] for r in first), ['Saved personal BOT', 'personal BOT already exists'])
        stored = fleet.read_json(self.root / 'state/secrets/personal/BOT.json')['value']
        self.assertEqual(first[['token-a', 'token-b'].index(stored)], 'Saved personal BOT.')
        rotated = self.race({'name': 'BOT', 'value': 'token-c', 'note': None, 'replace': True}, {'name': 'BOT', 'value': 'token-d', 'note': None, 'replace': True})
        self.assertEqual(rotated, ['Saved personal BOT.'] * 2)
        kept = [fleet.read_json(path)['value'] for path in (self.root / 'state/secrets/personal/replaced').glob('BOT-*.json')]
        final = fleet.read_json(self.root / 'state/secrets/personal/BOT.json')['value']
        self.assertEqual(sorted(kept + [final]), sorted([stored, 'token-c', 'token-d']))

    def test_scopes_default_to_identities_and_can_be_configured(self):
        cases = [('BOT', 'v', 'school', 'n', 'scope must be one of: personal, work'), ('bot-token', 'v', 'personal', 'n', 'environment variable name'),
                 ('../BOT', 'v', 'personal', 'n', 'environment variable name'), ('BOT', '\n', 'personal', 'n', 'non-empty'),
                 ('BOT', 'x' * (64 * 1024 + 1), 'personal', 'n', 'at most 64 KB'), ('BOT', 'v', 'personal', None, '--note')]
        for name, value, scope, note, message in cases:
            with self.assertRaisesRegex(ValueError, message):
                self.store(name, value, scope=scope, note=note)
        self.assertFalse((self.root / 'state/secrets/personal').exists())
        self.config['secrets']['scopes'] = ['school']
        self.store('BOT', 'v', scope='school')

    def test_other_machines_reach_the_vault_over_ssh_without_values_in_argv(self):
        os.environ['FLEET_MACHINE'] = 'peer'
        request = {'action': 'set', 'scope': 'personal', 'name': 'DISCORD_BOT_TOKEN', 'value': 'token-1', 'note': 'n', 'replace': False,
                   'sender': {'agent': 'a', 'machine': 'peer', 'profile': 'work-codex', 'folder': 'bot'}}
        with patch.object(fleet.subprocess, 'run') as run:
            run.side_effect = subprocess.TimeoutExpired('ssh', 60)
            with self.assertRaisesRegex(ValueError, 'browser check'):
                fleet.secret(self.config, request)
            run.side_effect = None
            run.return_value = subprocess.CompletedProcess([], 0, '', 'fleet: personal DISCORD_BOT_TOKEN already exists.')
            with self.assertRaisesRegex(ValueError, 'already exists'):
                fleet.secret(self.config, request)
            run.return_value = subprocess.CompletedProcess([], 0, '{"saved": "Saved personal DISCORD_BOT_TOKEN."}\n', '')
            self.assertEqual(fleet.secret(self.config, request), {'saved': 'Saved personal DISCORD_BOT_TOKEN.'})
        command = run.call_args.args[0]
        self.assertEqual(command[-4:], ['hub-alias', '~/fleet/scripts/fleet', 'secret', '--relay'])
        self.assertNotIn('token-1', ' '.join(command))
        self.assertEqual(json.loads(run.call_args.kwargs['input']), request)
        os.environ['FLEET_MACHINE'] = 'hub'
        fleet.secret(self.config, request)
        self.assertEqual(fleet.read_json(self.root / 'state/secrets/personal/DISCORD_BOT_TOKEN.json')['updated_by'], 'peer · work-codex · bot')

    def test_cli_set_reads_stdin_and_list_hides_values(self):
        config = self.root / 'fleet.json'
        config.write_text(json.dumps(self.config))
        env = dict(os.environ, FLEET_CONFIG=str(config))
        fleet_cli = [sys.executable, str(ROOT / 'scripts/fleetlib.py'), 'secret']
        run = lambda *args, value=None: subprocess.run(fleet_cli + list(args), input=value, capture_output=True, text=True, env=env)
        self.assertIn('--note', run('set', 'API_KEY', '--scope', 'personal', value='secret-value').stderr)
        self.assertEqual(run('set', 'API_KEY', '--scope', 'personal', '--note', 'Example key', value='secret-value').stdout, 'Saved personal API_KEY.\n')
        listing = run('list').stdout
        self.assertIn('API_KEY', listing)
        self.assertNotIn('secret-value', listing)
        self.assertEqual(run('get', 'API_KEY', '--scope', 'personal').stdout, 'secret-value\n')


@unittest.skipIf(os.name == 'nt', 'the fake CLI is a POSIX script')
class ClaudeDebugTests(FleetCLITestCase):
    def arrange(self, name='work', email='owner@example.com', signed_in=True, reported=None):
        """A fake claude CLI records each call and reports whatever login the test arranged.
        email is the cached account in .claude.json; reported is what `auth status` says, by default the same."""
        self.config['debug'] = {'claude': {'personal': {'home': '~/.claude', 'email': 'owner@example.com'},
                                           'work': {'home': '~/.claude_work', 'email': 'owner@example.com', 'login': 'claudeai', 'sso': True}}}
        script = ("#!/usr/bin/env python3\nimport json, os, sys\n"
                  "with open(os.environ['FAKE_CLAUDE_CALLS'], 'a') as f:\n"
                  "    f.write(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}) + '\\n')\n"
                  "if sys.argv[1:3] == ['auth', 'status']:\n"
                  "    print(json.dumps({'loggedIn': os.environ['FAKE_CLAUDE_SIGNED_IN'] == '1', 'email': os.environ['FAKE_CLAUDE_EMAIL'] or None}))\n"
                  "else:\n"
                  "    print(json.dumps({'result': 'ok', 'total_cost_usd': 0.002, 'fast_mode_state': 'on'}))\n")
        fleet.atomic_write(self.root / 'bin/claude', script, 0o700)
        self.calls = self.root / 'calls.jsonl'
        account = fleet.claude_debug_account_file(self.config, name)
        if email:
            fleet.atomic_write(account, fleet.encode({'oauthAccount': {'emailAddress': email}}))
        return {'FAKE_CLAUDE_CALLS': str(self.calls), 'FAKE_CLAUDE_SIGNED_IN': '1' if signed_in else '0',
                'FAKE_CLAUDE_EMAIL': (email if reported is None else reported) or '',
                'PATH': str(self.root / 'bin') + os.pathsep + os.environ['PATH']}

    def claude_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_ask_goes_direct_with_no_context_and_is_logged(self):
        env = self.arrange()
        env.update({'ANTHROPIC_BASE_URL': 'http://100.64.0.1:8318', 'ANTHROPIC_AUTH_TOKEN': 'gateway-token',
                    'CLAUDE_CONFIG_DIR': str(self.root / '.claude_fleet'), 'CLAUDE_CODE_SESSION_ID': 'agent'})
        with patch.dict(os.environ, env), patch.object(fleet.sys, 'stdout', io.StringIO()) as out:
            code = fleet.claude_debug_ask(self.config, 'work', 'Say ok.', 'Does the account allow fast mode?', 'claude-opus-5-5', 'xhigh', fast=True, max_usd=0.1)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())['fast_mode_state'], 'on')
        status, ask = self.claude_calls()
        self.assertEqual(status['args'], ['auth', 'status', '--json'])
        for call in (status, ask):
            self.assertEqual(call['env']['CLAUDE_CONFIG_DIR'], str(self.root / '.claude_work'))
            self.assertFalse({'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_SESSION_ID'} & set(call['env']))
        args = ask['args']
        self.assertEqual((args[0], args[-1]), ('-p', 'Say ok.'))
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertEqual(args[args.index('--setting-sources') + 1], 'project')
        self.assertEqual(args[args.index('--max-budget-usd') + 1], '0.1')
        self.assertEqual(json.loads(args[args.index('--settings') + 1]), {'fastMode': True})
        self.assertEqual((args[args.index('--model') + 1], args[args.index('--effort') + 1]), ('claude-opus-5-5', 'xhigh'))
        self.assertIn('--strict-mcp-config', args)
        self.assertIn('--no-session-persistence', args)
        self.assertNotEqual(Path(ask['cwd']).resolve(), Path.cwd().resolve())
        self.assertFalse(Path(ask['cwd']).exists())
        log = self.root / 'state/claude-debug/requests.jsonl'
        [record] = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual((record['home'], record['reason'], record['fast'], record['cost_usd']), ('work', 'Does the account allow fast mode?', True, 0.002))
        self.assertEqual(record['sender']['agent'], 'agent')
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_default_home_is_addressed_without_a_config_dir(self):
        env = self.arrange('personal')
        env['CLAUDE_CONFIG_DIR'] = str(self.root / '.claude_fleet')
        with patch.dict(os.environ, env), patch.object(fleet.sys, 'stdout', io.StringIO()):
            self.assertEqual(fleet.claude_debug_ask(self.config, 'personal', 'Say ok.', 'check', 'claude-haiku-5-5'), 0)
        self.assertEqual(fleet.claude_debug_account_file(self.config, 'personal'), self.root / '.claude.json')
        self.assertTrue(all('CLAUDE_CONFIG_DIR' not in call['env'] for call in self.claude_calls()))

    def test_sends_nothing_without_a_reason_or_the_expected_login(self):
        for kwargs, message in [({'email': None}, 'signed out'), ({'signed_in': False}, 'signed out'),
                                ({'email': 'someone-else@example.com'}, 'is someone-else@example.com, not owner@example.com')]:
            env = self.arrange(**kwargs)
            with patch.dict(os.environ, env):
                with self.assertRaisesRegex(ValueError, message):
                    fleet.claude_debug_ask(self.config, 'work', 'Say ok.', 'check', 'claude-haiku-5-5')
                with self.assertRaisesRegex(ValueError, '--reason'):
                    fleet.claude_debug_ask(self.config, 'work', 'Say ok.', ' ', 'claude-haiku-5-5')
            fleet.claude_debug_account_file(self.config, 'work').unlink(missing_ok=True)
        self.assertTrue(all(call['args'][:2] == ['auth', 'status'] for call in self.claude_calls()))
        self.assertFalse((self.root / 'state/claude-debug/requests.jsonl').exists())

    def test_ask_refuses_unless_auth_status_names_the_configured_account(self):
        # auth status names another account, or none.
        for reported, message in [('different@example.com', 'is different@example.com, not owner@example.com'),
                                  ('', 'did not report which account it is')]:
            with self.subTest(reported or 'no email'):
                env = self.arrange(reported=reported)
                with patch.dict(os.environ, env), patch.object(fleet.sys, 'stdout', io.StringIO()):
                    with self.assertRaisesRegex(ValueError, message):
                        fleet.claude_debug_ask(self.config, 'work', 'Say ok.', 'check', 'claude-haiku-5-5')
        self.assertTrue(all(call['args'][:2] == ['auth', 'status'] for call in self.claude_calls()))
        self.assertFalse((self.root / 'state/claude-debug/requests.jsonl').exists())
        env = self.arrange(reported='OWNER@example.com')
        with patch.dict(os.environ, env):
            self.assertIn('signed in as OWNER@example.com', fleet.claude_debug_status(self.config, 'work'))

    def test_login_uses_the_configured_method_and_never_opens_a_browser(self):
        env = self.arrange()
        with patch.dict(os.environ, env):
            fleet.claude_debug_login(self.config, 'work')
        login = self.claude_calls()[0]
        self.assertEqual(login['args'], ['auth', 'login', '--claudeai', '--email', 'owner@example.com', '--sso'])
        self.assertEqual(login['env']['BROWSER'], '/usr/bin/true')

    def test_unconfigured_or_unknown_home_exits_nonzero(self):
        with self.assertRaisesRegex(ValueError, 'not configured'):
            fleet.claude_debug_status(self.config, 'personal')
        config = self.root / 'fleet.json'
        config.write_text(json.dumps(self.config))
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/fleetlib.py'), 'claude-debug', 'status', 'personal'],
                                capture_output=True, text=True, env=dict(os.environ, FLEET_CONFIG=str(config)))
        self.assertEqual(result.returncode, 1)
        self.assertIn('not configured', result.stderr)
        self.arrange()
        with self.assertRaisesRegex(ValueError, 'No debug.claude entry named school'):
            fleet.claude_debug_status(self.config, 'school')


class InitAndCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/fleetlib.py'), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_noninteractive_init_passes_check_and_refuses_to_overwrite(self):
        output = self.root / 'new/fleet.json'
        result = self.cli('init', '--owner', 'Example Owner', '--state-dir', '~/fleet-state', '--identity', 'personal', '--identity', 'work',
                          '--machine', 'studio,os=macos,user=example-user,address=100.64.0.1,ssh=studio,roles=development+macos',
                          '--machine', 'tower,os=windows,user=example-user,address=100.64.0.2,ssh=tower,roles=windows+gpu',
                          '--notify', 'ntfy', '--output', str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(output.read_text())
        self.assertEqual(config['$schema'], fleetconfig.SCHEMA.as_uri())  # outside the checkout
        self.assertEqual(set(config['machines']), {'studio', 'tower'})
        self.assertEqual(config['machines']['tower']['work_root'], 'D:\\AgentWork')
        self.assertEqual(config['sync']['machines'], ['tower'])
        self.assertEqual(config['notify']['channels'][0]['topic'], {'secret': 'personal/FLEET_PAGE_NTFY_TOPIC'})
        self.assertEqual({p['identity'] for p in config['profiles'].values()}, {'personal', 'work'})
        self.assertEqual(self.cli('config', 'check', '--config', str(output)).returncode, 0)
        before = output.read_text()
        refused = self.cli('init', '--output', str(output))
        self.assertEqual(refused.returncode, 1)
        self.assertIn('--force', refused.stderr)
        self.assertEqual(output.read_text(), before)
        self.assertEqual(self.cli('init', '--output', str(output), '--force').returncode, 0)
        self.assertEqual(json.loads(output.read_text())['owner'], 'Your name')

    def test_every_notify_type_and_placeholder_defaults_pass_check(self):
        for i, kind in enumerate([None, 'none', *fleetconfig.CHANNEL_TYPES]):
            with self.subTest(kind):
                path, config = fleetconfig.init(self.root / f'{i}.json', notify=kind, interactive=False)
                self.assertEqual(fleetconfig.check_file(path), [])
                self.assertEqual(config['machines']['devbox1']['address'], '100.64.0.1')

    def test_interactive_init_prompts_only_for_missing_values(self):
        # Owner, six machine fields, identities and the notify type; the state directory was given as a flag.
        answers = iter(['Example Owner', 'studio', '', '', '', '', '', 'personal', 'command'])
        with patch('builtins.input', side_effect=lambda prompt: next(answers)) as prompt:
            _, config = fleetconfig.init(self.root / 'fleet.json', state_dir='~/fleet-state', interactive=True)
        self.assertEqual(prompt.call_count, 9)
        self.assertEqual((config['owner'], config['state_dir'], list(config['machines'])), ('Example Owner', '~/fleet-state', ['studio']))
        self.assertEqual(config['notify']['channels'][0]['type'], 'command')

    def test_check_catches_bad_configs_with_readable_messages(self):
        good = json.loads((ROOT / 'fleet.json').read_text())
        self.assertEqual(fleetconfig.validate(good), [])
        cases = [
            (lambda c: c.pop('owner'), 'missing required key "owner"'),
            (lambda c: c.update(typo=1), 'unknown key "typo"'),
            (lambda c: c['machines']['mainpc'].update(os='beos'), 'machines.mainpc.os: must be one of macos, windows, linux'),
            (lambda c: c['notify']['channels'][0].update(url='https://hooks.example.com/x'), 'never holds a URL, topic or token'),
            (lambda c: c['notify'].update(host='nowhere'), 'notify.host: "nowhere" is not a machine'),
            (lambda c: c['notify']['channels'].append({'type': 'ntfy'}), 'channels[1]: missing required key "topic"'),
            (lambda c: c['notify']['channels'].append({'type': 'pager'}), 'type: must be one of discord-webhook'),
            (lambda c: c['notify']['channels'][0].update(url={'secret': 'school/X'}), 'scope "school" is not one of the vault scopes'),
            (lambda c: c['notify']['channels'][0].update(argv=['x']), 'argv: is not used by a discord-webhook channel'),
            (lambda c: c['notify'].update(max_chars=0), 'notify.max_chars: must be at least 1'),
            (lambda c: c['sync'].update(service_label='com.your-user.skill-sync'), "must not contain a machine's username"),
            (lambda c: c['sync']['machines'].append('devbox1'), 'the sync host updates itself'),
            (lambda c: c['profiles']['fleet-codex'].pop('model'), 'a managed profile needs "model"'),
            (lambda c: c['profiles']['fleet-codex'].update(gateway='missing'), 'gateway "missing" is not configured'),
            (lambda c: c['debug']['claude']['personal'].update(email='nobody'), 'debug.claude.personal.email'),
            (lambda c: c['machines'].update({'Bad Name': c['machines']['mainpc']}), 'key "Bad Name" does not match'),
            (lambda c: c.update(schema_version=2), 'schema_version: must be 1'),
        ]
        for change, message in cases:
            with self.subTest(message):
                config = copy.deepcopy(good)
                change(config)
                problems = fleetconfig.validate(config)
                self.assertTrue(any(message in p for p in problems), problems)
        broken = self.root / 'broken.json'
        broken.write_text('{"owner": ')
        self.assertIn('not valid JSON', fleetconfig.check_file(broken)[0])
        result = self.cli('config', 'check', '--config', str(broken))
        self.assertEqual(result.returncode, 1)
        self.assertIn('1 problem(s)', result.stdout)

    def test_wrong_types_are_reported_never_raised(self):
        good = json.loads((ROOT / 'fleet.json').read_text())
        schema = fleetconfig.load_schema()
        cases = [
            ('profiles', ['wrong-type'], 'fleet.json.profiles: must be an object'),
            ('profiles', {'p': []}, 'fleet.json.profiles.p: must be an object'),
            ('profiles', {'p': {'driver': 'codex', 'identity': 'personal', 'home': '~/.p', 'managed': True, 'gateway': ['x']}}, 'profiles.p.gateway: must be a string'),
            ('machines', 'wrong-type', 'fleet.json.machines: must be an object'),
            ('machines', {'devbox1': 7}, 'fleet.json.machines.devbox1: must be an object'),
            ('additional_gateways', 1, 'fleet.json.additional_gateways: must be an object'),
            ('secrets', None, 'fleet.json.secrets: must be an object'),
            ('secrets', {'host': 'devbox1', 'scopes': [1, 'personal']}, 'secrets.scopes[0]: must be a string'),
            ('notify', True, 'fleet.json.notify: must be an object'),
            ('notify', {'host': 'devbox1', 'channels': 5}, 'notify.channels: must be an array'),
            ('notify', {'host': 'devbox1', 'channels': [1, {'name': ['x'], 'type': 'command', 'argv': ['x']}]}, 'channels[1].name: must be a string'),
            ('sync', {'host': ['devbox1'], 'machines': 'mainpc'}, 'sync.machines: must be an array'),
            ('debug', {'claude': []}, 'fleet.json.debug.claude: must be an object'),
        ]
        for section, value, message in cases:
            with self.subTest(section=section, value=value):
                problems = fleetconfig.validate({**copy.deepcopy(good), section: value}, schema)
                self.assertTrue(any(message in p for p in problems), problems)

        def paths(value, path=()):
            yield path
            children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
            for key, child in children:
                yield from paths(child, path + (key,))
        # Every value anywhere in the starter, replaced by every JSON type, is reported rather than raised.
        for path in list(paths(good))[1:]:
            for wrong in (['wrong-type'], 'wrong-type', 0, 1.5, None, True, {}, {'x': []}):
                config = copy.deepcopy(good)
                parent = config
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = wrong
                try:
                    self.assertTrue(all(isinstance(p, str) for p in fleetconfig.validate(config, schema)))
                except Exception as error:
                    self.fail(f'{path} = {wrong!r} raised {error!r}')
        for text in ('{"profiles": ["wrong-type"]}', '[]', '"fleet"', 'null', '1', '[' * 100000 + ']' * 100000):
            with self.subTest(text[:30]):
                path = self.root / 'wrong.json'
                path.write_text(text)
                result = self.cli('config', 'check', '--config', str(path))
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, '')
                self.assertIn('problem(s)', result.stdout)

    def test_schema_and_validator_agree(self):
        schema = fleetconfig.load_schema()
        self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
        self.assertEqual(set(schema['properties']), {'$schema', 'schema_version', 'owner', 'state_dir', 'gateway', 'additional_gateways', 'machines',
                                                     'profiles', 'protected_services', 'skill_policy', 'notify', 'secrets', 'sync', 'debug'})
        starter = json.loads((ROOT / 'fleet.json').read_text())
        self.assertTrue(set(starter) <= set(schema['properties']))
        self.assertEqual(fleetconfig.validate(starter), [])
        self.assertEqual(fleetconfig.validate(json.loads((ROOT / 'examples/legacy-fleet.json').read_text())), [])
        channel = schema['$defs']['channel']
        self.assertEqual(set(channel['properties']['type']['enum']), set(fleetconfig.CHANNEL_TYPES))
        self.assertEqual(set(channel['properties']) - {'name', 'type'}, set().union(*fleetconfig.CHANNEL_KEYS.values()))
        self.assertEqual(set(schema['$defs']['machine']['properties']['os']['enum']), set(fleetconfig.OSES))
        try:
            import jsonschema
        except ImportError:
            return
        jsonschema.Draft202012Validator(schema).validate(starter)
        bad = copy.deepcopy(starter)
        bad['notify']['channels'][0]['url'] = 'https://hooks.example.com/x'
        self.assertFalse(jsonschema.Draft202012Validator(schema).is_valid(bad))


if __name__ == '__main__':
    unittest.main()
