import subprocess
import tempfile
import unittest
from pathlib import Path

from directory_routes import discover, directory_id, extensions, select_directory
from voip import SetupError


class DirectoryRoutesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def path(self, relative):
        p = self.root / relative
        p.mkdir(parents=True, exist_ok=True)
        return p

    def map(self, *paths, **kwargs):
        result = extensions(self.root, [self.root / p for p in paths], **kwargs)
        return {code: str(Path(path).relative_to(self.root)) for code, path in result['extensions'].items()}

    def test_compresses_unforked_paths_and_numbers_forks(self):
        self.assertEqual(self.map('work/apps/alpha', 'work/apps/beta', 'notes/journal'),
                         {'1': 'notes/journal', '21': 'work/apps/alpha', '22': 'work/apps/beta'})

    def test_home_is_default_even_without_tmux_and_single_branch_is_dialable(self):
        result = extensions(self.root, [])
        self.assertEqual(result['default'], str(self.root))
        self.assertEqual(result['extensions'], {})
        self.assertEqual(self.map('a/b/c'), {'1': 'a/b/c'})

    def test_active_parent_and_child_are_separate_targets(self):
        self.assertEqual(self.map('work', 'work/apps/alpha'), {'1': 'work', '11': 'work/apps/alpha'})

    def test_changing_tree_renumbers_extensions_but_not_session_identity(self):
        first = extensions(self.root, [self.root/'work/alpha'])
        second = extensions(self.root, [self.root/'notes', self.root/'work/alpha'])
        self.assertEqual(first['sessions'][str(self.root/'work/alpha')], second['sessions'][str(self.root/'work/alpha')])
        self.assertEqual(first['extensions']['1'], str(self.root/'work/alpha'))
        self.assertEqual(second['extensions']['2'], str(self.root/'work/alpha'))
        self.assertNotEqual(directory_id(self.root/'work/alpha'), directory_id(self.root/'work/beta'))

    def test_wide_forks_use_unambiguous_equal_width_labels(self):
        paths = [f'p{i:02}/one' for i in range(10)] + ['p00/two']
        mapping = self.map(*paths)
        self.assertEqual(mapping['101'], 'p00/one')
        self.assertEqual(mapping['102'], 'p00/two')
        self.assertEqual(mapping['11'], 'p01/one')
        self.assertEqual(len(set(mapping.values())), len(paths))

    def test_manual_extensions_are_reserved_and_duplicates_coalesce(self):
        self.assertEqual(self.map('a', 'b', 'a', reserved=['1']), {'2': 'a', '3': 'b'})
        self.assertEqual(self.map('a/x', 'a/y', 'b', reserved=['11']), {'12': 'a/x', '13': 'a/y', '2': 'b'})

    def test_overflow_is_an_error_instead_of_truncating_or_dropping_directories(self):
        with self.assertRaisesRegex(SetupError, 'cannot fit'):
            self.map('a', 'a/b', max_digits=1)
        with self.assertRaisesRegex(SetupError, 'cannot fit'):
            self.map(*['/'.join(['a'] * depth) for depth in range(1, 8)])

    def test_rejects_outside_and_unnormalized_snapshot_paths(self):
        for p in [Path('/outside'), self.root/'x/../y', Path('relative')]:
            with self.assertRaises(SetupError): extensions(self.root, [p])

    def runner(self, rows, returncode=0, error=''):
        def run(command, **kwargs):
            self.assertNotIn('send-keys', command)
            self.assertIn('list-panes', command)
            return subprocess.CompletedProcess(command, returncode, '\n'.join(rows), error)
        return run

    def row(self, session, pane, base, current, name='worker', dead='0'):
        return '\t'.join((session, name, pane, dead, str(base), str(current)))

    def test_session_base_and_pane_directory_are_deliberately_different_modes(self):
        project = self.path('project')
        runner = self.runner([self.row('$1', '%1', self.root, project)])
        self.assertEqual(discover(self.root, mode='sessions', runner=runner)[0]['directory'], str(self.root))
        self.assertEqual(discover(self.root, mode='panes', runner=runner)[0]['directory'], str(project))

    def test_excludes_dead_panes_and_operator_sessions_deduplicates_linked_panes(self):
        project = self.path('project')
        rows = [self.row('$1', '%1', project, project), self.row('$2', '%1', project, project, name='linked'),
                self.row('$3', '%2', self.root, self.root, name='voice-home'),
                self.row('$4', '%3', self.root, self.root, dead='1')]
        result = discover(self.root, exclude=['voice-home'], runner=self.runner(rows))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['panes'], ['%1'])
        self.assertEqual(set(result[0]['sessions']), {'$1', '$2'})

    def test_symlink_aliases_coalesce_and_escape_is_excluded(self):
        project = self.path('project')
        alias = self.root/'alias'; alias.symlink_to(project, target_is_directory=True)
        outside = self.root/'outside'; outside.symlink_to('/tmp', target_is_directory=True)
        rows = [self.row('$1','%1',project,project), self.row('$2','%2',alias,alias), self.row('$3','%3',outside,outside)]
        result = discover(self.root, runner=self.runner(rows))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['directory'], str(project))

    def test_discovery_failure_is_not_an_empty_tree(self):
        with self.assertRaises(SetupError):
            discover(self.root, runner=self.runner([], 1, 'Permission denied'))
        self.assertEqual(discover(self.root, runner=self.runner([], 1, 'no server running')), [])
        with self.assertRaises(SetupError): discover(self.root, runner=self.runner(['malformed']))

    def test_smart_selection_respects_non_home_base_over_better_name_match(self):
        base, other = self.path('deliberate'), self.path('worker')
        self.assertEqual(select_directory(self.root, 'worker', base, [other])[0], base)

    def test_smart_selection_matches_normalized_and_suffixed_names(self):
        for name, directory in [('task_board', 'task-board'), ('render', 'renderer-demo'), ('voip', 'voip')]:
            candidate = self.path(directory)
            self.assertEqual(select_directory(self.root, name, self.root, [candidate, self.path('unrelated')])[0], candidate)

    def test_smart_selection_falls_back_to_shared_project_ignoring_home_panes(self):
        project = self.path('demo_workspace')
        paths = [project, project, self.root]
        selected, reason = select_directory(self.root, 'starlight', self.root, paths)
        self.assertEqual(selected, project)
        self.assertIn('common pane ancestor', reason)

    def test_smart_selection_uses_deepest_common_parent_of_different_pane_directories(self):
        project = self.path('demo_workspace')
        paths = [self.path('demo_workspace/web/src'), self.path('demo_workspace/api')]
        self.assertEqual(select_directory(self.root, 'starlight', self.root, paths)[0], project)

    def test_smart_selection_uses_common_parent_after_ambiguous_name_match(self):
        project = self.path('work')
        paths = [self.path('work/one/app'), self.path('work/two/app')]
        selected, reason = select_directory(self.root, 'app', self.root, paths)
        self.assertEqual(selected, project)
        self.assertIn('ambiguous name match', reason)

    def test_smart_selection_keeps_home_when_panes_have_no_shared_project(self):
        self.assertEqual(select_directory(self.root, 'app', self.root, [self.path('one/app'), self.path('two/app')])[0], self.root)
        for paths in [[], [self.root], [self.root.parent]]:
            self.assertEqual(select_directory(self.root, 'starlight', self.root, paths)[0], self.root)

    def test_smart_discovery_applies_common_parent_fallback_to_live_panes_only(self):
        project = self.path('demo_workspace')
        rows = [self.row('$1','%1',self.root,project,name='starlight'),
                self.row('$1','%2',self.root,self.root,name='starlight'),
                self.row('$1','%3',self.root,self.path('unrelated'),name='starlight',dead='1')]
        result = discover(self.root, runner=self.runner(rows))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['directory'], str(project))
        self.assertIn('common pane ancestor', result[0]['selection']['$1'])

    def test_smart_selection_can_find_project_above_a_panes_current_directory(self):
        source = self.path('project/src')
        self.assertEqual(select_directory(self.root, 'project', self.root, [source])[0], source.parent)

    def test_smart_discovery_groups_each_session_under_one_directory(self):
        match, other = self.path('worker'), self.path('other')
        rows = [self.row('$1','%1',self.root,match), self.row('$1','%2',self.root,other)]
        result = discover(self.root, runner=self.runner(rows))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['directory'], str(match))
        self.assertIn('inferred', result[0]['selection']['$1'])
