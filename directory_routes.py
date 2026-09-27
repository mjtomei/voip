"""Read-only tmux discovery and deterministic, compressed directory extensions."""
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import hashlib
import os
from pathlib import Path
import re
import subprocess

from voip import SetupError


def directory_id(path):
    # Identity belongs to the directory, never to its changing telephone number.
    return 'dir_' + hashlib.sha256(str(path).encode()).hexdigest()[:24]


def select_directory(root, name, base, candidates):
    """Prefer an intentional base; infer from pane paths only for home-based sessions."""
    root, base = Path(root), Path(base)
    if base != root:
        return base, 'explicit session base'
    candidates = {Path(p) for p in candidates if Path(p).is_relative_to(root) and Path(p) != root}

    def common_directory(reason):
        # A spare shell at home supplies no project context. Keep the deepest
        # ancestor shared by the remaining panes, or home if there are none.
        if not candidates:
            return root, 'home: no project pane directories; ' + reason
        return Path(os.path.commonpath(candidates)), 'common pane ancestor: ' + reason

    words = lambda text: re.findall(r'[a-z0-9]+', text.lower())
    session_words = [w for w in words(name) if w != 'pm' and not re.fullmatch(r'[0-9a-f]{7,}', w)] or words(name)
    normalized = ''.join(session_words)
    ranked = []
    # A shell can be in project/src while the session names the project itself.
    choices = set()
    for candidate in candidates:
        path = Path(candidate)
        while path != root and path.is_relative_to(root):
            choices.add(path)
            path = path.parent
    for path in sorted(choices):
        parts = words(path.name)
        candidate = ''.join(parts)
        score = SequenceMatcher(None, normalized, candidate).ratio()
        # Names like render/renderer-demo should survive a descriptive suffix.
        for a in session_words:
            for b in parts:
                if min(len(a), len(b)) >= 4:
                    score = max(score, .95 * SequenceMatcher(None, a, b).ratio())
        ranked.append((score, path))
    ranked.sort(key=lambda item: (-item[0], str(item[1])))
    if not ranked or ranked[0][0] < .60:
        return common_directory('no confident name match')
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < .10:
        return common_directory('ambiguous name match')
    return ranked[0][1], 'inferred from session name (score %.2f)' % ranked[0][0]


def discover(root, *, socket='', mode='smart', exclude=(), runner=subprocess.run):
    """Detached sessions count; dead panes and our own voice sessions do not."""
    if mode not in {'smart', 'sessions', 'panes'}:
        raise SetupError('Directory discovery must use smart, sessions or panes')
    root = Path(root).expanduser().resolve()
    command = ['tmux'] + (['-S', socket] if socket else [])
    command += ['list-panes', '-a', '-F',
                '#{session_id}\t#{session_name}\t#{pane_id}\t#{pane_dead}\t#{session_path}\t#{pane_current_path}']
    result = runner(command, capture_output=True, text=True, timeout=10)
    if result.returncode:
        if any(s in result.stderr.lower() for s in ('no server running', 'no such file or directory')):
            return []
        raise SetupError('Cannot inspect tmux; existing directory routes must not be replaced with an empty result')
    records = {}
    for line in result.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) != 6:
            raise SetupError('Ambiguous tmux output; tabs/newlines in paths are not supported')
        session, name, pane, dead, base, current = fields
        if not re.fullmatch(r'\$[0-9]+', session) or not re.fullmatch(r'%[0-9]+', pane) or dead not in {'0', '1'}:
            raise SetupError('Invalid tmux discovery record')
        if dead == '1' or name in exclude:
            continue
        paths = [Path(base), Path(current)]
        if any(not p.is_absolute() for p in paths):
            raise SetupError('tmux directories must be absolute')
        base, current = (p.resolve() for p in paths)
        entry = records.setdefault(session, {'name': name, 'base': base, 'panes': {}})
        if entry['name'] != name or entry['base'] != base:
            raise SetupError('tmux session changed during discovery; retry the snapshot')
        entry['panes'][pane] = current
    directories = {}
    for session, record in records.items():
        candidates = {p for p in record['panes'].values() if p.is_relative_to(root) and p.is_dir()}
        if mode == 'smart':
            selected, reason = select_directory(root, record['name'], record['base'], candidates)
            selected = {selected}
        elif mode == 'sessions':
            selected, reason = {record['base']}, 'session base'
        else:
            selected, reason = candidates, 'live pane directory'
        for path in selected:
            if not path.is_relative_to(root) or not path.is_dir():
                continue
            entry = directories.setdefault(str(path), {'directory': str(path), 'sessions': {}, 'panes': set(), 'selection': {}})
            entry['sessions'][session] = record['name']
            entry['selection'][session] = reason
            entry['panes'].update(pane for pane, p in record['panes'].items() if mode != 'panes' or p == path)
    return [{**entry, 'panes': sorted(entry['panes'])}
            for _, entry in sorted(directories.items())]


@dataclass
class Node:
    path: Path
    target: bool = False
    children: dict = field(default_factory=dict)


class TooLong(Exception):
    pass


def extensions(root, directories, *, reserved=(), max_digits=5):
    """Assign equal-width sibling labels; collapse non-target unary directories.

    The home/root target uses no extension. A target with descendants is a choice
    between stopping here and continuing, so it retains a child numbering step.
    At a fork with more than nine children labels use 10, 11, ...; fixed widths
    within each fork prevent collisions between e.g. branch 11 and branches 1/1.
    Explicit extensions reserve exact numbers; generated sibling labels skip
    them. Rebuilding from the same paths and reservations is deterministic.
    """
    root = Path(root).expanduser().resolve()
    if type(max_digits) is not int or not 1 <= max_digits <= 5:
        raise SetupError('Generated extensions must fit the existing 1–5 digit dialing format')
    reserved = set(reserved)
    if any(not isinstance(x, str) or not re.fullmatch(r'[1-9][0-9]{0,4}', x) for x in reserved):
        raise SetupError('Invalid reserved manual extension')
    tree = Node(root, target=True)
    unique = set()
    for directory in directories:
        path = Path(directory)
        if not path.is_absolute() or '..' in path.parts:
            raise SetupError('Directory snapshot paths must be absolute and normalized')
        if not path.is_relative_to(root):
            raise SetupError('Directory snapshot contains a path outside its configured root')
        unique.add(path)
        node = tree
        for part in path.relative_to(root).parts:
            node = node.children.setdefault(part, Node(node.path / part))
        node.target = True
    if len(unique) > 512:
        raise SetupError('Directory discovery exceeds the 512-directory limit')

    minimum = {}
    def needed(node):
        if not node.children:
            value = 0
        else:
            count = len(node.children)
            width = next(w for w in range(1, 5) if 9 * 10**(w-1) >= count)
            value = max(needed(child) for child in node.children.values())
            if count > 1 or node.target: value += width
        minimum[node.path] = value
        return value
    needed(tree)

    def assign(node, prefix, is_root=False):
        if len(prefix) + minimum[node.path] > max_digits:
            raise TooLong()
        own = {prefix: str(node.path)} if node.target else {}
        if node.target and not is_root and (not prefix or prefix in reserved):
            raise TooLong()
        if not node.children:
            return own
        children = [child for _, child in sorted(node.children.items())]
        if len(children) == 1 and not node.target:
            return assign(children[0], prefix)
        available = max_digits - len(prefix)
        for width in range(1, available + 1):
            first, last = 10 ** (width - 1), 10 ** width
            if last - first < len(children):
                continue
            routes, label = dict(own), first
            for child in children:
                while label < last:
                    candidate = prefix + str(label)
                    label += 1
                    try:
                        branch = assign(child, candidate)
                    except TooLong:
                        continue
                    routes.update(branch)
                    break
                else:
                    break
            else:
                return routes
        raise TooLong()

    try:
        routes = assign(tree, '', is_root=True)
    except TooLong:
        raise SetupError('Active directory tree cannot fit unique extensions within five digits; reduce the discovery root or reserve fewer manual extensions') from None
    return {'default': str(root), 'extensions': {code: path for code, path in routes.items() if code},
            'sessions': {str(path): directory_id(path) for path in sorted(unique | {root})}}
