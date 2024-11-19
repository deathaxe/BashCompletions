from __future__ import annotations
import asyncio
import re
import subprocess

import sublime
import sublime_plugin

from pathlib import Path
from threading import Thread
from typing import Optional, Awaitable

__loop: Optional[asyncio.AbstractEventLoop] = None
__thread: Optional[Thread] = None

def run_future(future: Awaitable):
    global __loop
    if __loop:
        f = asyncio.ensure_future(future, loop=__loop)
        __loop.call_soon_threadsafe(asyncio.ensure_future, f)


def setup_event_loop():
    global __loop
    global __thread

    if __loop:
        raise RuntimeError("Event loop already running!")
    __loop = asyncio.new_event_loop()
    __thread = Thread(target=__loop.run_forever)
    __thread.start()


def shutdown_event_loop():
    global __loop
    global __thread

    if not __loop:
        raise RuntimeError("No event loop to shutdown!")

    def __shutdown():
        for task in asyncio.all_tasks():
            task.cancel()
        asyncio.get_event_loop().stop()

    if __loop and __thread:
        __loop.call_soon_threadsafe(__shutdown)
        __thread.join()
        __loop.run_until_complete(__loop.shutdown_asyncgens())
        __loop.close()
    __loop = None
    __thread = None


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

    setup_event_loop()


def plugin_unloaded():
    shutdown_event_loop()


class BashCompletionListener(sublime_plugin.ViewEventListener):
    enabled = True

    @classmethod
    def applies_to_primary_view_only(cls):
        return False

    def on_query_completions(self, prefix: str, locations: list[sublime.Point]):
        if not self.enabled:
            return None

        pt = locations[0]
        if not self.view.match_selector(pt, "source.shell - comment - string.quoted"):
            return None

        # get last shell word in front of caret (doesn't account for quotes)
        prefix = self.view.substr(sublime.Region(self.view.line(pt).begin(), pt))
        tokens = re.split(r"(?<!\\)[|&<>()\s]", prefix)
        if tokens:
            prefix = tokens[-1]

        # guess working directory
        file_name = self.view.file_name()
        cwd = Path(file_name).parent if file_name else None

        completions_list = sublime.CompletionList(None)
        run_future(self.resolve_completions(completions_list, cwd, prefix))
        return completions_list

    async def resolve_completions(
        self,
        completions_list: sublime.CompletionList,
        cwd: Path | None,
        prefix: str
    ):
        items = []

        info = None
        if sublime.platform() == "windows":
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = subprocess.SW_HIDE

        try:
            proc = await asyncio.create_subprocess_shell(
                f"bash -c \"compgen -cfv {prefix} \"",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                startupinfo=info)

            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=4.0)
            if proc.returncode == 0:
                file_kind = [sublime.KindId.NAMESPACE, "f", "filesystem"]
                items = (
                    sublime.CompletionItem(
                        trigger=word,
                        kind=file_kind,
                        details="folder or file"
                    )
                    for word in set(str(stdout, "utf-8").splitlines()) - KNOWN_COMPLETIONS
                )

        except asyncio.exceptions.TimeoutError:
            pass

        except FileNotFoundError:
            self.enabled = False
            print("Bash not found, disabling completions!")

        completions_list.set_completions(items)
