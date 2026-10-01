"""comment_density [ROOT ...] - comment and docstring lines per Python file, densest first."""
import io
import os
import sys
import tokenize

SKIP_DIRS = {"tests", "__pycache__", "proto", "testday", "tools"}


def density(path):
    src = open(path, encoding="utf-8", errors="replace").read()
    total = src.count("\n") or 1
    com = 0
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            com += 1
        elif tok.type == tokenize.STRING and tok.string.lstrip("rbuRBU").startswith(('"""', "'''")):
            com += tok.string.count("\n") + 1
    return com, total


def main(roots):
    rows = []
    for root in roots:
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.endswith(".py"):
                    p = os.path.join(dp, f)
                    try:
                        c, t = density(p)
                    except (tokenize.TokenError, SyntaxError):
                        continue
                    rows.append((c, t, p))
    rows.sort(reverse=True)
    print("files %d, comment+docstring lines %d of %d" % (
        len(rows), sum(r[0] for r in rows), sum(r[1] for r in rows)))
    for c, t, p in rows[:30]:
        print("%5d/%5d %3d%%  %s" % (c, t, 100 * c // t, p))


if __name__ == "__main__":
    main(sys.argv[1:] or ["."])
