"""comment_rewrite - pull comments out of files, put rewritten ones back, prove only comments changed.

    python comment_rewrite.py extract FILE...      print every comment block with an id
    python comment_rewrite.py apply MAPFILE        rewrite the blocks named in MAPFILE
    python comment_rewrite.py verify FILE...       code unchanged vs git HEAD, comments aside

MAPFILE holds blocks like this (a block not listed stays as it is):

    @@ path/to/file.py 12
    new comment text, one or more lines
    @@ path/to/file.py 13 DELETE

Python: full-line comment runs, trailing comments and docstrings. Shell: full-line comment
runs only (a # inside a string makes trailing ones unsafe to touch).
"""
import ast
import io
import os
import re
import subprocess
import sys
import tokenize
import warnings


def is_shell(path):
    if path.endswith(".py"):
        return False
    with open(path, encoding="utf-8", errors="replace") as f:
        first = f.readline()
    return path.endswith(".sh") or first.startswith("#!") and ("sh" in first and "python" not in first)


def py_blocks(src):
    lines = src.splitlines(keepends=True)
    blocks = []
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    run = None
    for t in toks:
        if t.type == tokenize.COMMENT:
            row, col = t.start
            full = lines[row - 1][:col].strip() == ""
            if full and not (row == 1 and t.string.startswith("#!")) and not t.string.startswith("# -*-"):
                if run and run["end"] == row - 1 and run["col"] == col:
                    run["end"] = row
                    run["text"].append(t.string[1:].strip())
                else:
                    run = {"kind": "full", "start": row, "end": row, "col": col, "text": [t.string[1:].strip()]}
                    blocks.append(run)
                continue
            if not full and "noqa" not in t.string and "type:" not in t.string:
                blocks.append({"kind": "trail", "start": row, "end": row, "col": col,
                               "text": [t.string[1:].strip()]})
        run = run if t.type in (tokenize.COMMENT, tokenize.NL) else None
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                e = body[0]
                blocks.append({"kind": "doc", "start": e.lineno, "end": e.end_lineno, "col": e.col_offset,
                               "text": body[0].value.value.strip("\n").splitlines(),
                               "sole": len(body) == 1})
    blocks.sort(key=lambda b: (b["start"], b["kind"]))
    return blocks


def sh_blocks(src):
    blocks, run = [], None
    for i, line in enumerate(src.splitlines(), 1):
        s = line.lstrip()
        if s.startswith("#") and not (i == 1 and s.startswith("#!")):
            col = len(line) - len(s)
            if run and run["end"] == i - 1 and run["col"] == col:
                run["end"] = i
                run["text"].append(s[1:].strip())
            else:
                run = {"kind": "full", "start": i, "end": i, "col": col, "text": [s[1:].strip()]}
                blocks.append(run)
        else:
            run = None
    return blocks


def blocks_of(path):
    src = open(path, encoding="utf-8").read()
    return (sh_blocks if is_shell(path) else py_blocks)(src), src


def extract(paths):
    for p in paths:
        blocks, _ = blocks_of(p)
        for n, b in enumerate(blocks):
            print("@@ %s %d  [%s L%d-%d]" % (p, n, b["kind"], b["start"], b["end"]))
            for l in b["text"]:
                print("    " + l)


def read_map(mapfile):
    out, cur = {}, None
    for line in open(mapfile, encoding="utf-8").read().splitlines():
        m = re.match(r"^@@ (\S+) (\d+)(?: (DELETE))?\s*$", line)
        if m:
            cur = (m.group(1), int(m.group(2)))
            out[cur] = None if m.group(3) else []
            continue
        if cur is not None and out[cur] is not None:
            out[cur].append(line)
    return {k: (v if v is None else "\n".join(v).strip("\n").splitlines()) for k, v in out.items()}


def render(b, new, lines, shell):
    ind = " " * b["col"]
    if b["kind"] == "full":
        return [ind + ("# " + t if t else "#") + "\n" for t in new]
    if b["kind"] == "trail":
        line = lines[b["start"] - 1]
        code = line[:b["col"]].rstrip()
        return [code + ("  # " + " ".join(new) if new else "") + "\n"]
    q = '"""'
    # the map holds the docstring's value, so a backslash has to be doubled to survive the literal
    new = [t.replace("\\", "\\\\") for t in new]
    if len(new) == 1:
        return [ind + q + new[0] + q + "\n"]
    return [ind + q + new[0] + "\n"] + [(ind + t if t else "") + "\n" for t in new[1:]] + [ind + q + "\n"]


def apply(mapfile):
    todo = read_map(mapfile)
    for path in sorted({p for p, _ in todo}):
        blocks, src = blocks_of(path)
        shell = is_shell(path)
        lines = src.splitlines(keepends=True)
        for n in sorted((n for p, n in todo if p == path), key=lambda n: -blocks[n]["start"]):
            b, new = blocks[n], todo[(path, n)]
            if new is None:
                if b["kind"] == "doc" and b.get("sole"):
                    raise SystemExit("%s block %d: the docstring is the whole body - shorten it, dont delete" % (path, n))
                rep = render(b, [], lines, shell) if b["kind"] == "trail" else []
            else:
                rep = render(b, new, lines, shell)
            lines[b["start"] - 1:b["end"]] = rep
        open(path, "w", encoding="utf-8", newline="").write("".join(lines))
        print("rewrote %s (%d blocks)" % (path, sum(1 for p, _ in todo if p == path)))


class _StripDocs(ast.NodeTransformer):
    def generic_visit(self, node):
        super().generic_visit(node)
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and \
                isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
        return node


def code_shape(src, shell):
    if shell:
        return [l for l in src.splitlines() if not l.lstrip().startswith("#") or l.startswith("#!")]
    return ast.dump(_StripDocs().visit(ast.parse(src)), include_attributes=False)


def verify(paths):
    bad = 0
    for p in paths:
        shell = is_shell(p)
        rel = os.path.relpath(p, git_root(p)).replace(os.sep, "/")
        old = subprocess.run(["git", "show", "HEAD:" + rel], cwd=git_root(p), capture_output=True,
                             text=True, encoding="utf-8").stdout
        new = open(p, encoding="utf-8").read()
        if not old:
            print("SKIP  %s (not in HEAD)" % p)
            continue
        try:
            same = code_shape(old, shell) == code_shape(new, shell)
            if not shell:
                with warnings.catch_warnings():
                    warnings.simplefilter("error")
                    compile(new, p, "exec")
        except (SyntaxError, SyntaxWarning) as e:
            same = False
            print("  syntax error or warning: %s" % e)
        if shell:
            r = subprocess.run(["bash", "-n", p], capture_output=True, text=True)
            same = same and r.returncode == 0
        print("%s  %s" % ("OK  " if same else "FAIL", p))
        bad += not same
    return 1 if bad else 0


def git_root(p):
    return subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(p)),
                          capture_output=True, text=True).stdout.strip()


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("extract", "apply", "verify"):
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "extract":
        extract(args)
    elif cmd == "apply":
        apply(args[0])
    else:
        sys.exit(verify(args))
