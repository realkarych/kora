import sys

from kora.cli.plugin_child import main

if __name__ == "__main__":
    assert sys.flags.isolated == 1
    main(sys.argv[1], sys.argv[2])
