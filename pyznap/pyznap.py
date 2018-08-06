#!/usr/bin/env python
"""
Created on Sat Aug 12 2017

@author: yboetz

ZFS snapshot tool written in python.
"""

import sys
import os
import re
import logging
from logging.config import fileConfig
from argparse import ArgumentParser
from datetime import datetime
from .utils import read_config, create_config
from .clean import clean_config
from .take import take_config
from .send import send_config


DIRNAME = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = '/etc/pyznap/'

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s',
                        datefmt='%b %d %H:%M:%S')
    logger = logging.getLogger(__name__)
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    logger.info('Starting pyznap...')

    parser = ArgumentParser(prog='pyznap', description='ZFS snapshot tool written in python')
    parser.add_argument('--config', action="store",
                        dest="config", help='path to config file')
    parser.add_argument('--version', action="store_true", help='prints version and exits')
    subparsers = parser.add_subparsers(dest='command')

    parser_snap = subparsers.add_parser('snap', help='snapshot tools')
    parser_snap.add_argument('--take', action="store_true",
                             help='take snapshots according to config file')
    parser_snap.add_argument('--clean', action="store_true",
                             help='clean old snapshots according to config file')
    parser_snap.add_argument('--full', action="store_true",
                             help='take snapshots then clean old according to config file')

    parser_send = subparsers.add_parser('send', help='zfs send/receive tools')
    parser_send.add_argument('-s', '--source', action="store",
                             dest='source', help='source filesystem')
    parser_send.add_argument('-d', '--dest', action="store",
                             dest='dest', help='destination filesystem')
    parser_send.add_argument('-i', '--key', action="store",
                             dest='key', help='ssh key for destination')

    parser_setup = subparsers.add_parser('setup', help='initial setup')
    parser_setup.add_argument('-p', '--path', action='store',
                              dest='path', help='pyznap config dir. default is {:s}'.format(CONFIG_DIR))

    args = parser.parse_args(sys.argv[1:])

    if args.command in ('snap', 'send'):
        config_path = args.config if args.config else '/etc/pyznap/pyznap.conf'
        config = read_config(config_path)
        if config == None:
            return 1

    if args.version:
        with open(os.path.join(DIRNAME, '__init__.py'), 'r') as file:
            version = re.search(r'__version__ = \'(.*?)\'', file.read()).group(1)
        logger.info('pyznap version: {:s}'.format(version))

    elif args.command == 'setup':
        path = args.path if args.path else CONFIG_DIR
        create_config(path)

    elif args.command == 'snap':
        # Default if no args are given
        if not args.take and not args.clean:
            args.full = True

        if args.take or args.full:
            take_config(config)

        if args.clean or args.full:
            clean_config(config)

    elif args.command == 'send':
        if args.source and args.dest:
            key = [args.key] if args.key else None
            send_config([{'name': args.source, 'dest': [args.dest], 'dest_keys': key}])
        elif args.source and not args.dest:
            logger.error('Missing dest...')
        elif args.dest and not args.source:
            logger.error('Missing source...')
        else:
            send_config(config)

    logger.info('Finished successfully...\n')
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)