"""Tests for the structured unified-diff parser."""

import unittest

from core.diffparse import parse_diff

DIFF = """diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,6 +10,8 @@ def main():
     return run()
 
+print('hello')
 old line
-removed line one
-removed line two
+added line one
 same context
@@ -30,4 +30,4 @@ def other():
 old2
-removed2
+added2
 end
diff --git a/docs/guide.md b/docs/guide.md
new file mode 100644
index 000..333
--- /dev/null
+++ b/docs/guide.md
@@ -0,0 +1,3 @@
+# Guide
+intro text
+end
"""


class ParseDiffTest(unittest.TestCase):
    def test_files_are_split_with_stats(self):
        files = parse_diff(DIFF)
        self.assertEqual([f.path for f in files], ["src/app.py", "docs/guide.md"])
        self.assertEqual((files[0].added, files[0].removed), (3, 3))
        self.assertEqual((files[1].added, files[1].removed), (3, 0))

    def test_hunks_keep_typed_lines_with_numbers(self):
        files = parse_diff(DIFF)
        hunks = files[0].hunks
        self.assertEqual(len(hunks), 2)
        h = hunks[0]
        self.assertEqual(h.old_start, 10)
        self.assertEqual(h.new_start, 10)
        kinds = [l.kind for l in h.lines]
        self.assertEqual(kinds, ["ctx", "ctx", "add", "ctx", "del", "del", "add", "ctx"])
        add = [l for l in h.lines if l.kind == "add"]
        self.assertEqual(add[0].text, "print('hello')")
        self.assertIsNone(add[0].old_no)
        self.assertEqual(add[0].new_no, 12)
        dele = [l for l in h.lines if l.kind == "del"]
        self.assertIsNone(dele[0].new_no)
        self.assertEqual(dele[0].old_no, 13)

    def test_old_and_new_numbers_advance_across_hunks(self):
        files = parse_diff(DIFF)
        h2 = files[0].hunks[1]
        first_ctx = [l for l in h2.lines if l.kind == "ctx"][0]
        self.assertEqual(first_ctx.old_no, 30)
        self.assertEqual(first_ctx.new_no, 30)

    def test_quoted_paths_with_spaces_are_parsed(self):
        files = parse_diff(
            'diff --git "a/my file.py" "b/my file.py"\n'
            "--- a/my file.py\n+++ b/my file.py\n"
            "@@ -1,1 +1,1 @@\n-old\n+new\n"
        )
        self.assertEqual(files[0].path, "my file.py")

    def test_empty_and_header_only_input(self):
        self.assertEqual(parse_diff(""), [])
        self.assertEqual(parse_diff("diff --git a/x b/x\n"), [])  # no hunks
        self.assertEqual(parse_diff("random text\nno diffs here\n"), [])


if __name__ == "__main__":
    unittest.main()
