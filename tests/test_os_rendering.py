from unittest.mock import patch

import probes
from rendering import render


def preview(commands):
    with patch.object(probes, '_run', side_effect=lambda args, **kw: commands.get(args[0], (127, ''))):
        return render('lab', 'host', {'OS and security updates': probes._os_updates()})


def test_os_package_lists_and_remediation_are_in_the_email():
    page = preview({'apt-get': (0, 'Inst libexample [1] (2 Ubuntu:24.04/noble-security [amd64])\n'
                                  'Inst linux-image-example (3 Ubuntu:24.04/noble-updates [amd64])'),
                    'systemctl': (0, 'enabled'), 'snap': (0, 'Name Version Rev\nexample-snap 2 3')})
    for text in ('Security:</b> libexample', 'Other:</b> linux-image-example', 'example-snap',
                 'Kernel update waiting', 'Automatic security updates', 'Package list age',
                 'sudo apt update &amp;&amp; sudo apt full-upgrade'):
        assert text in page


def test_missing_commands_do_not_claim_no_updates():
    page = preview({})
    assert 'not available on this system' in page
    assert 'No security updates are waiting' not in page
