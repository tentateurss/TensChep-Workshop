"""Windows console launcher; command arguments retain the normal CLI."""
import os
from pathlib import Path
import shutil
import sys


def prepare():
    if getattr(sys, 'frozen', False):
        root = Path((os.environ.get('LOCALAPPDATA') or Path.home())) / 'TensChep Workshop'
        root.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault('POECRAFT_DATA_DIR', str(root / 'data'))
        os.environ.setdefault('POECRAFT_CONFIG_DIR', str(root / 'config'))
        config = Path(os.environ['POECRAFT_CONFIG_DIR'])
        config.mkdir(parents=True, exist_ok=True)
        if not (config / 'prices.json').exists():
            shutil.copyfile(Path(__file__).parent / 'config' / 'prices.json', config / 'prices.json')


def main():
    prepare()
    if sys.argv[1:3] == ['-m', 'poecraft']:
        del sys.argv[1:3]
    if len(sys.argv) > 1:
        from poecraft.__main__ import main as cli
        return cli()
    from poecraft import config
    print('TensChep Workshop v1.0.0 Beta 1')
    print('PoE 3.30: WAITING FOR GAME DATA. Data:', config.DATA_DIR)
    print('Close this console or press Ctrl+C to stop the server.')
    if not config.DB_PATH.exists():
        print('First start: downloading game data. Internet is required; this may take several minutes.')
        from poecraft.data.update import update
        update(config.REPOE_BASE_URL)
    from poecraft.server import serve
    serve(host='127.0.0.1', port=8765)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('Startup failed:', error, file=sys.stderr)
        if len(sys.argv) == 1:
            input('Press Enter to close. Fix the connection or data directory and retry. ')
        raise SystemExit(1)
