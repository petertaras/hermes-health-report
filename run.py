"""Load the package from its installed directory for no-agent cron jobs."""
import importlib.util
from pathlib import Path
import sys


def load():
    name = '_health_report_plugin'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('__init__.py'))
        if spec is None or spec.loader is None:
            raise ImportError(name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(name + '.health')


if __name__ == '__main__':
    raise SystemExit(load().main())
