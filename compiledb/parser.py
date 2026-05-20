#!/usr/bin/env python3
#
#   compiledb: Tool for generating LLVM Compilation Database
#   files for make-based build systems.
#
#   Copyright (c) 2017 Nick Diego Yamane <nick.diego@gmail.com>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
import logging
import re

import bashlex
import bashlex.ast

from compiledb.compiler import get_compiler
from compiledb.utils import run_cmd

# Internal variables used to parse build log entries
cc_compile_regex = re.compile(r"^.*-?g?cc-?[0-9.]*$|^.*-?clang-?[0-9.]*$")
cpp_compile_regex = re.compile(r"^.*-?[gc]\+\+-?[0-9.]*$|^.*-?clang\+\+-?[0-9.]*$")
file_regex = re.compile(r"^.+\.c$|^.+\.cc$|^.+\.cpp$|^.+\.cxx$|^.+\.cu$|^.+\.mpp$|^.+\.mxx$|^.+\.s$", re.IGNORECASE)
compiler_wrappers = {"ccache", "icecc", "sccache"}

# Leverage `make --print-directory` option
make_enter_dir = re.compile(r"^\s*make\[\d+\]: Entering directory [`\'\"](?P<dir>.*)[`\'\"]\s*$")
make_leave_dir = re.compile(r"^\s*make\[\d+\]: Leaving directory .*$")

# We want to skip such lines from configure to avoid spurious MAKE expansion errors.
checking_make = re.compile(r"^checking whether .* sets \$\(\w+\)\.\.\. (yes|no)$")
compiler_candidate_regex = re.compile(
    r"(^|[\s;&|()])[\w./+-]*(g?cc|clang|[gc]\+\+|clang\+\+)(-[0-9.]+)?(?=$|[\s;&|().])"
)
command_substitution_markers = ("$(", "`")

logger = logging.getLogger(__name__)


class ParsingResult:
    def __init__(self) -> None:
        self.skipped = 0
        self.count = 0
        self.compdb = []

    def __str__(self) -> str:
        return f"Line count: {self.count}, Skipped: {self.skipped}, Entries: {self.compdb!s}"


class Error(Exception):
    def __init__(self, msg) -> None:
        self.msg = msg

    def __str__(self) -> str:
        return f"Error: {self.msg}"


def iter_preprocessed_build_log(build_log):
    inline_file_pattern = '@"(.*?)"'

    if isinstance(build_log, str):
        build_log = build_log.splitlines()

    for line in build_log:
        result = re.search(inline_file_pattern, line)
        while result is not None:
            inline_file_path = result.group(1)
            with open(inline_file_path) as file:
                inlined_text = file.read()
            line = re.sub(pattern=inline_file_pattern, repl=inlined_text, string=line)
            result = re.search(inline_file_pattern, line)
        yield from line.splitlines()


def preprocess_build_log(build_log):
    return list(iter_preprocessed_build_log(build_log))


def may_contain_compile_command(line) -> bool:
    return compiler_candidate_regex.search(line) is not None


def parse_build_log(build_log, proj_dir, exclude_files, command_style=False, add_predefined_macros=False,
                    use_full_path=False, extra_wrappers=None):
    if extra_wrappers is None:
        extra_wrappers = []
    result = ParsingResult()

    def skip_line(cmd, reason) -> None:
        logger.debug(f"Line {lineno}: {reason}. Ignoring: '{cmd}'")
        result.skipped += 1

    exclude_files_regex = None
    if len(exclude_files) > 0:
        try:
            exclude_files = "|".join(exclude_files)
            exclude_files_regex = re.compile(exclude_files)
        except re.error:
            raise Error(f'Exclude files regex not valid: {exclude_files}')

    compiler_wrappers.update(extra_wrappers)

    dir_stack = [proj_dir]
    working_dir = proj_dir
    lineno = 0

    lines = iter(iter_preprocessed_build_log(build_log))

    # Process build log
    for line in lines:
        lineno += 1
        # Concatenate line if need
        accumulate_line = line
        while line.endswith(('\\\n', '\\')):
            accumulate_line = accumulate_line.removesuffix('\\\n').removesuffix('\\')
            line = next(lines, '')
            accumulate_line += line
        line = accumulate_line.rstrip()

        # Parse directory that make entering/leaving
        enter_dir = make_enter_dir.match(line)
        if enter_dir is not None:
            working_dir = enter_dir.group('dir')
            dir_stack.append(working_dir)
            continue
        if (make_leave_dir.match(line)):
            dir_stack.pop()
            working_dir = dir_stack[-1]
            continue
        if (checking_make.match(line)):
            continue
        if not may_contain_compile_command(line):
            result.skipped += 1
            continue

        commands = []
        try:
            commands = CommandProcessor.process(line, working_dir)
        except Exception as err:
            msg = f'Failed to parse build command [Details: ({type(err)}) {err!s}]'
            skip_line(line, msg)
            continue

        if not commands:
            result.skipped += 1

        for c in commands:
            filepath = c['filepath']
            cmd = c['cmd']
            if filepath is None:
                skip_line(cmd, 'Empty file name')
                continue
            else:
                result.count += 1

            if filepath and exclude_files_regex and exclude_files_regex.match(filepath):
                skip_line(cmd, f"Excluding file (regex='{exclude_files}')")
                continue

            wrappers = c['wrappers']
            unknown = [f"'{w}'" for w in wrappers if w not in compiler_wrappers]
            if unknown:
                unknown = ', '.join(unknown)
                logger.debug(f"Add command with unknown wrapper(s) {unknown}")

            # add entry to database
            tokens = c['tokens']
            arguments = [unescape(a) for a in tokens[len(wrappers):]]

            compiler = get_compiler(arguments[0])

            if add_predefined_macros:
                predefined_macros = compiler.get_predefined_macros(arguments, filepath)
                arguments.extend(predefined_macros)

            if use_full_path:
                arguments[0] = compiler.full_path

            command_str = ' '.join(arguments)

            logger.debug(f"Adding command {len(result.compdb)}: {command_str}")

            if command_style:
                result.compdb.append({
                    'directory': working_dir,
                    'command': command_str,
                    'file': filepath,
                })
            else:
                result.compdb.append({
                    'directory': working_dir,
                    'arguments': arguments,
                    'file': filepath,
                })

    return result


class SubstCommandVisitor(bashlex.ast.nodevisitor):
    """Uses bashlex to parse and process sh/bash substitution commands.
       May result in a parsing exception for invalid commands."""

    def __init__(self) -> None:
        self.substs = []

    def visitcommandsubstitution(self, n, command) -> bool:
        self.substs.append(n)
        return False


class CommandProcessor(bashlex.ast.nodevisitor):
    """Uses bashlex to parse and traverse the resulting bash AST
       looking for and extracting compilation commands."""
    @staticmethod
    def process(line, wd):
        trees = bashlex.parser.parse(line)
        if not trees:
            return []
        if not any(marker in line for marker in command_substitution_markers):
            processor = CommandProcessor(line, wd)
            for tree in trees:
                processor.do_process(tree)
            return processor.commands
        for tree in trees:
            svisitor = SubstCommandVisitor()
            svisitor.visit(tree)
            substs = svisitor.substs
            substs.reverse()
            preprocessed = list(line)
            for s in substs:
                start, end = s.command.pos
                s_cmd = line[start:end]
                out = run_cmd(s_cmd, shell=True, cwd=wd)
                start, end = s.pos
                preprocessed[start:end] = out.strip()
            preprocessed = ''.join(preprocessed)

        trees = bashlex.parser.parse(preprocessed)
        processor = CommandProcessor(preprocessed, wd)
        for tree in trees:
            processor.do_process(tree)
        return processor.commands

    def __init__(self, line, wd) -> None:
        self.line = line
        self.wd = wd
        self.commands = []
        self.reset()

    def reset(self) -> None:
        self.compiler = None
        self.cmd = None
        self.filepath = None
        self.tokens = []
        self.wrappers = []

    def do_process(self, tree):
        self.visit(tree)
        self.check_last_cmd()
        return self.commands

    def visitcommand(self, n, parts) -> bool:
        self.check_last_cmd()
        self.cmd = self.line[n.pos[0]:n.pos[1]]
        logger.debug(f'New command: {self.cmd}')
        return True

    def visitword(self, n, word) -> bool:
        # Check if it looks like an entry of interest and
        # and try to determine the compiler
        if self.compiler is None:
            if ((cc_compile_regex.match(word) or cpp_compile_regex.match(word)) and
                    word not in compiler_wrappers):
                self.compiler = word
            else:
                self.wrappers.append(word)
        elif (file_regex.match(word)):
            self.filepath = word

        self.tokens.append(word)
        return True

    def check_last_cmd(self) -> None:
        # check if it seems to be a compilation command
        if self.compiler is not None:
            self.commands.append({"cmd": self.cmd, "wrappers": self.wrappers, "tokens": self.tokens,
                                 "compiler": self.compiler, "filepath": self.filepath})
        # reset state to process new command
        self.reset()


def unescape(s):
    return s.encode().decode('unicode_escape')

# ex: ts=2 sw=4 et filetype=python
