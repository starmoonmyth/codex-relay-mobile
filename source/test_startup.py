import pathlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from server import mobile_state
from startup import installed_root, windows_task


class StartupTests(unittest.TestCase):
    def test_windows_recovery_is_per_user_unbounded_and_pins_data_directory(self):
        root = r'C:\Users\Name & Name\Relay Data'
        executable = r'C:\Apps\Relay & Tools\CodexRelayMobile.exe'
        xml = ET.fromstring(windows_task(executable, root, 'S-1-5-21-123-1001'))
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        def value(path):
            return xml.find(path, ns).text
        self.assertEqual(value('t:Triggers/t:TimeTrigger/t:Repetition/t:Interval'), 'PT1M')
        self.assertEqual(value('t:Principals/t:Principal/t:LogonType'), 'InteractiveToken')
        self.assertEqual(value('t:Principals/t:Principal/t:RunLevel'), 'LeastPrivilege')
        self.assertEqual(value('t:Settings/t:MultipleInstancesPolicy'), 'IgnoreNew')
        self.assertEqual(value('t:Settings/t:ExecutionTimeLimit'), 'PT0S')
        self.assertEqual(value('t:Settings/t:DisallowStartIfOnBatteries'), 'false')
        self.assertEqual(value('t:Actions/t:Exec/t:Command'), executable)
        self.assertIn('--data-dir "' + root + '"', value('t:Actions/t:Exec/t:Arguments'))

    def test_background_data_directory_is_independent_of_launch_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('sys.argv', ['relay', '--background', '--data-dir', directory]):
                self.assertEqual(mobile_state(), pathlib.Path(directory).resolve())
            root = pathlib.Path(directory)
            (root / 'config.json').write_text('{}', 'utf-8')
            self.assertTrue((installed_root(root) / 'config.json').is_file())


if __name__ == '__main__':
    unittest.main()
