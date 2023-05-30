# SPDX-License-Identifier: GPL-2.0-only
#
# This file is part of Nominatim. (https://nominatim.org)
#
# Copyright (C) 2022 by the Nominatim developer community.
# For a full list of authors see the git log.
"""
Command-line interface to the Nominatim functions for import, update,
database administration and querying.
"""
from typing import Optional, Any, List, Union
import importlib
import logging
import os
import sys
import argparse
from pathlib import Path

from nominatim.config import Configuration
from nominatim.tools.exec_utils import run_legacy_script, run_php_server
from nominatim.errors import UsageError
from nominatim import clicmd
from nominatim import version
from nominatim.clicmd.args import NominatimArgs, Subcommand

LOG = logging.getLogger()

class CommandlineParser:
    """ Wraps some of the common functions for parsing the command line
        and setting up subcommands.
    """
    def __init__(self, prog: str, description: Optional[str]):
        self.parser = argparse.ArgumentParser(
            prog=prog,
            description=description,
            formatter_class=argparse.RawDescriptionHelpFormatter)

        self.subs = self.parser.add_subparsers(title='available commands',
                                               dest='subcommand')

        # Global arguments that only work if no sub-command given
        self.parser.add_argument('--version', action='store_true',
                                 help='Print Nominatim version and exit')

        # Arguments added to every sub-command
        self.default_args = argparse.ArgumentParser(add_help=False)
        group = self.default_args.add_argument_group('Default arguments')
        group.add_argument('-h', '--help', action='help',
                           help='Show this help message and exit')
        group.add_argument('-q', '--quiet', action='store_const', const=0,
                           dest='verbose', default=1,
                           help='Print only error messages')
        group.add_argument('-v', '--verbose', action='count', default=1,
                           help='Increase verboseness of output')
        group.add_argument('--project-dir', metavar='DIR', default='.',
                           help='Base directory of the Nominatim installation (default:.)')
        group.add_argument('-j', '--threads', metavar='NUM', type=int,
                           help='Number of parallel threads to use')


    def nominatim_version_text(self) -> str:
        """ Program name and version number as string
        """
        text = f'Nominatim version {version.NOMINATIM_VERSION!s}'
        if version.GIT_COMMIT_HASH is not None:
            text += f' ({version.GIT_COMMIT_HASH})'
        return text


    def add_subcommand(self, name: str, cmd: Subcommand) -> None:
        """ Add a subcommand to the parser. The subcommand must be a class
            with a function add_args() that adds the parameters for the
            subcommand and a run() function that executes the command.
        """
        assert cmd.__doc__ is not None

        parser = self.subs.add_parser(name, parents=[self.default_args],
                                      help=cmd.__doc__.split('\n', 1)[0],
                                      description=cmd.__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter,
                                      add_help=False)
        parser.set_defaults(command=cmd)
        cmd.add_args(parser)


    def run(self, **kwargs: Any) -> int:
        """ Parse the command line arguments of the program and execute the
            appropriate subcommand.
        """
        args = NominatimArgs()
        try:
            self.parser.parse_args(args=kwargs.get('cli_args'), namespace=args)
        except SystemExit:
            return 1

        if args.version:
            print(self.nominatim_version_text())
            return 0

        if args.subcommand is None:
            self.parser.print_help()
            return 1

        args.phpcgi_path = Path(kwargs['phpcgi_path'])
        args.project_dir = Path(args.project_dir).resolve()

        if 'cli_args' not in kwargs:
            logging.basicConfig(stream=sys.stderr,
                                format='%(asctime)s: %(message)s',
                                datefmt='%Y-%m-%d %H:%M:%S',
                                level=max(4 - args.verbose, 1) * 10)

        args.config = Configuration(args.project_dir,
                                    environ=kwargs.get('environ', os.environ))
        args.config.set_libdirs(module=kwargs['module_dir'],
                                osm2pgsql=kwargs['osm2pgsql_path'])

        log = logging.getLogger()
        log.warning('Using project directory: %s', str(args.project_dir))

        try:
            return args.command.run(args)
        except UsageError as exception:
            if log.isEnabledFor(logging.DEBUG):
                raise # use Python's exception printing
            log.fatal('FATAL: %s', exception)

        # If we get here, then execution has failed in some way.
        return 1


# Subcommand classes
#
# Each class needs to implement two functions: add_args() adds the CLI parameters
# for the subfunction, run() executes the subcommand.
#
# The class documentation doubles as the help text for the command. The
# first line is also used in the summary when calling the program without
# a subcommand.
#
# No need to document the functions each time.
# pylint: disable=C0111
class QueryExport:
    """\
    Export addresses as CSV file from the database.
    """

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group('Output arguments')
        group.add_argument('--output-type', default='street',
                           choices=('continent', 'country', 'state', 'county',
                                    'city', 'suburb', 'street', 'path'),
                           help='Type of places to output (default: street)')
        group.add_argument('--output-format',
                           default='street;suburb;city;county;state;country',
                           help=("Semicolon-separated list of address types "
                                 "(see --output-type). Multiple ranks can be "
                                 "merged into one column by simply using a "
                                 "comma-separated list."))
        group.add_argument('--output-all-postcodes', action='store_true',
                           help=("List all postcodes for address instead of "
                                 "just the most likely one"))
        group.add_argument('--language',
                           help=("Preferred language for output "
                                 "(use local name, if omitted)"))
        group = parser.add_argument_group('Filter arguments')
        group.add_argument('--restrict-to-country', metavar='COUNTRY_CODE',
                           help='Export only objects within country')
        group.add_argument('--restrict-to-osm-node', metavar='ID', type=int,
                           help='Export only children of this OSM node')
        group.add_argument('--restrict-to-osm-way', metavar='ID', type=int,
                           help='Export only children of this OSM way')
        group.add_argument('--restrict-to-osm-relation', metavar='ID', type=int,
                           help='Export only children of this OSM relation')


    def run(self, args: NominatimArgs) -> int:
        params: List[Union[int, str]] = [
                             '--output-type', args.output_type,
                             '--output-format', args.output_format]
        if args.output_all_postcodes:
            params.append('--output-all-postcodes')
        if args.language:
            params.extend(('--language', args.language))
        if args.restrict_to_country:
            params.extend(('--restrict-to-country', args.restrict_to_country))
        if args.restrict_to_osm_node:
            params.extend(('--restrict-to-osm-node', args.restrict_to_osm_node))
        if args.restrict_to_osm_way:
            params.extend(('--restrict-to-osm-way', args.restrict_to_osm_way))
        if args.restrict_to_osm_relation:
            params.extend(('--restrict-to-osm-relation', args.restrict_to_osm_relation))

        return run_legacy_script('export.php', *params, config=args.config)


class AdminServe:
    """\
    Start a simple web server for serving the API.

    This command starts a built-in webserver to serve the website
    from the current project directory. This webserver is only suitable
    for testing and development. Do not use it in production setups!

    There are different webservers available. The default 'php' engine
    runs the classic PHP frontend. The other engines are Python servers
    which run the new Python frontend code. This is highly experimental
    at the moment and may not include the full API.

    By the default, the webserver can be accessed at: http://127.0.0.1:8088
    """

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group('Server arguments')
        group.add_argument('--server', default='127.0.0.1:8088',
                           help='The address the server will listen to.')
        group.add_argument('--engine', default='php',
                           choices=('php', 'sanic', 'falcon', 'starlette'),
                           help='Webserver framework to run. (default: php)')


    def run(self, args: NominatimArgs) -> int:
        if args.engine == 'php':
            run_php_server(args.server, args.project_dir / 'website')
        else:
            server_info = args.server.split(':', 1)
            host = server_info[0]
            if len(server_info) > 1:
                if not server_info[1].isdigit():
                    raise UsageError('Invalid format for --server parameter. Use <host>:<port>')
                port = int(server_info[1])
            else:
                port = 8088

            if args.engine == 'sanic':
                server_module = importlib.import_module('nominatim.server.sanic.server')

                app = server_module.get_application(args.project_dir)
                app.run(host=host, port=port, debug=True, single_process=True)
            else:
                import uvicorn # pylint: disable=import-outside-toplevel

                if args.engine == 'falcon':
                    server_module = importlib.import_module('nominatim.server.falcon.server')
                elif args.engine == 'starlette':
                    server_module = importlib.import_module('nominatim.server.starlette.server')

                app = server_module.get_application(args.project_dir)
                uvicorn.run(app, host=host, port=port)

        return 0


def get_set_parser() -> CommandlineParser:
    """\
    Initializes the parser and adds various subcommands for
    nominatim cli.
    """
    parser = CommandlineParser('nominatim', nominatim.__doc__)

    parser.add_subcommand('import', clicmd.SetupAll())
    parser.add_subcommand('freeze', clicmd.SetupFreeze())
    parser.add_subcommand('replication', clicmd.UpdateReplication())

    parser.add_subcommand('special-phrases', clicmd.ImportSpecialPhrases())

    parser.add_subcommand('add-data', clicmd.UpdateAddData())
    parser.add_subcommand('index', clicmd.UpdateIndex())
    parser.add_subcommand('refresh', clicmd.UpdateRefresh())

    parser.add_subcommand('admin', clicmd.AdminFuncs())

    parser.add_subcommand('export', QueryExport())
    parser.add_subcommand('serve', AdminServe())

    parser.add_subcommand('search', clicmd.APISearch())
    parser.add_subcommand('reverse', clicmd.APIReverse())
    parser.add_subcommand('lookup', clicmd.APILookup())
    parser.add_subcommand('details', clicmd.APIDetails())
    parser.add_subcommand('status', clicmd.APIStatus())

    return parser


def nominatim(**kwargs: Any) -> int:
    """\
    Command-line tools for importing, updating, administrating and
    querying the Nominatim database.
    """
    return get_set_parser().run(**kwargs)
