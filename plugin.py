from __future__ import annotations
import asyncio
import os
import re
import subprocess

import sublime
import sublime_aio

from itertools import chain
from pathlib import Path


def plugin_loaded():
    """
    Generate a list of known words, provided by static completion files
    of ST's ShellScript package. It contains keywords, built-in commands and
    variables, which don't need to be provided by this plugin and would
    otherwise cause duplicates.
    """
    global KNOWN_COMPLETIONS
    KNOWN_COMPLETIONS = set()

    for res in sublime.find_resources("*.sublime-completions"):
        if res.startswith("Packages/ShellScript/"):
            data = sublime.decode_value(sublime.load_resource(res))
            if data:
                if sublime.score_selector("source.shell.bash", data["scope"].split(" ", 1)[0]) > 0:
                    for item in data["completions"]:
                        trigger = item.get("trigger")
                        if trigger:
                            KNOWN_COMPLETIONS.add(trigger)
                        else:
                            KNOWN_COMPLETIONS.add(str(item))


class BashCompletionListener(sublime_aio.ViewEventListener):
    found_shell=None

    startupinfo = None
    if sublime.platform() == "windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    @classmethod
    def applies_to_primary_view_only(cls):
        return False

    @classmethod
    def is_applicable(cls, settings: sublime.Settings):
        cls.enabled = settings.get("shell.bash.enable_completions", True)
        if not cls.enabled:
            return False

        fname = settings.get("shell.bash.interpreter", None)
        if fname:
            # use configured interpreter
            cls.shell = f"\"{sublime.expand_variables(fname, os.environ)}\""
            return True

        if sublime.platform() != "windows":
            # use normal bash on Linux/MacOS
            cls.shell = "bash"
            return True

        # already know the shell
        if cls.found_shell is None:
            cls.found_shell = False

            # search bash on various default paths
            for fname in (
                "$HOMEDRIVE\\cygwin64\\bin\\bash.exe",
                "$HOMEDRIVE\\cygwin\\bin\\bash.exe",
                "$HOMEDRIVE\\mingw64\\bin\\bash.exe",
                "$HOMEDRIVE\\mingw\\bin\\bash.exe",
                "$PROGRAMFILES\\Git\\bin\\bash.exe",
                "$SYSTEMROOT\\System32\\bash.exe"  # uses WSL as last option
            ):
                fname = sublime.expand_variables(fname, os.environ)
                if fname and os.path.exists(fname):
                    cls.shell = f"\"{fname}\""
                    cls.found_shell = True
                    break

        return cls.found_shell

    async def on_query_completions(self, prefix: str, locations: list[sublime.Point]):
        if not self.enabled:
            return None

        pt = locations[0]
        selector = self.view.settings().get(
            "shell.bash.completion_selector",
            "source.shell - comment - string.quoted"
        )
        if not self.view.match_selector(pt, selector):
            return None

        # get last shell word in front of caret (doesn't account for quotes)
        prefix = self.view.substr(sublime.Region(self.view.line(pt).begin(), pt))
        tokens = re.split(r"(?<!\\)[|&<>()\s]", prefix)
        if tokens:
            prefix = tokens[-1]

        # guess working directory
        file_name = self.view.file_name()
        cwd = Path(file_name).parent if file_name else None

        coros = []

        selector = self.view.settings().get(
            "shell.bash.command_completion_selector",
            "meta.function-call.identifier"
        )
        if self.view.match_selector(pt - 1, selector):
            coros.append(self.get_commands(cwd, prefix))

        selector = self.view.settings().get(
            "shell.bash.file_completion_selector",
            "- meta.function-call.identifier"
        )
        if self.view.match_selector(pt - 1, selector):
            coros.append(self.get_files(cwd, prefix))

        selector = self.view.settings().get(
            "shell.bash.variable_completion_selector",
            ""
        )
        if self.view.match_selector(pt - 1, selector):
            coros.append(self.get_variables(cwd, prefix))

        return chain(*await asyncio.gather(*coros))

    async def get_commands(self, cwd: Path | None, prefix: str):
        """
        Gather all shell commands or globally available executables.
        """
        if not prefix:
            # would cause too many results
            return ()

        text = await self.check_output(f"compgen -c {prefix}", cwd)
        if not text:
            # got nothing, skip!
            return ()

        file_kind = [sublime.KindId.FUNCTION, "f", "command"]
        return (
            sublime.CompletionItem(
                trigger=word,
                kind=file_kind,
                details="shell command"
            )
            for word in set(text.splitlines()) - KNOWN_COMPLETIONS
        )

    async def get_files(self, cwd: Path | None, prefix: str):
        """
        Gather folders and files.
        """
        text = await self.check_output(f"compgen -f {prefix}", cwd)
        if not text:
            return ()

        file_kind = [sublime.KindId.NAMESPACE, "f", "filesystem"]
        return (
            sublime.CompletionItem(
                trigger=word,
                kind=file_kind,
                details="folder or file"
            )
            for word in set(text.splitlines()) - KNOWN_COMPLETIONS
        )

    async def get_variables(self, cwd: Path | None, prefix: str):
        """
        Gather all shell environment variables.
        """
        text = await self.check_output("compgen -v", cwd)
        if not text:
            return ()

        is_var = prefix and prefix[0] == "$"

        file_kind = [sublime.KindId.VARIABLE, "v", "Variable"]
        return (
            sublime.CompletionItem(
                trigger=word,
                completion=word if is_var else f"${word}",
                kind=file_kind,
                details="global environment variable"
            )
            for word in set(text.splitlines()) - KNOWN_COMPLETIONS
        )

    async def check_output(self, cmd: str, cwd: Path | None=None):
        """
        Run command in given login shell.

        :param cmd:
            The command to run
        :param cwd:
            The current working directory.

        :returns:
            Output string from stdout on success or `None` otherwise.
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd=f"{self.shell} -l -c \"{cmd}\"",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                startupinfo=self.startupinfo)

            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode == 0:
                return str(stdout, "utf-8").strip()

        except asyncio.TimeoutError:
            pass

        except FileNotFoundError:
            self.enabled = False
            print("Bash not found, disabling completions!")

        return None
