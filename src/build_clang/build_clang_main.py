#!/usr/bin/env python3

import argparse
import subprocess
import logging
import os

from typing import Any

from build_clang import remote_build


class ClangBuilder:
    args: Any

    def __init__(self) -> None:
        self.remote = False

    def parse_args(self) -> None:
        parser = argparse.ArgumentParser(description='Build Clang')
        parser.add_argument('--remote', action='store_true',
                            help='Run the build on a remote server. Useful during development.')
        parser.add_argument(
            '--remote_server', help='Server to build on',
            default=os.getenv('BUILD_CLANG_REMOTE_SERVER'))
        parser.add_argument(
            '--remote_build_scripts_path',
            help='Remote directory for the build-clang project repo',
            default=os.getenv('BUILD_CLANG_REMOTE_BUILD_SCRIPTS_PATH'))

        self.args = parser.parse_args()

    def run(self) -> None:
        if self.args.remote:
            remote_build.build_remotely(
                remote_server=self.args.remote_server,
                remote_build_scripts_path=self.args.remote_build_scripts_path
            )
            return


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(filename)s:%(lineno)d] %(asctime)s %(levelname)s: %(message)s")

    builder = ClangBuilder()
    builder.parse_args()
    builder.run()


if __name__ == '__main__':
    main()
