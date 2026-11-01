from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_public', ROOT / 'scripts/check-public.py')
public = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public)


def setUpModule():
    # Fixture repos commit with this identity rather than whatever the machine has, or lacks, as on CI runners.
    # Repository-local settings in a test still win over it.
    home = tempfile.TemporaryDirectory()
    config = Path(home.name) / 'gitconfig'
    config.write_text('[user]\n\tname = Example\n\temail = example@users.noreply.github.com\n')
    environment = patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config), 'GIT_CONFIG_NOSYSTEM': '1'})
    environment.start()
    unittest.addModuleCleanup(home.cleanup)
    unittest.addModuleCleanup(environment.stop)


class PublicReleaseTests(unittest.TestCase):
    def test_private_metadata_is_detected_without_echoing_values(self):
        # Fictional fixtures, never real owner metadata.
        private = '/' + 'Users/fixture-person/app\nhttps://' + '.'.join(['node', 'fixture-tailnet', 'ts', 'net']) + '\n' + '.'.join(['100', '100', '1', '2'])
        result = public.findings('example.md', private.encode())
        self.assertEqual({x[0] for x in result}, {'personal-home', 'private-tailnet-name', 'private-tailnet-address'})
        self.assertNotIn('fixture-person', repr(result))

    def test_placeholders_and_example_session_ids_are_allowed(self):
        example = '/Users/your-user/app\nhttps://devbox1.tailnet-name.ts.net\n100.64.0.1'
        self.assertEqual(public.findings('example.md', example.encode()), [])
        self.assertEqual(public.findings('skill/examples/report.json', b'00000000-0000-0000-0000-000000000001'), [])
        self.assertTrue(public.findings('skill/examples/report.json', b'11111111-1111-1111-1111-111111111111'))

    def test_state_files_fail_even_when_empty(self):
        for name in ['auth.json', 'skills/orchestrate-trifecta/trifecta.json', 'private/cookies.json', 'x/.env', 'x/cache.sqlite', 'x/backups/note.md', 'x/canvas-planning/config.json']:
            self.assertIn('private-state-file', {x[0] for x in public.findings(name, b'')})
        self.assertEqual(public.findings('config.example.json', b'{}'), [])

    def test_inventory_allowance_requires_real_source_hashes(self):
        import hashlib
        import json
        files = {'demo.md': b'example'}
        files['bank/files.json'] = json.dumps({'demo.md': hashlib.sha256(files['demo.md']).hexdigest()}).encode()
        self.assertEqual(public.inventory_findings(files), [])
        files['demo.md'] = b'changed'
        self.assertTrue(public.inventory_findings(files))

    def test_history_detects_data_deleted_from_tip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def git(*args):
                return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
            git('init', '-q')
            git('config', 'user.name', 'Example')
            git('config', 'user.email', 'example@example.com')
            (root / 'demo.md').write_text('/' + 'Users/fixture-person/project')
            git('add', '.')
            git('commit', '-qm', 'Fictional private fixture')
            (root / 'demo.md').write_text('/Users/your-user/project')
            git('commit', '-qam', 'Replace with example')
            self.assertEqual(public.findings('demo.md', (root / 'demo.md').read_bytes()), [])
            found, count = public.history_findings(root)
            self.assertEqual(count, 2)
            self.assertEqual({x[1] for x in found}, {'personal-home'})


def fixture_repo(root, email='fixture@users.noreply.github.com'):
    def git(*args, env_email=email):
        env = {**os.environ, 'GIT_AUTHOR_EMAIL': env_email, 'GIT_COMMITTER_EMAIL': env_email}
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, env=env)
    git('init', '-q')
    git('config', 'user.name', 'Example')
    git('config', 'commit.gpgsign', 'false')
    return git


def run_main(argv, root):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = public.main(argv, root=root)
    return code, out.getvalue() + err.getvalue()


class DenylistAndEmailTests(unittest.TestCase):
    TERM = 'zorb' + 'lax'  # fictional term, split so this file never contains it whole

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.denylist = Path(self.temp.name) / 'denylist.txt'
        # Line 1 comment, line 2 blank, line 3 the pattern.
        self.denylist.write_text('# private terms\n\n' + 'zorb[a-z]+\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_denylist_hit_reports_pattern_line_without_the_term(self):
        git = fixture_repo(self.root)
        (self.root / 'notes.md').write_text('clean line\nmet ' + self.TERM.upper() + ' today\n')
        git('add', '.')
        git('commit', '-qm', 'Fixture')
        code, output = run_main(['--denylist', str(self.denylist)], self.root)
        self.assertEqual(code, 1)
        self.assertIn('denylisted-term (deny-list line 3): notes.md:2', output)
        self.assertNotIn(self.TERM, output.lower())
        self.assertNotIn('zorb', output.lower())

    def test_denylist_env_var_is_used(self):
        fixture_repo(self.root)
        (self.root / 'notes.md').write_text(self.TERM)
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        with patch.dict(os.environ, {'PUBLIC_CHECK_DENYLIST': str(self.denylist)}):
            code, output = run_main([], self.root)
        self.assertEqual(code, 1)
        self.assertIn('denylisted-term', output)

    def test_comments_and_blank_lines_are_not_patterns(self):
        patterns = public.load_denylist(self.denylist)
        self.assertEqual([n for n, _ in patterns], [3])
        self.assertEqual(public.findings('doc.md', b'private terms\n\nordinary text', patterns), [])

    def test_invalid_pattern_names_only_its_line(self):
        self.denylist.write_text('# ok\nsecret(unclosed\n')
        with self.assertRaisesRegex(ValueError, 'deny-list line 2') as caught:
            public.load_denylist(self.denylist)
        self.assertNotIn('unclosed', str(caught.exception))

    def test_denylist_skips_binary_files_and_checks_paths(self):
        patterns = public.load_denylist(self.denylist)
        self.assertEqual(public.findings('audio.mp3', b'\0\x01' + self.TERM.encode(), patterns), [])
        self.assertEqual(public.findings(f'docs/{self.TERM}.md', b'', patterns),
                         [('denylisted-term (deny-list line 3)', f'docs/{self.TERM}.md', 0)])

    def test_denylist_applies_to_history_and_commit_messages(self):
        git = fixture_repo(self.root)
        (self.root / 'demo.md').write_text('about ' + self.TERM)
        git('add', '.')
        git('commit', '-qm', 'Fixture one')
        (self.root / 'demo.md').write_text('about nothing')
        git('commit', '-qam', 'Mention ' + self.TERM)
        patterns = public.load_denylist(self.denylist)
        self.assertEqual(public.findings('demo.md', (self.root / 'demo.md').read_bytes(), patterns), [])
        found, _ = public.history_findings(self.root, patterns)
        self.assertEqual([x[1:] for x in found], [('denylisted-term (deny-list line 3)', 'demo.md', 1)])
        commits = public.commit_findings(self.root, denylist=patterns)
        self.assertEqual([x[1:] for x in commits], [('denylisted-term (deny-list line 3)', 'commit-message', 1)])
        code, output = run_main(['--history', '--denylist', str(self.denylist)], self.root)
        self.assertEqual(code, 1)
        self.assertNotIn('zorb', output.lower())

    def test_commit_emails_must_be_noreply_or_allowed(self):
        personal = '@'.join(['fixture.person', 'mail-fixture.io'])
        git = fixture_repo(self.root, personal)
        (self.root / 'a.md').write_text('a')
        git('add', '.')
        git('commit', '-qm', 'Personal identity')
        bad = git('rev-parse', 'HEAD').stdout.decode().strip()
        (self.root / 'a.md').write_text('b')
        git('commit', '-qam', 'Private relay', env_email='123+fixture@users.noreply.github.com')
        found = public.commit_findings(self.root)
        self.assertEqual({(x[0], x[2]) for x in found}, {(bad[:12], 'author-email'), (bad[:12], 'committer-email')})
        code, output = run_main(['--history'], self.root)
        self.assertEqual(code, 1)
        self.assertIn(bad[:12], output)
        self.assertNotIn('fixture.person', output)
        self.assertNotIn('mail-fixture', output)
        self.assertEqual(public.commit_findings(self.root, ['*@mail-fixture.io']), [])

    def test_real_looking_emails_are_flagged_outside_vendor_and_licenses(self):
        personal = ('reach ' + '@'.join(['fixture.person', 'mail-fixture.io'])).encode()
        self.assertEqual(public.findings('docs/contact.md', personal), [('personal-email', 'docs/contact.md', 1)])
        for name in ['vendor/skills/x/evals.json', 'LICENSE', 'bank/licenses/x-LICENSE.txt']:
            self.assertEqual(public.findings(name, personal), [])
        self.assertEqual(public.findings('audio.mp3', b'\0' + personal), [])
        placeholders = ['owner@example.com', 'a@example.org', 'b@example.net', 'student@example.edu',
                        'c@test.example', 'test@example.invalid', '1+u@users.noreply.github.com',
                        'noreply@github.com', 'email@domain.com', 'icon@2x.png']
        self.assertEqual(public.findings('docs/ok.md', '\n'.join(placeholders).encode()), [])

    def test_denied_terms_and_emails_in_paths_are_never_printed(self):
        git = fixture_repo(self.root)
        email = '@'.join(['fixture.person', 'mail-fixture.io'])
        (self.root / 'docs').mkdir()
        (self.root / 'docs' / f'{self.TERM}-notes.md').write_text('/' + 'Users/fixture-person/x\n')
        (self.root / 'people' / email).mkdir(parents=True)
        (self.root / 'people' / email / 'a.md').write_text('a\n')
        git('add', '.')
        git('commit', '-qm', 'Fixture')
        (self.root / 'docs' / f'{self.TERM}-notes.md').unlink()
        git('commit', '-qam', 'Remove the note')  # the history finding remains
        (self.root / f'link-{self.TERM}').symlink_to('people')
        self.assertIn(('personal-email', f'people/{email}/a.md', 0), public.findings(f'people/{email}/a.md', b''))
        for argv in (['--history'], ['--range', 'HEAD']):
            with self.subTest(argv=argv):
                code, output = run_main(argv + ['--denylist', str(self.denylist)], self.root)
                self.assertEqual(code, 1)
                self.assertIn('people/<redacted name ', output)
                self.assertIn('personal-home: docs/<redacted name ', output)  # a deleted file, from history
                for secret in ('zorb', 'fixture.person', 'mail-fixture'):
                    self.assertNotIn(secret, output.lower())
        # Without a deny-list the term is unknown, but an email in a path is still withheld.
        self.assertNotIn('mail-fixture', run_main(['--history'], self.root)[1])
        git('add', '.')  # a tracked link fails the working-tree scan before any finding
        code, output = run_main(['--denylist', str(self.denylist)], self.root)
        self.assertEqual(code, 1)
        self.assertIn('Unreviewed link: <redacted name ', output)
        self.assertNotIn('zorb', output.lower())


class HistoryScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.git = fixture_repo(self.root)
        self.private = ('/' + 'Users/fixture-person/x\n').encode()

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, name, data, message='Fixture', email=None):
        (self.root / name).write_bytes(data)
        self.git('add', '.')
        self.git('commit', '-qm', message, **({'env_email': email} if email else {}))
        return self.git('rev-parse', 'HEAD').stdout.decode().strip()

    def test_history_is_head_not_every_ref(self):
        base = self.commit('a.md', b'clean\n')
        self.git('checkout', '-qb', 'side')
        junk = self.commit('b.md', self.private)
        self.git('checkout', '-q', '-')
        self.git('update-ref', 'refs/t3/checkpoints/fixture/0', junk)
        found, count = public.history_findings(self.root)
        self.assertEqual((found, count), ([], 1))
        self.assertEqual(run_main(['--history'], self.root)[0], 0)
        # Refs named explicitly are scanned.
        found, count = public.history_findings(self.root, revisions=public.history_revisions(self.root, ['side'])[0])
        self.assertEqual(({x[1] for x in found}, count), ({'personal-home'}, 2))
        self.assertEqual(public.history_revisions(self.root, [f'{base}..side']), ([junk], [base]))

    def test_range_checks_only_the_given_commits(self):
        self.commit('kept.md', self.private)
        self.commit('a.md', self.private, email='@'.join(['fixture.person', 'mail-fixture.io']))
        self.commit('a.md', b'clean\n', 'Mention ' + DenylistAndEmailTests.TERM)
        denylist = Path(self.temp.name) / 'deny.txt'
        denylist.write_text('zorb[a-z]+\n')
        (self.root / 'untracked-but-staged.md').write_bytes(self.private)
        self.git('add', '.')  # --range never reads the working tree or the index
        code, output = run_main(['--range', 'HEAD~1..HEAD', '--published', 'HEAD~1'], self.root)
        self.assertEqual(code, 0, output)
        self.assertIn('1 historical revision(s)', output)
        code, output = run_main(['--range', 'HEAD~1..HEAD', '--published', 'HEAD~1', '--denylist', str(denylist)], self.root)
        self.assertEqual(code, 1)
        self.assertIn('commit-message:1', output)
        code, output = run_main(['--range', 'HEAD~1..HEAD', '--range', 'HEAD~2..HEAD~1'], self.root)
        self.assertEqual(code, 1)
        self.assertIn('personal-commit-email', output)
        self.assertIn('personal-home: a.md:1', output)
        # kept.md is unchanged since the base, but only a published ref proves the base is public.
        self.assertIn('kept.md', output)
        code, output = run_main(['--range', 'HEAD~1..HEAD', '--range', 'HEAD~2..HEAD~1', '--published', 'HEAD~2'], self.root)
        self.assertIn('personal-home: a.md:1', output)
        self.assertNotIn('kept.md', output)
        self.assertIn('kept.md', run_main(['--range', 'HEAD', '--published', 'HEAD~2'], self.root)[1])

    def test_a_range_base_is_published_only_when_a_published_ref_reaches_it(self):
        published = self.commit('a.md', b'clean\n')
        unpushed = self.commit('kept.md', self.private, 'Unpushed')
        self.commit('a.md', b'changed\n')  # keeps kept.md
        for argv in ([], ['--published', published]):
            with self.subTest(argv=argv):
                code, output = run_main(['--range', f'{unpushed}..HEAD', *argv], self.root)
                self.assertEqual(code, 1, output)
                self.assertIn('personal-home: kept.md:1', output)
        self.assertEqual(run_main(['--range', f'{unpushed}..HEAD', '--published', unpushed], self.root)[0], 0)

    def test_history_checks_annotated_tags_into_head(self):
        term = DenylistAndEmailTests.TERM
        denylist = Path(self.temp.name) / 'deny.txt'
        denylist.write_text('zorb[a-z]+\n')
        self.commit('a.md', b'clean\n')
        self.git('tag', '-a', 'into-head', '-m', 'Release ' + term)
        self.git('tag', '-a', 'nested', '-m', 'Nested ' + term, 'into-head')
        self.git('tag', 'light')
        self.git('checkout', '-qb', 'side')
        self.commit('b.md', b'side\n')
        self.git('tag', '-a', 'elsewhere', '-m', 'Side ' + term)
        self.git('checkout', '-q', '-')
        ids = {name: self.git('rev-parse', name).stdout.decode().strip() for name in ('into-head', 'nested', 'elsewhere')}
        code, output = run_main(['--history', '--denylist', str(denylist)], self.root)
        self.assertEqual(code, 1, output)
        self.assertIn(f"{ids['into-head'][:12]}: denylisted-term (deny-list line 1): tag-message:1", output)
        self.assertIn(f"{ids['nested'][:12]}: denylisted-term", output)
        self.assertNotIn(ids['elsewhere'][:12], output)
        self.assertNotIn('zorb', output.lower())

    def test_tags_of_trees_and_blobs_are_checked(self):
        self.git('checkout', '-qb', 'scratch')
        self.commit('home.md', self.private)
        self.git('tag', '-a', 'blob-tag', '-m', 'Blob', 'HEAD:home.md')
        self.git('tag', '-a', 'tree-tag', '-m', 'Tree', 'HEAD^{tree}')
        for tag, label in (('blob-tag', 'tagged-blob'), ('tree-tag', 'home.md')):
            with self.subTest(tag):
                code, output = run_main(['--range', tag], self.root)
                self.assertEqual(code, 1, output)
                self.assertIn(f'personal-home: {label}:1', output)

    def test_a_published_link_is_neither_reported_again_nor_inventoried(self):
        import json
        (self.root / 'link').symlink_to('a.md')
        self.commit('a.md', b'a\n')
        (self.root / 'bank').mkdir()
        self.commit('bank/files.json', json.dumps({'a.md': public.sha256(b'a\n')}).encode())
        self.assertEqual(run_main(['--range', 'HEAD~1..HEAD', '--published', 'HEAD~1'], self.root)[0], 0)
        code, output = run_main(['--range', 'HEAD'], self.root)
        self.assertEqual(output.count('unreviewed-link: link:0'), 1)  # once, not per revision

    def test_each_blob_is_read_scanned_and_hashed_once(self):
        import json
        big = b'shared line\n' * 1000
        for number in range(4):
            files = {'big.md': big, 'n.md': str(number).encode()}
            for name, data in files.items():
                (self.root / name).write_bytes(data)
            inventory = {name: public.sha256(data) for name, data in files.items()}
            (self.root / 'bank').mkdir(exist_ok=True)
            self.commit('bank/files.json', json.dumps(inventory).encode())
        blobs = {line.split()[0] for line in self.git('rev-list', '--objects', 'HEAD').stdout.decode().splitlines()
                 if self.git('cat-file', '-t', line.split()[0]).stdout.strip() == b'blob'}
        with patch.object(public, 'sha256', wraps=public.sha256) as digest, \
                patch.object(public, 'content_findings', wraps=public.content_findings) as scan:
            found, count = public.history_findings(self.root)
        self.assertEqual((found, count), ([], 4))
        self.assertEqual(digest.call_count, len(blobs))
        self.assertEqual(scan.call_count, len(blobs))
        self.assertEqual(len(blobs), 1 + 4 + 4)  # big.md once, n.md and the inventory per revision


class PrePushHookTests(unittest.TestCase):
    ZERO = '0' * 40

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / 'repo'
        (self.root / 'scripts').mkdir(parents=True)
        self.git = fixture_repo(self.root)
        self.git('config', 'core.hooksPath', '.githooks')
        shutil.copy2(ROOT / 'scripts/check-public.py', self.root / 'scripts/check-public.py')
        shutil.copy2(ROOT / '.gitleaks.toml', self.root / '.gitleaks.toml')
        shutil.copytree(ROOT / '.githooks', self.root / '.githooks')
        self.dest = base / 'dest.git'  # a local bare destination: the hook lists what it holds
        subprocess.run(['git', 'init', '-q', '--bare', str(self.dest)], check=True)
        self.git('remote', 'add', 'origin', str(self.dest))
        tools = base / 'bin'
        tools.mkdir()
        (tools / 'gitleaks').write_text(RECORDING_GITLEAKS)
        (tools / 'gitleaks').chmod(0o755)
        self.denylist = base / 'deny.txt'
        self.denylist.write_text('zorb[a-z]+\n')
        self.log, self.stdin = base / 'gitleaks.log', base / 'gitleaks.stdin'
        self.env = {**os.environ, 'PATH': f'{tools}{os.pathsep}{os.environ["PATH"]}', 'PUBLIC_CHECK_DENYLIST': str(self.denylist),
                    'GITLEAKS_LOG': str(self.log), 'GITLEAKS_STDIN': str(self.stdin)}
        # Published history that predates the rules and must not block new pushes.
        for name in ('a.md', 'kept.md'):  # kept.md stays unchanged in the pushed commits
            (self.root / name).write_text('/' + 'Users/fixture-person/x\n')
        self.git('add', 'a.md', 'kept.md')
        self.git('commit', '-qm', 'Published before the checks')
        self.base = self.head()
        self.git('push', '-q', '--no-verify', 'origin', 'HEAD:refs/heads/main')
        self.git('update-ref', 'refs/t3/checkpoints/fixture/0', self.base)
        (self.root / 'a.md').write_text('clean\n')
        self.git('commit', '-qam', 'Clean change')

    def tearDown(self):
        self.temp.cleanup()

    def head(self):
        return self.git('rev-parse', 'HEAD').stdout.decode().strip()

    def push(self, *lines, remote='origin', url=None):
        url = str(self.dest) if url is None else url
        stdin = ''.join(' '.join(line) + '\n' for line in lines)
        hook = subprocess.run([str(self.root / '.githooks/pre-push'), remote, url],
                              cwd=self.root, input=stdin, capture_output=True, text=True, env=self.env)
        return hook.returncode, hook.stdout + hook.stderr

    def scanned(self):
        """The commits the Gitleaks stand-in was asked to scan since the last call."""
        commits = set(self.log.read_text().split()) if self.log.exists() else set()
        self.log.unlink(missing_ok=True)
        return commits

    def test_hook_checks_only_the_pushed_commits(self):
        new = self.head()
        cases = {
            'new branch': ('refs/heads/feature', new, 'refs/heads/feature', self.ZERO),
            'update': ('refs/heads/main', new, 'refs/heads/main', self.base),
            'unfetched remote tip': ('refs/heads/main', new, 'refs/heads/main', '1' * 40),
            'delete': ('(delete)', self.ZERO, 'refs/heads/old', self.base),
        }
        for label, line in cases.items():
            with self.subTest(label):
                code, output = self.push(line)
                self.assertEqual(code, 0, output)
        self.assertEqual(self.push()[0], 0)

    def test_hook_blocks_a_pushed_commit_that_fails(self):
        (self.root / 'b.md').write_text('about ' + DenylistAndEmailTests.TERM + '\n')
        self.git('add', 'b.md')
        self.git('commit', '-qm', 'Fixture')
        code, output = self.push(('refs/heads/main', self.head(), 'refs/heads/main', self.base))
        self.assertEqual(code, 1)
        self.assertIn('push blocked', output)
        self.assertIn('denylisted-term (deny-list line 1): b.md:1', output)
        self.assertNotIn('zorb', output.lower())
        code, output = self.push(('refs/heads/feature', self.head(), 'refs/heads/feature', self.ZERO))
        self.assertEqual(code, 1)

    def test_hook_reads_the_denylist_from_repo_git_config(self):
        (self.root / 'b.md').write_text('about ' + DenylistAndEmailTests.TERM + '\n')
        self.git('add', 'b.md')
        self.git('commit', '-qm', 'Fixture')
        line = ('refs/heads/main', self.head(), 'refs/heads/main', self.base)
        del self.env['PUBLIC_CHECK_DENYLIST']
        self.assertEqual(self.push(line)[0], 0)  # no deny-list anywhere: the term is not known
        self.git('config', 'publiccheck.denylist', str(self.denylist))
        code, output = self.push(line)
        self.assertEqual(code, 1)
        self.assertIn('push blocked', output)
        self.assertIn('denylisted-term (deny-list line 1): b.md:1', output)
        self.assertNotIn('zorb', output.lower())

    def private_commit_kept_by(self):
        """(private, new): a commit the destination lacks that adds p.md with a denied term, and a child keeping it."""
        (self.root / 'p.md').write_text('about ' + DenylistAndEmailTests.TERM + '\n')
        self.git('add', 'p.md')
        self.git('commit', '-qm', 'Private only')
        private = self.head()
        (self.root / 'a.md').write_text('later\n')
        self.git('commit', '-qam', 'Keeps p.md')
        return private, self.head()

    def test_only_what_the_destination_lists_counts_as_published(self):
        private, new = self.private_commit_kept_by()
        self.git('checkout', '-q', self.base)
        (self.root / 'other.md').write_text('other\n')
        self.git('add', 'other.md')
        self.git('commit', '-qm', 'Only on the destination')
        other = self.head()
        self.git('checkout', '-q', '-')
        self.git('push', '-q', '--no-verify', '-f', 'origin', f'{other}:refs/heads/main')
        # A destination ref this clone never fetched names an unknown commit: it proves nothing and breaks nothing.
        stranger = subprocess.run(['git', '--git-dir', str(self.dest), 'commit-tree', '-m', 'Unknown here', f'{other}^{{tree}}'],
                                  capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(['git', '--git-dir', str(self.dest), 'update-ref', 'refs/heads/stranger', stranger], check=True)
        # Local tracking refs that claim the private commit is published. None is the destination's word.
        # A nested remote name, written as git remote add would: since git 2.51 that command refuses a name
        # under an existing remote's, but older clones may still have one and git still reads it.
        self.git('config', 'remote.origin/bar.url', '/fictional-private.git')
        self.git('config', 'remote.origin/bar.fetch', '+refs/heads/*:refs/remotes/origin/bar/*')
        self.git('update-ref', 'refs/remotes/origin/bar/main', private)
        self.git('update-ref', 'refs/remotes/origin/stale', private)  # a ref the destination no longer has
        self.git('remote', 'set-url', 'origin', '/fictional-private.git')
        self.git('config', 'remote.origin.pushurl', str(self.dest))  # fetches private history, pushes public
        self.git('update-ref', 'refs/remotes/origin/main', private)
        cases = {
            'new branch': (('refs/heads/feature', new, 'refs/heads/feature', self.ZERO), 'origin'),
            'force push': (('refs/heads/main', new, 'refs/heads/main', other), 'origin'),
            'unfetched remote tip': (('refs/heads/main', new, 'refs/heads/main', '1' * 40), 'origin'),
            'bare URL': (('refs/heads/feature', new, 'refs/heads/feature', self.ZERO), str(self.dest)),
        }
        for label, (line, remote) in cases.items():
            with self.subTest(label):
                code, output = self.push(line, remote=remote)
                self.assertEqual(code, 1, output)
                self.assertIn('denylisted-term (deny-list line 1): p.md:1', output)
                self.assertNotIn('zorb', output.lower())
                scanned = self.scanned()
                self.assertLessEqual({private, new}, scanned)
                self.assertFalse({other, self.base} & scanned)  # the destination has these
        clean = ('refs/heads/feature', other, 'refs/heads/feature', self.ZERO)
        self.assertEqual(self.push(clean)[0], 0)
        # A real push hands the hook the push URL, not the fetch URL.
        push = subprocess.run(['git', 'push', 'origin', 'HEAD:refs/heads/feature'], cwd=self.root, capture_output=True,
                              text=True, env=self.env)
        self.assertNotEqual(push.returncode, 0, push.stdout + push.stderr)
        self.assertIn('p.md:1', push.stderr)
        listed = subprocess.run(['git', 'ls-remote', str(self.dest)], capture_output=True, text=True, check=True).stdout
        self.assertNotIn('refs/heads/feature', listed)

    def test_an_unlisted_destination_counts_nothing_as_published(self):
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        line = ('refs/heads/main', self.head(), 'refs/heads/main', self.base)
        for url in (str(Path(self.temp.name) / 'missing.git'), 'https://fixture-user:tok-fixture@127.0.0.1:9/repo.git'):
            with self.subTest(url=url[:5]):
                code, output = self.push(line, url=url)
                self.assertEqual(code, 1, output)
                self.assertIn('could not list the destination', output)
                self.assertIn('personal-home: kept.md:1', output)  # published on the real destination, but unproven
                self.assertIn(self.base, self.scanned())
                for secret in ('missing.git', 'tok-fixture', 'fixture-user', '127.0.0.1'):
                    self.assertNotIn(secret, output)

    def test_pushed_ref_names_are_checked(self):
        term = DenylistAndEmailTests.TERM
        email = '@'.join(['fixture.person', 'mail-fixture.io'])
        new = self.head()
        cases = {
            'remote ref': (('refs/heads/main', new, f'refs/heads/{term}', self.ZERO), 'denylisted-term (deny-list line 1)'),
            'local ref': ((f'refs/heads/{term}', new, 'refs/heads/main', self.base), 'denylisted-term (deny-list line 1)'),
            'email': (('refs/heads/main', new, f'refs/heads/{email}', self.ZERO), 'personal-email'),
            'published commit': (('refs/heads/main', self.base, f'refs/tags/{term}', self.ZERO), 'denylisted-term'),
        }
        for label, (line, rule) in cases.items():
            with self.subTest(label):
                code, output = self.push(line)
                self.assertEqual(code, 1, output)
                self.assertIn(f'ref-name: {rule}', output)
                self.assertIn('<redacted ref ', output)
                for secret in ('zorb', 'fixture.person', 'mail-fixture'):
                    self.assertNotIn(secret, output.lower())
        # Deleting a denied name publishes nothing.
        self.assertEqual(self.push(('(delete)', self.ZERO, f'refs/heads/{term}', self.base))[0], 0)

    @unittest.skipUnless(shutil.which('gitleaks'), 'Gitleaks is not installed')
    def test_real_gitleaks_blocks_a_push_whose_only_secret_is_a_commit_message(self):
        import secrets
        self.git('push', '-q', '--no-verify', 'origin', 'HEAD:refs/heads/main')
        before = self.head()
        # Synthetic and made fresh for each run. Gitleaks ignores a generic key whose Shannon entropy is under 3.5,
        # as about 2% of random 48-digit hex strings are, or that holds a stopword such as "dead". Fixed digit
        # counts give an entropy of 3.79 every time, and no two letters touch.
        pick = secrets.SystemRandom().sample
        value = ''.join(digit + letter for digit, letter in zip(pick('01234567' * 3, 24), pick('abcdef' * 4, 24)))
        secret = 'api_key = "' + value + '"'
        self.git('commit', '--allow-empty', '-qm', 'Configure\n\n' + secret)
        env = {**self.env, 'PATH': os.environ['PATH']}  # the real Gitleaks, not the stand-in
        push = subprocess.run(['git', 'push', 'origin', 'HEAD:refs/heads/main'], cwd=self.root, capture_output=True,
                              text=True, env=env)
        output = push.stdout + push.stderr
        self.assertNotEqual(push.returncode, 0, output)
        self.assertIn(f'{self.head()[:12]}: gitleaks:generic-api-key: commit-message:3', output)
        self.assertNotIn(secret.split('"')[1], output)
        listed = subprocess.run(['git', 'ls-remote', str(self.dest), 'refs/heads/main'], capture_output=True, text=True,
                                check=True).stdout
        self.assertTrue(listed.startswith(before))

    def test_hook_checks_every_ref_of_one_push(self):
        new = self.head()
        self.git('checkout', '-qb', 'side')
        (self.root / 'side.md').write_text('about ' + DenylistAndEmailTests.TERM + '\n')
        self.git('add', 'side.md')
        self.git('commit', '-qm', 'Side')
        side = self.head()
        delete = ('(delete)', self.ZERO, 'refs/heads/old', self.base)
        clean = ('refs/heads/main', new, 'refs/heads/main', self.base)
        self.assertEqual(self.push(clean, delete, ('refs/heads/copy', new, 'refs/heads/copy', self.ZERO))[0], 0)
        for lines in ((clean, ('refs/heads/side', side, 'refs/heads/side', self.ZERO), delete),
                      (delete, ('refs/heads/side', side, 'refs/heads/side', self.ZERO), clean)):
            with self.subTest(lines=[line[0] for line in lines]):
                code, output = self.push(*lines)
                self.assertEqual(code, 1, output)
                self.assertIn('side.md:1', output)
                self.assertLessEqual({new, side}, self.scanned())

    def test_remote_urls_are_never_printed(self):
        url = 'https://fixture-user:tok-fixture@127.0.0.1:9/repo.git'  # refused at once; the tests stay offline
        self.git('remote', 'set-url', 'origin', url)
        (self.root / 'b.md').write_text('about ' + DenylistAndEmailTests.TERM + '\n')
        self.git('add', 'b.md')
        self.git('commit', '-qm', 'Fixture')
        for remote in ('origin', url):
            with self.subTest(remote=remote[:5]):
                code, output = self.push(('refs/heads/feature', self.head(), 'refs/heads/feature', self.ZERO), remote=remote, url=url)
                self.assertEqual(code, 1)
                self.assertNotIn('tok-fixture', output)
                self.assertNotIn('fixture-user', output)

    def real_push(self, *args, env=None):
        push = subprocess.run(['git', 'push', *args], cwd=self.root, capture_output=True, text=True, env=env or self.env)
        return push.returncode, push.stdout + push.stderr

    def test_a_relative_destination_is_a_path_never_a_remote_name(self):
        private, new = self.private_commit_kept_by()
        hidden = Path(self.temp.name) / 'private.git'  # holds the private commit, under a remote name
        subprocess.run(['git', 'init', '-q', '--bare', str(hidden)], check=True)
        self.git('push', '-q', '--no-verify', str(hidden), f'{private}:refs/heads/main')
        local = self.root / 'destination'  # a local destination of the same name, which git push writes to
        subprocess.run(['git', 'init', '-q', '--bare', str(local)], check=True)
        self.git('remote', 'add', 'destination', str(hidden))
        self.git('remote', 'set-url', 'origin', 'destination')
        code, output = self.real_push('origin', 'HEAD:refs/heads/main')
        self.assertNotEqual(code, 0, output)
        self.assertIn('denylisted-term (deny-list line 1): p.md:1', output)
        self.assertEqual(subprocess.run(['git', '--git-dir', str(local), 'for-each-ref'], capture_output=True, text=True).stdout, '')
        # A relative path that does not exist proves nothing either.
        code, output = self.push(('refs/heads/main', new, 'refs/heads/main', self.ZERO), url='missing-destination')
        self.assertEqual(code, 1, output)
        self.assertIn('the destination path does not exist', output)
        # Nor does a URL that a remote of that very name, or a rewrite, sends somewhere else.
        url = self.dest.as_uri()
        self.git('config', f'remote.{url}.url', str(hidden))
        code, output = self.push(('refs/heads/main', new, 'refs/heads/main', self.ZERO), url=url)
        self.assertEqual(code, 1, output)
        self.assertIn('git would list another repository for that name', output)
        self.assertIn('p.md:1', output)
        self.assertNotIn(url, output)

    def test_replace_refs_cannot_hide_pushed_history(self):
        private, new = self.private_commit_kept_by()
        # A replacement makes the published base seem to descend from the private commit.
        fake = self.git('commit-tree', f'{self.base}^{{tree}}', '-p', private, '-m', 'Replacement').stdout.decode().strip()
        self.git('replace', self.base, fake)
        code, output = self.push(('refs/heads/new', new, 'refs/heads/new', self.ZERO))
        self.assertEqual(code, 1, output)
        self.assertIn('denylisted-term (deny-list line 1): p.md:1', output)
        self.assertIn(private, self.scanned())
        code, output = self.real_push('origin', f'{new}:refs/heads/new')
        self.assertNotEqual(code, 0, output)
        missing = subprocess.run(['git', '--git-dir', str(self.dest), 'cat-file', '-e', private], capture_output=True)
        self.assertNotEqual(missing.returncode, 0)

    def test_a_shallow_or_grafted_clone_counts_nothing_as_published(self):
        line = ('refs/heads/main', self.head(), 'refs/heads/main', self.base)
        self.assertEqual(self.push(line)[0], 0)
        git_dir = self.root / '.git'
        for name, path in (('shallow', git_dir / 'shallow'), ('grafts', git_dir / 'info' / 'grafts')):
            with self.subTest(name):
                path.parent.mkdir(exist_ok=True)
                path.write_text(self.base + '\n')
                try:
                    code, output = self.push(line)
                finally:
                    path.unlink()
                self.assertEqual(code, 1, output)
                self.assertIn('this clone is shallow or has grafts', output)
                self.assertIn('personal-home: kept.md:1', output)  # published, but nothing here can show it

    def test_the_listing_never_prompts_for_credentials(self):
        import http.server
        import threading

        class Challenge(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="fixture"')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Challenge)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        asked = Path(self.temp.name) / 'asked.log'
        askpass = Path(self.temp.name) / 'askpass'
        askpass.write_text(f'#!/bin/sh\necho asked >> "{asked}"\necho fixture-credential\n')
        askpass.chmod(0o755)
        env = {**self.env, 'GIT_ASKPASS': str(askpass), 'SSH_ASKPASS': str(askpass),
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}  # no credential helper either
        url = f'http://127.0.0.1:{server.server_port}/repo.git'
        hook = subprocess.run([str(self.root / '.githooks/pre-push'), 'origin', url], cwd=self.root, capture_output=True,
                              text=True, env=env, input=f'refs/heads/main {self.head()} refs/heads/main {self.base}\n')
        output = hook.stdout + hook.stderr
        self.assertFalse(asked.exists())
        self.assertEqual(hook.returncode, 1, output)
        self.assertIn("could not list the destination's refs (git ls-remote failed", output)
        self.assertNotIn('127.0.0.1', output)

    def fake_ssh(self, body):
        """A stand-in for ssh that logs its arguments and then runs body."""
        ssh, log = Path(self.temp.name) / 'fake-ssh', Path(self.temp.name) / 'ssh.log'
        ssh.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n{body}\n')
        ssh.chmod(0o755)
        return ssh, log

    def test_the_ssh_listing_runs_in_batch_mode_with_the_configured_command(self):
        ssh, log = self.fake_ssh(f'case "$*" in *git-upload-pack*) exec git-upload-pack "{self.dest}" ;; esac\nexit 1')
        self.git('config', 'core.sshCommand', str(ssh))
        self.env['GIT_SSH_VARIANT'] = 'ssh'
        code, output = self.push(('refs/heads/main', self.head(), 'refs/heads/main', self.base), url='fixture-host:repo.git')
        self.assertEqual(code, 0, output)  # the listing worked: the base counts as published
        self.assertIn('-o BatchMode=yes -o ConnectTimeout=10', log.read_text())

    def test_a_stalled_listing_times_out_and_counts_nothing(self):
        import time
        ssh, log = self.fake_ssh('exec sleep 30')
        env = {**self.env, 'GIT_SSH_COMMAND': str(ssh), 'GIT_SSH_VARIANT': 'ssh'}
        started = time.monotonic()
        check = subprocess.run([sys.executable, 'scripts/check-public.py', '--range', 'HEAD', '--destination',
                                'fixture-host:repo.git', '--destination-timeout', '1'], cwd=self.root, capture_output=True,
                               text=True, env=env, timeout=20)
        output = check.stdout + check.stderr
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(check.returncode, 1, output)
        self.assertIn("could not list the destination's refs (no answer within 1 s)", output)
        self.assertIn('personal-home: kept.md:1', output)
        self.assertIn('-o BatchMode=yes', log.read_text())
        self.assertNotIn('fixture-host', output)

    def test_a_listing_of_thousands_of_commits_is_quick(self):
        import time
        count = 2000  # distinct commits, each listed by the destination and held here
        subprocess.run(['git', 'fast-import', '--quiet'], cwd=self.root, check=True, input=''.join(
            f'commit refs/heads/r{n}\ncommitter Example <fixture@users.noreply.github.com> 1000000000 +0000\n'
            f'data {len(str(n)) + 1}\n{n}\n\n' for n in range(count)).encode())
        self.git('push', '-q', '--no-verify', 'origin', 'refs/heads/r*:refs/heads/r*')
        started = time.monotonic()
        code, output = self.push(('refs/heads/main', self.head(), 'refs/heads/main', self.base))
        self.assertEqual(code, 0, output)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.scanned(), {self.head()})

    @unittest.skipUnless(shutil.which('gitleaks'), 'Gitleaks is not installed')
    def test_real_gitleaks_blocks_a_secret_added_only_by_a_merge(self):
        import secrets
        self.git('push', '-q', '--no-verify', 'origin', 'HEAD:refs/heads/main')
        before = self.head()
        self.git('checkout', '-qb', 'side')
        (self.root / 'side.md').write_text('side\n')
        self.git('add', 'side.md')
        self.git('commit', '-qm', 'Side')
        self.git('checkout', '-q', '-')
        self.git('commit', '--allow-empty', '-qm', 'Main')
        self.git('merge', '-q', '--no-commit', '--no-ff', 'side')
        secret = secrets.token_hex(24)  # synthetic, made fresh for each run
        (self.root / 'settings.py').write_text(f'api_key = "{secret}"\n')
        self.git('add', 'settings.py')
        self.git('commit', '-qm', 'Merge side')
        merge = self.head()
        self.git('rm', '-q', 'settings.py')
        self.git('commit', '-qm', 'Remove the setting')
        env = {**self.env, 'PATH': os.environ['PATH']}  # the real Gitleaks, not the stand-in
        code, output = self.real_push('origin', 'HEAD:refs/heads/main', env=env)
        self.assertNotEqual(code, 0, output)
        self.assertIn(f'{merge[:12]}: gitleaks:generic-api-key: settings.py:1', output)
        self.assertNotIn(secret, output)
        listed = subprocess.run(['git', 'ls-remote', str(self.dest), 'refs/heads/main'], capture_output=True, text=True,
                                check=True).stdout
        self.assertTrue(listed.startswith(before))
        # CI's whole-history scan finds it too, without the hook's deny-list.
        history =subprocess.run([sys.executable, 'scripts/check-public.py', '--history', '--secrets'], cwd=self.root,
                                 capture_output=True, text=True, env={**env, 'PUBLIC_CHECK_DENYLIST': ''})
        self.assertEqual(history.returncode, 1, history.stdout + history.stderr)
        self.assertIn(f'{merge[:12]}: gitleaks:generic-api-key: settings.py:1', history.stderr)

    def test_annotated_tags_are_checked_even_on_published_commits(self):
        term = DenylistAndEmailTests.TERM
        personal = '@'.join(['fixture.person', 'mail-fixture.io'])
        self.git('tag', '-a', 'v1', '-m', 'Release ' + term, self.base)
        self.git('tag', '-a', 'v2', '-m', 'Clean', self.base, env_email=personal)
        signed = (f'object {self.base}\ntype commit\ntag v3\ntagger Example <fixture@users.noreply.github.com> 0 +0000\n\n'
                  f'Release\n-----BEGIN PGP SIGNATURE-----\n\n{term}\n-----END PGP SIGNATURE-----\n')
        v3 = subprocess.run(['git', 'mktag'], cwd=self.root, input=signed, capture_output=True, text=True, check=True).stdout.strip()
        self.git('tag', 'light', self.base)
        tag = lambda name: self.git('rev-parse', name).stdout.decode().strip()
        cases = {
            'message': (tag('v1'), 'denylisted-term (deny-list line 1): tag-message:1'),
            'tagger email': (tag('v2'), 'personal-commit-email: tagger-email:0'),
            'signature block': (v3, 'denylisted-term (deny-list line 1): tag-message:4'),
        }
        for label, (oid, finding) in cases.items():
            with self.subTest(label):
                code, output = self.push(('refs/tags/x', oid, 'refs/tags/x', self.ZERO))
                self.assertEqual(code, 1, output)
                self.assertIn(f'{oid[:12]}: {finding}', output)
                for secret in ('zorb', 'fixture.person', 'mail-fixture'):
                    self.assertNotIn(secret, output.lower())
        self.assertIn('Release', self.stdin.read_text())  # Gitleaks saw the tag text
        # A lightweight tag names an already-checked commit and adds nothing.
        self.assertEqual(self.push(('refs/tags/light', self.base, 'refs/tags/light', self.ZERO))[0], 0)


# A Gitleaks stand-in: records the commits a git scan would cover and the text given on stdin.
RECORDING_GITLEAKS = '''#!/bin/sh
case "$1" in
  git) for arg; do case "$arg" in --log-opts=*) git log --format=%H ${arg#--log-opts=} >> "$GITLEAKS_LOG" ;; esac; done ;;
  stdin) cat >> "$GITLEAKS_STDIN" ;;
esac
exit 0
'''

# A Gitleaks stand-in that prints private names, as the real one prints file paths and emails.
NOISY_GITLEAKS = '''#!/bin/sh
report=
while [ $# -gt 0 ]; do [ "$1" = --report-path ] && report=$2; shift; done
echo "scanning $PWD for $FIXTURE_TERM"
echo "File: docs/$FIXTURE_TERM.md Email: $FIXTURE_EMAIL" >&2
case "$FAKE_GITLEAKS_MODE" in
  leak) [ -n "$report" ] && printf '[{"RuleID":"fixture-rule","File":"docs/%s.md","StartLine":3,"Commit":"","Email":"%s"}]' \
          "$FIXTURE_TERM" "$FIXTURE_EMAIL" > "$report"; exit 1 ;;
  fail) exit 2 ;;
esac
[ -n "$report" ] && printf '[]' > "$report"
exit 0
'''


class FailureOutputTests(unittest.TestCase):
    """Failures print no private name. The checker runs as a process, so git's and Gitleaks' own output counts."""
    TERM = DenylistAndEmailTests.TERM
    EMAIL = '@'.join(['fixture.person', 'mail-fixture.io'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / f'{self.TERM}-home'
        self.root = self.base / f'{self.TERM}-repo'
        (self.root / 'scripts').mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/check-public.py', self.root / 'scripts/check-public.py')
        self.git = fixture_repo(self.root)
        (self.root / 'a.md').write_text('clean\n')
        self.git('add', 'a.md')
        self.git('commit', '-qm', 'Fixture')
        tools = self.base / 'bin'
        tools.mkdir()
        (tools / 'gitleaks').write_text(NOISY_GITLEAKS)
        (tools / 'gitleaks').chmod(0o755)
        self.denylist = self.base / 'deny.txt'
        self.denylist.write_text('zorb[a-z]+\n')
        self.env = {**os.environ, 'PATH': f'{tools}{os.pathsep}{os.environ["PATH"]}',
                    'FIXTURE_TERM': self.TERM, 'FIXTURE_EMAIL': self.EMAIL}
        self.env.pop('PUBLIC_CHECK_DENYLIST', None)

    def tearDown(self):
        self.temp.cleanup()

    def check(self, *argv, mode='pass', denylist=True):
        argv = [*argv, '--denylist', str(self.denylist)] if denylist else list(argv)
        result = subprocess.run([sys.executable, str(self.root / 'scripts/check-public.py'), *argv], cwd=self.root,
                                capture_output=True, text=True, env={**self.env, 'FAKE_GITLEAKS_MODE': mode})
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        for secret in (self.TERM, 'mail-fixture'):
            self.assertNotIn(secret, output.lower())
        self.assertNotIn('Traceback', output)
        return output

    def test_failures_never_print_private_names(self):
        self.assertIn('git ', self.check('--range', f'refs/heads/{self.TERM}-missing'))
        self.assertIn('names no commit', self.check('--range', 'HEAD', '--published', f'refs/remotes/{self.TERM}/main'))
        self.assertIn('gitleaks:fixture-rule: docs/<redacted name ', self.check('--secrets', mode='leak'))
        self.assertIn('Gitleaks', self.check('--secrets', '--range', 'HEAD', mode='fail'))
        self.assertIn('deny-list', self.check('--denylist', str(self.base / f'{self.TERM}-deny.txt'), denylist=False))
        unreadable = self.root / f'{self.TERM}-notes.md'
        unreadable.write_text('x\n')
        self.git('add', unreadable.name)
        unreadable.chmod(0)
        try:
            output = self.check()
        finally:
            unreadable.chmod(0o644)
        self.assertIn('Permission denied: <redacted name ', output)
        unreadable.unlink()
        (self.root / f'link-{self.TERM}').symlink_to('a.md')
        self.git('add', '-A')
        self.assertIn('Unreviewed link: <redacted name ', self.check())


# A Gitleaks stand-in that reports each line holding MARK, from stdin or a directory scan; its git scan finds nothing.
DETECTING_GITLEAKS = '''#!PYTHON
import json, os, sys
MARK = 'FIXTURE' + '-SECRET'
args = sys.argv[1:]
leaks = []
def scan(text, name):
    leaks.extend({'RuleID': 'fixture-rule', 'File': name, 'StartLine': number, 'Commit': ''}
                 for number, line in enumerate(text.split('\\n'), 1) if MARK in line)
if args[0] == 'stdin':
    scan(sys.stdin.read(), '')
elif args[0] == 'dir':
    for folder, _, names in os.walk('.'):
        for name in names:
            with open(os.path.join(folder, name), errors='ignore') as file:
                scan(file.read(), os.path.relpath(os.path.join(folder, name)))
with open(args[args.index('--report-path') + 1], 'w') as report:
    json.dump(leaks, report)
sys.exit(1 if leaks else 0)
'''.replace('PYTHON', sys.executable)


class SecretScanTests(unittest.TestCase):
    """Gitleaks sees every text the custom checks see: messages, ref names and files a range inherits."""
    MARK = 'FIXTURE' + '-SECRET'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / 'repo'
        self.root.mkdir()
        self.git = fixture_repo(self.root)
        tools = base / 'bin'
        tools.mkdir()
        (tools / 'gitleaks').write_text(DETECTING_GITLEAKS)
        (tools / 'gitleaks').chmod(0o755)
        self.path = patch.dict(os.environ, {'PATH': f'{tools}{os.pathsep}{os.environ["PATH"]}'})
        self.path.start()

    def tearDown(self):
        self.path.stop()
        self.temp.cleanup()

    def commit(self, name, data, message='Fixture'):
        (self.root / name).write_text(data)
        self.git('add', '.')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD').stdout.decode().strip()

    def test_commit_and_tag_messages_reach_gitleaks_in_one_batch(self):
        base = self.commit('a.md', 'clean\n')
        self.git('commit', '--allow-empty', '-qm', f'Subject\n\nbody {self.MARK}')
        first = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.git('commit', '--allow-empty', '-qm', self.MARK)
        second = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.git('tag', '-a', 'v1', '-m', f'Release\n\n{self.MARK}')
        tag = self.git('rev-parse', 'v1').stdout.decode().strip()
        expected = [f'{first[:12]}: gitleaks:fixture-rule: commit-message:3', f'{second[:12]}: gitleaks:fixture-rule: commit-message:1']
        with patch.object(public, 'run_gitleaks', wraps=public.run_gitleaks) as gitleaks:
            code, output = run_main(['--range', f'v1 --not {base}', '--published', base, '--secrets'], self.root)
        self.assertEqual(code, 1, output)
        for finding in [*expected, f'{tag[:12]}: gitleaks:fixture-rule: tag-object:8']:
            self.assertIn(finding, output)
        self.assertEqual([call.args[0] for call in gitleaks.call_args_list].count(['stdin']), 1)
        code, output = run_main(['--history', '--secrets'], self.root)
        self.assertEqual(code, 1, output)
        for finding in expected:
            self.assertIn(finding, output)

    def test_ref_names_are_checked_without_printing_them(self):
        term = DenylistAndEmailTests.TERM
        denylist = Path(self.temp.name) / 'deny.txt'
        denylist.write_text('zorb[a-z]+\n')
        self.commit('a.md', 'clean\n')
        argv = ['--range', 'HEAD', '--published', 'HEAD', '--denylist', str(denylist)]
        self.assertEqual(run_main([*argv, '--ref-name', 'refs/heads/main'], self.root)[0], 0)
        email = '@'.join(['fixture.person', 'mail-fixture.io'])
        for name, rule in ((f'refs/heads/{term}', 'denylisted-term (deny-list line 1)'), (f'refs/heads/{email}', 'personal-email'),
                           (f'refs/heads/{self.MARK}', 'gitleaks:fixture-rule')):
            with self.subTest(rule=rule):
                code, output = run_main([*argv, '--secrets', '--ref-name', name], self.root)
                self.assertEqual(code, 1, output)
                self.assertIn(f'ref-name: {rule}: <redacted ref {public.sha256(name.encode())[:10]}>', output)
                for secret in ('zorb', 'fixture.person', 'mail-fixture', self.MARK):
                    self.assertNotIn(secret.lower(), output.lower())
        # --history checks local branch and tag names.
        self.assertEqual(run_main(['--history', '--denylist', str(denylist)], self.root)[0], 0)
        for ref in (f'refs/heads/{term}-topic', f'refs/tags/{term}'):
            with self.subTest(ref=ref):
                self.git('update-ref', ref, 'HEAD')
                code, output = run_main(['--history', '--denylist', str(denylist)], self.root)
                self.assertEqual(code, 1, output)
                self.assertIn('ref-name: denylisted-term (deny-list line 1): <redacted ref ', output)
                self.assertNotIn('zorb', output.lower())
                self.git('update-ref', '-d', ref)

    def test_files_kept_from_an_unproven_range_base_reach_gitleaks(self):
        self.commit('a.md', 'clean\n')
        base = self.commit('secret.txt', f'token {self.MARK}\n')
        self.git('mv', 'secret.txt', 'renamed.txt')
        self.git('commit', '-qm', 'Rename')
        tip = self.commit('a.md', 'changed\n')
        code, output = run_main(['--range', f'{base}..HEAD', '--secrets'], self.root)
        self.assertEqual(code, 1, output)
        self.assertIn(f'{tip[:12]}: gitleaks:fixture-rule: renamed.txt:1', output)
        # A published base proves its files public; Gitleaks need not see them again.
        self.assertEqual(run_main(['--range', f'{base}..HEAD', '--published', base, '--secrets'], self.root)[0], 0)

    def merge_with(self, resolution):
        """(base, merge): a merge of two branches that both change conflict.txt, whose result is resolution's files
        (names to text), followed by a commit deleting them. Returns the commit before the branches and the merge."""
        base = self.commit('conflict.txt', 'base\n')
        self.git('checkout', '-qb', 'side')
        self.commit('conflict.txt', 'side\n')
        self.git('checkout', '-q', '-')
        self.commit('conflict.txt', 'main\n')
        subprocess.run(['git', 'merge', '-q', '--no-commit', 'side'], cwd=self.root, capture_output=True)
        for name, text in resolution.items():
            (self.root / name).write_text(text)
        self.git('add', '.')
        self.git('commit', '-qm', 'Merge side')
        merge = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.git('rm', '-q', *[name for name in resolution if name != 'conflict.txt'])
        self.commit('conflict.txt', 'settled\n', 'Clean up')
        return base, merge

    def test_files_only_a_merge_holds_reach_gitleaks(self):
        # Both a conflict resolution and a file the merge adds; the next commit removes both.
        base, merge = self.merge_with({'conflict.txt': f'resolved {self.MARK}\n', 'added.txt': f'token {self.MARK}\n'})
        expected = [f'{merge[:12]}: gitleaks:fixture-rule: conflict.txt:1', f'{merge[:12]}: gitleaks:fixture-rule: added.txt:1']
        for argv in (['--history'], ['--range', f'{base}..HEAD', '--published', base]):
            with self.subTest(argv=argv[0]):
                code, output = run_main([*argv, '--secrets'], self.root)
                self.assertEqual(code, 1, output)
                for finding in expected:
                    self.assertIn(finding, output)
        # A merge that only takes each file from one parent adds nothing to scan.
        self.git('checkout', '-qb', 'other')
        self.commit('other.txt', 'other\n')
        self.git('checkout', '-q', '-')
        self.git('merge', '-q', '--no-ff', '-m', 'Clean merge', 'other')
        self.assertEqual(public.merge_files([self.git('rev-parse', 'HEAD').stdout.decode().strip()], root=self.root), [])

    def test_the_custom_checks_read_files_only_a_merge_holds(self):
        term = DenylistAndEmailTests.TERM
        denylist = Path(self.temp.name) / 'deny.txt'
        denylist.write_text('zorb[a-z]+\n')
        base, merge = self.merge_with({'conflict.txt': f'resolved {term}\n', 'added.txt': f'about {term}\n'})
        for argv in (['--history'], ['--range', f'{base}..HEAD', '--published', base]):
            with self.subTest(argv=argv[0]):
                code, output = run_main([*argv, '--denylist', str(denylist)], self.root)
                self.assertEqual(code, 1, output)
                for name in ('conflict.txt', 'added.txt'):
                    self.assertIn(f'{merge[:12]}: denylisted-term (deny-list line 1): {name}:1', output)
                self.assertNotIn('zorb', output.lower())

    @unittest.skipUnless(shutil.which('gitleaks'), 'Gitleaks is not installed')
    def test_real_gitleaks_reads_files_kept_from_an_unproven_base(self):
        import secrets
        self.path.stop()
        self.path = patch.dict(os.environ, {})  # the real Gitleaks
        self.path.start()
        shutil.copy2(ROOT / '.gitleaks.toml', self.root / '.gitleaks.toml')
        secret = secrets.token_hex(24)
        base = self.commit('settings.py', f'api_key = "{secret}"\n')
        self.commit('a.md', 'later\n')
        code, output = run_main(['--range', f'{base}..HEAD', '--secrets'], self.root)
        self.assertEqual(code, 1, output)
        self.assertIn('gitleaks:generic-api-key: settings.py:1', output)
        self.assertNotIn(secret, output)


class ArgumentErrorTests(unittest.TestCase):
    def test_argument_errors_print_no_values(self):
        term = DenylistAndEmailTests.TERM
        script = str(ROOT / 'scripts/check-public.py')
        for argv in ([f'/tmp/{term}.txt'], [f'--{term}'], ['--range'], ['--denylist']):
            with self.subTest(argv=len(argv[0])):
                result = subprocess.run([sys.executable, script, *argv], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage:', result.stderr)
                self.assertNotIn('zorb', (result.stdout + result.stderr).lower())
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as caught:
            public.main([f'{term}.txt'])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn('zorb', err.getvalue())
