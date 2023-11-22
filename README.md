# PyOHIF CLI #

Command line tool for interacting with OHIF via XNAT.

## Installation ##
The recommended usage of this project is not to clone it directly, but
to install it, instead, into your virtual environment. From there you
will have access to the application as a **CLI** tool.
This software requires the use of Python3.10 or greater.

```bash
$ python3 -m pip install git+https://github.com/xnat-ville/Upload_OHIF_ROI.git
```

If you are using **PyOHIF** in a container, ensure the container has
access to git as well as a python interpreter.

### For Windows and Mac Users ###
Additional libraries may be required in order to use some **PyOHIF**
features. On Windows you will want to run the following to install the
DLL files.

```bash
# This will install the DLL files required to
# make calls to `libmagic`, a dependency of PyOHIF.
$ python3 -m pip install python-magic-bin
```

For MacOS installation will require `libmagic` or `file` depending on
your package manager
```zsh
# For homebrew
$ brew install libmagic

# For macports
$ port install file
```

## Usage ##
Once **PyOHIF** has been installed, you should be able to run the
following to access it:

```bash
$ ohif --version
ohif, version 0.1.0
```

Or by using `-h`/`--help` to view your available options.

```bash
$ ohif --help
Usage: ohif [OPTIONS] COMMAND [ARGS]...

  Manage OHIF via XNAT.

Options:
  --version            Show the version and exit.
  -h, --host TEXT
  -u, --username TEXT
  -p, --password TEXT
  -P, --port INTEGER
  -v, --verbose
  --help               Show this message and exit.

Commands:
  roi  OHIF ROI Management
```
