#!/usr/bin/env python3

import argparse
import subprocess
import logging
import os

from typing import Any, Optional, Dict, List

from build_clang import remote_build
from build_clang.git_helpers import git_clone_tag
from build_clang.helpers import (
    mkdir_p,
    rm_rf,
    ChangeDir,
    run_cmd,
    multiline_str_to_list,
    log_info_heading,
    EnvVarContext,
    which,
    get_current_timestamp_str,
)
from build_clang.file_downloader import FileDownloader
from build_clang.cmake_installer import get_cmake_path
from build_clang.compiler_wrapper import get_cmake_args_for_compiler_wrapper


LLVM_REPO_URL = 'https://github.com/llvm/llvm-project.git'
NUM_STAGES = 3


DEVTOOLSET_ENV_VARS = set(multiline_str_to_list("""
    INFOPATH
    LD_LIBRARY_PATH
    MANPATH
    PATH
    PCP_DIR
    PERL5LIB
    PKG_CONFIG_PATH
    PYTHONPATH
"""))


def cmake_vars_to_args(vars: Dict[str, str]) -> List[str]:
    return ['-D%s=%s' % (k, v) for (k, v) in vars.items()]


def activate_devtoolset() -> None:
    devtoolset_env_str = subprocess.check_output(
        ['bash', '-c', '. /opt/rh/devtoolset-8/enable && env']).decode('utf-8')

    for line in devtoolset_env_str.split("\n"):
        line = line.strip()
        if not line:
            continue
        k, v = line.split("=", 1)
        if k in DEVTOOLSET_ENV_VARS:
            logging.info("Setting %s to: %s", k, v)
            os.environ[k] = v


class ClangBuildConf:
    version: str
    llvm_parent_dir_for_specific_version: str
    llvm_project_clone_dir: str
    cmake_executable_path: str

    # Whether to delete CMake build directory before the build.
    clean_build: bool

    # A timestamp string for when this confugration was created.
    build_start_timestamp_str: str

    def __init__(
            self,
            version: str,
            top_dir_suffix: str,
            clean_build: bool) -> None:
        self.version = version

        if top_dir_suffix:
            effective_top_dir_suffix = '-%s' % top_dir_suffix
        else:
            effective_top_dir_suffix = ''

        self.llvm_parent_dir_for_specific_version = os.path.join(
            '/opt/yb-build/llvm',
            'llvm-v%s%s' % (version, effective_top_dir_suffix))
        self.llvm_project_clone_dir = os.path.join(
            self.llvm_parent_dir_for_specific_version, 'src', 'llvm-project')
        self.cmake_executable_path = get_cmake_path()
        self.clean_build = clean_build
        self.build_start_timestamp_str = get_current_timestamp_str()


class ClangBuildStage:
    # Build configuration. The same for all stages.
    build_conf: ClangBuildConf

    stage_number: int

    # Previous stage, e.g. stage 1 if this is stage 2, or None for stage 1.
    prev_stage: Optional['ClangBuildStage']

    # Base directory for this stage's directories.
    stage_base_dir: str

    # Directory that CMake will generate Ninja files in and where the build will run for this stage.
    cmake_build_dir: str

    # Installation prefix. The destination directory of "ninja install".
    install_prefix: str

    # We set this when we start building the stage.
    stage_start_timestamp_str: Optional[str]

    is_last_stage: bool

    def __init__(
            self,
            build_conf: ClangBuildConf,
            stage_number: int,
            prev_stage: Optional['ClangBuildStage'],
            is_last_stage: bool) -> None:
        # Fields based directly on the parameters.
        self.build_conf = build_conf
        self.stage_number = stage_number
        self.prev_stage = prev_stage
        if self.prev_stage is not None:
            assert self.prev_stage.stage_number != self.stage_number

        parent_dir_for_llvm_version = self.build_conf.llvm_parent_dir_for_specific_version

        # Computed fields.
        self.stage_base_dir = os.path.join(
            parent_dir_for_llvm_version, 'stage-%d' % self.stage_number)
        self.cmake_build_dir = os.path.join(self.stage_base_dir, 'build')

        self.compiler_invocations_top_dir = os.path.join(
            self.stage_base_dir, 'compiler_invocations')
        if is_last_stage:
            self.install_prefix = parent_dir_for_llvm_version
        else:
            self.install_prefix = os.path.join(self.stage_base_dir, 'installed')
        self.stage_start_timestamp_str = None
        self.is_last_stage = is_last_stage

    def get_llvm_enabled_projects(self) -> List[str]:
        enabled_projects = multiline_str_to_list("""
            clang
            compiler-rt
            libcxx
            libcxxabi
            libunwind
            lld
        """)
        if self.stage_number > 1:
            enabled_projects.append('clang-tools-extra')
        return sorted(enabled_projects)

    def get_llvm_cmake_variables(self) -> Dict[str, str]:
        """
        See https://llvm.org/docs/CMake.html for the full list of possible options.

        https://raw.githubusercontent.com/llvm/llvm-project/master/llvm/CMakeLists.txt

        https://raw.githubusercontent.com/llvm/llvm-project/master/clang/CMakeLists.txt
        """
        first_stage = self.prev_stage is None
        if not first_stage:
            assert self.prev_stage is not self
            assert self.stage_number > 1

        ON = 'ON'
        OFF = 'OFF'
        vars = dict(
            LLVM_ENABLE_PROJECTS=';'.join(self.get_llvm_enabled_projects()),
            CMAKE_INSTALL_PREFIX=self.install_prefix,
            CMAKE_BUILD_TYPE='Release',
            LLVM_TARGETS_TO_BUILD='X86',

            CLANG_DEFAULT_CXX_STDLIB='libc++',
            CLANG_DEFAULT_LINKER='lld',

            BUILD_SHARED_LIBS=ON,

            LIBCXXABI_USE_COMPILER_RT=ON,
            LIBCXXABI_USE_LLVM_UNWINDER=ON,

            LIBUNWIND_USE_COMPILER_RT=ON,

            LIBCXX_USE_COMPILER_RT=ON,

            CMAKE_EXPORT_COMPILE_COMMANDS=ON,

            LLVM_ENABLE_RTTI=
        )

        # LIBCXX_CXX_ABI=libcxxabi
        # LIBCXX_USE_COMPILER_RT=On
        # LIBCXXABI_USE_LLVM_UNWINDER=On
        # LIBCXXABI_USE_COMPILER_RT=On
        # LIBCXX_HAS_GCC_S_LIB=Off
        # LIBUNWIND_USE_COMPILER_RT=On

        if not first_stage:
            assert self.prev_stage is not None
            prev_stage_install_prefix = self.prev_stage.install_prefix
            prev_stage_cxx_include_dir = os.path.join(
                prev_stage_install_prefix, 'include', 'c++', 'v1')
            prev_stage_cxx_lib_dir = os.path.join(prev_stage_install_prefix, 'lib')

            # extra_cxx_flags = '-I%s' % prev_stage_cxx_include_dir,
            # extra_linker_flags = ' '.join([
            #     '-L%s' % prev_stage_cxx_lib_dir,
            #     '-Wl,-rpath=%s' % prev_stage_cxx_lib_dir
            # ])

            extra_linker_flags = ' '.join([
                '-Wl,-rpath=%s' % os.path.join(self.install_prefix, 'lib')
            ])

            # To avoid depending on libgcc.a when using Clang's runtime library compiler-rt.
            # Otherwise building protobuf fails to find _Unwind_Resume.
            # _Unwind_Resume is ultimately defined in /lib64/libgcc_s.so.1.
            extra_cxx_flags = ''
            extra_linker_flags = '-Wl,--exclude-libs,libgcc.a'
            vars.update(
                LLVM_ENABLE_LLD=ON,
                LLVM_ENABLE_LIBCXX=ON,
                LLVM_BUILD_TESTS=ON,
                CLANG_DEFAULT_RTLIB='compiler-rt',
                SANITIZER_CXX_ABI='libc++',

                CMAKE_CXX_FLAGS=extra_cxx_flags,
                CMAKE_SHARED_LINKER_FLAGS_INIT=extra_linker_flags,
                CMAKE_MODULE_LINKER_FLAGS_INIT=extra_linker_flags,
                CMAKE_EXE_LINKER_FLAGS_INIT=extra_linker_flags,
                # LIBCXX_CXX_ABI_INCLUDE_PATHS=os.path.join(
                #     prev_stage_install_prefix, 'include', 'c++', 'v1')
                # LLVM_ENABLE_LTO='Full',
            )

        vars.update(get_cmake_args_for_compiler_wrapper())

        return vars

    def build(self) -> None:
        self.stage_start_timestamp_str = get_current_timestamp_str()
        if os.path.exists(self.cmake_build_dir) and self.build_conf.clean_build:
            logging.info("Deleting directory: %s", self.cmake_build_dir)
            rm_rf(self.cmake_build_dir)

        if self.stage_number == 1:
            c_compiler = which('gcc')
            cxx_compiler = which('g++')
        else:
            assert self.prev_stage is not None
            prev_stage_install_prefix = self.prev_stage.install_prefix
            c_compiler = os.path.join(prev_stage_install_prefix, 'bin', 'clang')
            cxx_compiler = os.path.join(prev_stage_install_prefix, 'bin', 'clang++')

        compiler_invocations_dir = os.path.join(
            self.compiler_invocations_top_dir,
            self.build_conf.build_start_timestamp_str)
        mkdir_p(compiler_invocations_dir)
        mkdir_p(self.cmake_build_dir)
        with ChangeDir(self.cmake_build_dir):
            with EnvVarContext(
                    BUILD_CLANG_UNDERLYING_C_COMPILER=c_compiler,
                    BUILD_CLANG_UNDERLYING_CXX_COMPILER=cxx_compiler,
                    BUILD_CLANG_COMPILER_INVOCATIONS_DIR=compiler_invocations_dir):

                cmake_vars = self.get_llvm_cmake_variables()
                run_cmd([
                    self.build_conf.cmake_executable_path,
                    '-G', 'Ninja',
                    '-S', os.path.join(self.build_conf.llvm_project_clone_dir, 'llvm')
                ] + cmake_vars_to_args(cmake_vars))

                #     '-S', llvm_src_path,
                #     '-DLLVM_ENABLE_PROJECTS=%s' % ';'.join(LLVM_ENABLE_PROJECTS),
                #     '-DCMAKE_INSTALL_PREFIX=%s' % llvm_install_prefix,
                #     '-DCMAKE_BUILD_TYPE=Release',
                #     '-DLLVM_TARGETS_TO_BUILD=X86',
                #     # '-DLLVM_BUILD_TESTS=ON',
                #     # '-DLLVM_BUILD_EXAMPLES=ON',
                #     '-DLLVM_CCACHE_BUILD=ON',
                #     '-DLLVM_CCACHE_MAXSIZE=100G',
                #     '-DBOOTSTRAP_LLVM_ENABLE_LLD=ON',
                #     '-DLLVM_CCACHE_DIR=%s' % os.path.expanduser('~/.ccache-llvm')
                # ])

                for target in ['cxxabi', 'cxx', 'compiler-rt', 'clang']:
                    log_info_heading("Building target %s", target)
                    run_cmd(['ninja', target])
                log_info_heading("Building all other targets")
                run_cmd(['ninja'])
                log_info_heading("Installing")
                run_cmd(['ninja', 'install'])

    def check_dynamic_libraries(self) -> None:
        for root, dirs, files in os.walk(self.install_prefix):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                logging.info("File path: %s", file_path)


class ClangBuilder:
    args: Any
    llvm_parent_dir: str
    stages: List[ClangBuildStage]
    build_conf: ClangBuildConf

    def __init__(self) -> None:
        self.stages = []

    def parse_args(self) -> None:
        parser = argparse.ArgumentParser(description='Build Clang')
        parser.add_argument(
            '--remote_server', help='Server to build on',
            default=os.getenv('BUILD_CLANG_REMOTE_SERVER'))
        parser.add_argument(
            '--remote_build_scripts_path',
            help='Remote directory for the build-clang project repo',
            default=os.getenv('BUILD_CLANG_REMOTE_BUILD_SCRIPTS_PATH'))
        parser.add_argument(
            '--clean',
            action='store_true',
            help='Clean the build directory before the build')
        parser.add_argument(
            '--min_stage',
            type=int,
            default=1,
            help='First stage to build')
        parser.add_argument(
            '--max_stage',
            type=int,
            default=NUM_STAGES,
            help='Last stage to build')
        parser.add_argument(
            '--top_dir_suffix',
            help='Suffix to append to the top-level directory that we will use for the build',
            default='')
        parser.add_argument(
            '--llvm_version',
            help='LLVM version to build, e.g. 10.0.1 or 11.0.0',
            default='11.0.0')

        self.args = parser.parse_args()

        if self.args.min_stage < 1:
            raise ValueError("--min-stage value too low: %d" % self.args.min_stage)
        if self.args.max_stage > NUM_STAGES:
            raise ValueError("--max-stage value too high: %d" % self.args.max_stage)
        if self.args.min_stage > self.args.max_stage:
            raise ValueError(
                "--min-stage value (%d) is greater than --max-stage value (%d)" % (
                    self.args.min_stage, self.args.max_stage))

        self.build_conf = ClangBuildConf(
            version=self.args.llvm_version,
            top_dir_suffix=self.args.top_dir_suffix,
            clean_build=self.args.clean
        )

    def init_stages(self) -> None:
        prev_stage: Optional[ClangBuildStage] = None
        for stage_number in range(1, NUM_STAGES + 1):
            self.stages.append(ClangBuildStage(
                build_conf=self.build_conf,
                stage_number=stage_number,
                prev_stage=prev_stage,
                is_last_stage=(stage_number == NUM_STAGES)
            ))
            prev_stage = self.stages[-1]

    def run(self) -> None:
        if os.getenv('BUILD_CLANG_REMOTELY') == '1':
            remote_build.build_remotely(
                remote_server=self.args.remote_server,
                remote_build_scripts_path=self.args.remote_build_scripts_path,
                # TODO: make this an argument?
                remote_mkdir=True
            )
            return

        logging.info("Using LLVM checkout directory %s", self.build_conf.llvm_project_clone_dir)

        activate_devtoolset()

        git_clone_tag(
            LLVM_REPO_URL,
            'llvmorg-%s' % self.build_conf.version,
            self.build_conf.llvm_project_clone_dir)

        self.init_stages()

        for stage in self.stages:
            if self.args.min_stage <= stage.stage_number <= self.args.max_stage:
                logging.info("Building stage %d", stage.stage_number)
                stage.build()
            else:
                logging.info("Skipping stage %d", stage.stage_number)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(filename)s:%(lineno)d] %(asctime)s %(levelname)s: %(message)s")

    builder = ClangBuilder()
    builder.parse_args()
    builder.run()


if __name__ == '__main__':
    main()
