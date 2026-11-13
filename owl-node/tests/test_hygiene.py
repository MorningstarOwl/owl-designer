"""Code checks that caught real bugs once.

Thread subclasses must not assign attributes threading.Thread itself uses: six classes stored an Event as `self._stop`,
which on Python <= 3.12 is a method Thread.join()/is_alive() call, so a finished thread raised "'Event' object is not
callable" (the image's Python 3.13 hid it)."""
import ast, os, threading, unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "owlnode")
# everything Thread defines except the two public properties meant to be set (name, daemon)
RESERVED = {n for n in dir(threading.Thread) if not n.startswith("__") and n not in ("name", "daemon")} | {
    "_target", "_args", "_kwargs", "_name", "_daemonic", "_ident", "_started", "_is_stopped", "_initialized",
    "_tstate_lock", "_stderr", "_invoke_excepthook", "_native_id", "_handle", "_stop", "_delete", "_reset_internal_locks"}


class ThreadSubclasses(unittest.TestCase):
    def test_no_thread_internals_shadowed(self):
        bad = []
        for dirpath, _, files in os.walk(ROOT):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                with open(path) as fh:
                    tree = ast.parse(fh.read(), path)
                for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                    if not any(ast.unparse(b) in ("threading.Thread", "Thread") for b in cls.bases):
                        continue
                    for node in ast.walk(cls):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
                        for t in targets:
                            for el in (t.elts if isinstance(t, ast.Tuple) else [t]):
                                if isinstance(el, ast.Attribute) and isinstance(el.value, ast.Name) and el.value.id == "self" and el.attr in RESERVED:
                                    bad.append(f"{os.path.relpath(path, ROOT)}:{el.lineno} {cls.name}.{el.attr}")
        self.assertEqual(bad, [], "these shadow threading.Thread's own attributes")


if __name__ == "__main__":
    unittest.main()
