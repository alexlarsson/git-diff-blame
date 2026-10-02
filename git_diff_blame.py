#!/usr/bin/env python3
"""Show a unified Git diff with each row attributed to its source revision.

Usage: git_diff_blame.py [git diff arguments]
Examples: git_diff_blame.py HEAD~3..HEAD -- path/to/file
          git_diff_blame.py --cached
With no arguments, compare the index to the working tree, like git diff.
"""

import codecs
import os
import posixpath
import re
import subprocess
import sys


HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
BLAME = re.compile(r"^([0-9a-f]{40,64}) \d+ (\d+)(?: \d+)?$")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], check=True, stdout=subprocess.PIPE,
        text=True, encoding="utf-8", errors="surrogateescape",
        cwd=cwd,
    ).stdout


def git_bytes(*args, cwd=None):
    return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE, cwd=cwd).stdout


def git_diff(*args):
    result = subprocess.run(
        ["git", "diff", *args], stdout=subprocess.PIPE,
        text=True, encoding="utf-8", errors="surrogateescape",
    )
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(result.returncode, result.args)
    return result.stdout


def revision(name):
    return git("rev-parse", "--verify", "--end-of-options", f"{name}^{{commit}}").strip()


def maybe_revision(name):
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "--end-of-options", f"{name}^{{commit}}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def diff_sources(args):
    """Resolve the two sides of common git diff forms for blame."""
    before_paths = args[:args.index("--")] if "--" in args else args
    cached = any(arg in ("--cached", "--staged") for arg in before_paths)
    reverse = any(arg in ("-R", "--reverse") for arg in before_paths)
    merge_base = "--merge-base" in before_paths
    takes_value = {"-U", "--unified", "-G", "-S", "--diff-filter", "--find-object"}
    operands = []
    skip = False
    for arg in before_paths:
        if skip:
            skip = False
        elif arg in takes_value:
            skip = True
        elif arg.startswith("-"):
            continue
        elif "..." in arg:
            left, right = arg.split("...", 1)
            operands.append(("merge", left or "HEAD", right or "HEAD"))
        elif ".." in arg:
            left, right = arg.split("..", 1)
            operands.extend((left or "HEAD", right or "HEAD"))
        elif maybe_revision(arg):
            operands.append(arg)

    base = maybe_revision("HEAD")
    if len(operands) == 1 and isinstance(operands[0], tuple):
        _, left, right = operands[0]
        old = git("merge-base", left, right).strip()
        new = revision(right)
    elif merge_base and operands:
        if len(operands) > 2:
            raise ValueError("Too many revisions for --merge-base")
        old = git("merge-base", *operands, *(["HEAD"] if len(operands) == 1 else [])).strip()
        new = revision(operands[-1]) if len(operands) == 2 else ("INDEX" if cached else "WORKTREE")
    elif len(operands) == 2:
        old, new = (revision(name) for name in operands)
    elif len(operands) == 1:
        old = revision(operands[0])
        new = "INDEX" if cached else "WORKTREE"
    elif not operands:
        old, new = (base or "EMPTY"), "INDEX" if cached else "WORKTREE"
        if not cached:
            old = "INDEX"
    else:
        raise ValueError("Expected at most two revisions")
    return (new, old, base) if reverse else (old, new, base)


def diff_path_prefix(args):
    """Find the repository prefix removed from patch paths by --relative."""
    before_paths = args[:args.index("--")] if "--" in args else args
    prefix = ""
    for arg in before_paths:
        if arg == "--relative":
            prefix = git("rev-parse", "--show-prefix").strip()
        elif arg.startswith("--relative="):
            prefix = arg.split("=", 1)[1]
        elif arg == "--no-relative":
            prefix = ""
    return prefix


def validate_diff_args(args):
    """Reject switches that replace or recolor the patch this parser reads."""
    before_paths = args[:args.index("--")] if "--" in args else args
    incompatible = (
        "--src-prefix", "--dst-prefix", "--no-prefix",
        "--default-prefix", "--line-prefix", "--word-diff",
        "--ext-diff", "--textconv", "--output", "-z",
    )
    for arg in before_paths:
        if any(arg == option or arg.startswith(option + "=") for option in incompatible):
            raise ValueError(f"{arg} changes the patch format and cannot be annotated")


def is_color_arg(arg):
    return arg in ("--color", "--no-color") or any(
        arg == name or arg.startswith(name + "=")
        for name in ("--color-moved", "--color-moved-ws", "--color")
    )


def uncolored_args(args):
    divider = args.index("--") if "--" in args else len(args)
    return [arg for arg in args[:divider] if not is_color_arg(arg)] + args[divider:]


def shortened_hunk(display, visible_length):
    """Hide hunk line numbers while preserving Git's color on the header."""
    position = 0
    visible = 0
    prefix_end = 0
    while match := ANSI.match(display, prefix_end):
        prefix_end = match.end()
    while visible < visible_length:
        match = ANSI.match(display, position)
        if match:
            position = match.end()
        else:
            position += 1
            visible += 1
    return display[:prefix_end] + "@@" + display[position:]


def patch_path(value):
    if value == "/dev/null":
        return None
    if value.startswith('"') and value.endswith('"'):
        raw = value[1:-1].encode("utf-8", "surrogateescape")
        value = os.fsdecode(codecs.escape_decode(raw)[0])
    if not value.startswith(("a/", "b/")):
        raise ValueError(f"Unexpected diff path: {value!r}")
    return value[2:]


def current_content(source, path, root):
    if source == "INDEX":
        return git_bytes("show", f":{path}", cwd=root)
    path = os.path.join(root, path)
    if os.path.islink(path):
        return os.fsencode(os.readlink(path))
    with open(path, "rb") as stream:
        return stream.read()


def blame(rev, path, base, root, origin_path=None):
    """Map final file line numbers to commits using Git's porcelain output."""
    if rev in ("INDEX", "WORKTREE"):
        content = current_content(rev, path, root)
        history_path = None
        for candidate in (path, origin_path):
            if candidate is not None and base is not None and subprocess.run(
                ["git", "cat-file", "-e", f"{base}:{candidate}"], cwd=root,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode == 0:
                history_path = candidate
                break
        if history_path is None:
            return {number: rev for number, _ in enumerate(content.splitlines(), 1)}
        output = subprocess.run(
            ["git", "blame", "--root", "--line-porcelain", "--contents", "-",
             base, "--", history_path],
            input=content, check=True, stdout=subprocess.PIPE, cwd=root,
        ).stdout.decode("utf-8", "surrogateescape")
    else:
        output = git("blame", "--root", "--line-porcelain", rev, "--", path, cwd=root)
    result = {}
    line_number = None
    for row in output.splitlines():
        match = BLAME.match(row)
        if match:
            commit, line_number = match.group(1), int(match.group(2))
        elif row.startswith("\t") and line_number is not None:
            result[line_number] = rev if set(commit) == {"0"} else commit[:10]
            line_number = None
    return result


def path_commit(rev, path, root):
    """Git cannot blame a submodule, so use its last superproject change."""
    if rev in ("INDEX", "WORKTREE"):
        return rev
    commit = git("log", "-1", "--format=%H", rev, "--", path, cwd=root).strip()
    if not commit:
        raise ValueError(f"Missing history for {rev[:10]}:{path}")
    return commit[:10]


def annotate(patch, old_rev, new_rev, root, base=None, path_prefix="", display_patch=None):
    cache = {}
    old_path = new_path = None
    old_line = new_line = None
    rows = patch.splitlines(keepends=True)
    display_rows = (display_patch if display_patch is not None else patch).splitlines(keepends=True)
    if len(rows) != len(display_rows):
        raise ValueError("Colored diff has a different number of lines")
    for row, display in zip(rows, display_rows):
        if ANSI.sub("", display) != row:
            raise ValueError("Colored diff differs from the parsed diff")
        if row.startswith("diff --git "):
            old_path = new_path = None
            old_line = new_line = None
        elif row.startswith("--- ") and old_line is None:
            old_path = patch_path(row[4:].rstrip("\n").split("\t", 1)[0])
            if old_path is not None:
                old_path = posixpath.join(path_prefix, old_path)
        elif row.startswith("+++ ") and old_line is None:
            new_path = patch_path(row[4:].rstrip("\n").split("\t", 1)[0])
            if new_path is not None:
                new_path = posixpath.join(path_prefix, new_path)
        elif match := HUNK.match(row):
            old_line, new_line = int(match.group(1)), int(match.group(2))
            display = shortened_hunk(display, match.end())

        if old_line is None or row[:1] not in (" ", "+", "-"):
            yield display
            continue

        kind = row[0]
        use_old = kind == "-" or (kind == " " and old_rev == "INDEX")
        source = (old_rev, old_path, old_line) if use_old else (new_rev, new_path, new_line)
        rev, path, number = source
        if path is None:
            raise ValueError("Diff row has no source file")
        origin_path = new_path if use_old else old_path
        key = (rev, path, origin_path)
        if row[1:].startswith("Subproject commit "):
            commit = path_commit(rev, path, root)
        else:
            if key not in cache:
                cache[key] = blame(rev, path, base, root, origin_path)
            commit = cache[key].get(number)
            if commit is None:
                raise ValueError(f"Missing blame for {rev[:10]}:{path}:{number}")
        yield f"{commit:<10} |{display}"
        if kind != "+":
            old_line += 1
        if kind != "-":
            new_line += 1


def main(argv):
    if argv in (["-h"], ["--help"]):
        print(__doc__)
        return
    validate_diff_args(argv)
    root = git("rev-parse", "--show-toplevel").strip()
    old_rev, new_rev, base = diff_sources(argv)
    fixed = (
        "--no-ext-diff", "--no-textconv", "--find-renames", "--submodule=short",
        "--src-prefix=a/", "--dst-prefix=b/",
    )
    patch = git_diff(*fixed, "--no-color", *uncolored_args(argv))
    display = git_diff(*fixed, *argv)
    for row in annotate(patch, old_rev, new_rev, root, base, diff_path_prefix(argv), display):
        sys.stdout.write(row)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (subprocess.CalledProcessError, ValueError) as error:
        sys.exit(f"git-diff-blame: {error}")
