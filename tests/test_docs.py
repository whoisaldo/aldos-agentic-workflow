import posixpath
from pathlib import Path
import re
import subprocess
import unittest
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SKIP_PREFIXES = ('vendor/', 'imports/')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})\s*([\w-]*)')
INLINE_CODE = re.compile(r'(`+).*?\1')
LINK = re.compile(r'!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
REFERENCE = re.compile(r'^ {0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+"[^"]*")?\s*$')
HTML = re.compile(r'<(?:img|a|source)\b[^>]*?\b(?:src|href)\s*=\s*["\']([^"\']+)["\']', re.I)
MERMAID_TYPES = ('flowchart', 'graph', 'sequenceDiagram', 'classDiagram', 'stateDiagram', 'stateDiagram-v2',
                 'erDiagram', 'journey', 'gantt', 'pie', 'quadrantChart', 'requirementDiagram', 'gitGraph',
                 'C4Context', 'C4Container', 'C4Component', 'C4Dynamic', 'C4Deployment', 'mindmap', 'timeline',
                 'sankey-beta', 'xychart-beta', 'block-beta', 'packet-beta', 'architecture-beta', 'kanban')
# Built from parts so this file does not itself look like a home path to the public check.
HOME_PATH = re.compile('/' + r'(?:Users|home)/[^/\s)]+|[A-Za-z]:\\+' + r'Users\\+[^\\\s]+', re.I)


def tracked():
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True)
    if result.returncode:
        return None
    return [name for name in result.stdout.decode().split('\0') if name]


def prose_lines(text):
    """(line number, text, mermaid block id or None) for lines outside code, plus each mermaid block's body."""
    lines, blocks, fence, block = [], [], None, None
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)
            if match.group(2) == 'mermaid':
                block = (number, [])
                blocks.append(block)
            continue
        if fence is not None:
            if line.strip().startswith(fence[0] * len(fence)) and not line.strip().strip(fence[0]):
                fence, block = None, None
            elif block is not None:
                block[1].append(line)
            continue
        lines.append((number, INLINE_CODE.sub('', line)))
    return lines, blocks


def link_targets(line):
    for pattern in (LINK, REFERENCE, HTML):
        for match in pattern.finditer(line):
            yield match.group(1)


class DocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tracked()
        if cls.files is None:
            raise unittest.SkipTest('Not a Git checkout; the docs checks need its tracked file list.')
        cls.dirs = {posixpath.dirname(name) for name in cls.files}
        for name in list(cls.dirs):
            while name:
                name = posixpath.dirname(name)
                cls.dirs.add(name)
        cls.markdown = [name for name in cls.files
                        if name.endswith('.md') and not name.startswith(SKIP_PREFIXES)]

    def test_relative_links_and_images_resolve_to_tracked_paths(self):
        tracked_files, broken = set(self.files), []
        for name in self.markdown:
            lines, _ = prose_lines((ROOT / name).read_text())
            for number, line in lines:
                for target in link_targets(line):
                    if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I) or target.startswith(('#', '/')):
                        continue  # URLs, mailto, in-page anchors and absolute paths are not relative links
                    path = unquote(target.split('#', 1)[0].split('?', 1)[0])
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), path)).rstrip('/')
                    if resolved.startswith('..') or (resolved not in tracked_files and resolved not in self.dirs):
                        broken.append(f'{name}:{number}: {target}')
        self.assertEqual(broken, [])

    def test_mermaid_blocks_name_a_diagram_type(self):
        problems, count = [], 0
        for name in self.markdown:
            _, blocks = prose_lines((ROOT / name).read_text())
            for number, body in blocks:
                count += 1
                content = [line.strip() for line in body if line.strip() and not line.strip().startswith('%%')]
                if not content:
                    problems.append(f'{name}:{number}: empty mermaid block')
                elif content[0].split()[0] not in MERMAID_TYPES:
                    problems.append(f'{name}:{number}: unknown diagram type {content[0].split()[0]!r}')
        self.assertEqual(problems, [])
        self.assertGreater(count, 0)

    def test_readme_has_no_absolute_home_paths(self):
        text = (ROOT / 'README.md').read_text()
        found = [number for number, line in enumerate(text.splitlines(), 1) if HOME_PATH.search(line)]
        self.assertEqual(found, [], 'README lines with a home path; use ~/ instead')

    def test_the_link_checker_catches_what_it_should(self):
        lines, blocks = prose_lines('see [a](docs/x.md#y) and `[b](no.md)`\n```sh\n[c](no.md)\n```\n'
                                    '```mermaid\nflowchart LR\n```\n<img src="i.png" width=200>\n[r]: ref.md\n')
        self.assertEqual([t for _, line in lines for t in link_targets(line)], ['docs/x.md#y', 'i.png', 'ref.md'])
        self.assertEqual(blocks, [(5, ['flowchart LR'])])
        self.assertTrue(HOME_PATH.search('/' + 'Users/someone/x'))
        self.assertFalse(HOME_PATH.search('~/Services/fleet'))


if __name__ == '__main__':
    unittest.main()
