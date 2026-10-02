from honed.core import symbols, testfiles


def names(path, patch):
    return {s.name for s in symbols.from_patch(path, patch)}


def test_python_definitions_and_hunk_context():
    patch = (
        "@@ -10,2 +10,3 @@ def fit_transform(self, X):\n-    return X\n+    return scale(X)\n+def _validate_params(x):"
    )
    assert names("sklearn/preprocessing/_data.py", patch) == {"fit_transform", "_validate_params"}


def test_typescript_functions_classes_consts_and_methods():
    patch = (
        "@@ -1,0 +1,6 @@\n"
        "+export function createEditor(config: Config) {\n"
        "+export class LexicalNode {\n"
        "+export const $getRoot = (): RootNode => {\n"
        "+  getTextContent(): string {\n"
        "+interface EditorState {\n"
        "+  if (isSelected(node)) {\n"
    )
    assert names("packages/lexical/src/LexicalEditor.ts", patch) == {
        "createEditor", "LexicalNode", "$getRoot", "getTextContent", "EditorState",
    }  # fmt: skip


def test_cpp_functions_macros_and_types():
    patch = (
        "@@ -5,0 +5,4 @@ namespace db {\n"
        "+future<> table::apply_mutation(const mutation& m) {\n"
        "+#define MAX_SSTABLES 32\n"
        "+struct compaction_descriptor {\n"
        "+    return do_something(m);\n"
    )
    assert names("replica/table.cc", patch) == {"apply_mutation", "MAX_SSTABLES", "compaction_descriptor"}


def test_stopwords_and_short_names_are_ignored():
    assert names("a.py", "@@ -1 +1 @@\n-def get(x):\n+def run(x):") == set()


def test_modules_and_headers():
    assert [s.name for s in symbols.from_path("sklearn/linear_model/_logistic.py")] == ["_logistic"]
    assert [(s.name, s.kind) for s in symbols.from_path("src/include/net/socket.hh")] == [("socket.hh", "header")]
    assert symbols.from_path("packages/x/src/index.ts") == []  # too generic to identify importers


def test_rank_prefers_definitions_and_longer_names():
    found = [
        symbols.Symbol("fit", "hunk_context", "a.py", "fit"),
        symbols.Symbol("predict_proba", "definition", "a.py", "predict_proba"),
        symbols.Symbol("_logistic", "module", "a.py", "_logistic"),
        symbols.Symbol("predict", "definition", "a.py", "predict"),
        symbols.Symbol("fit", "definition", "b.py", "fit"),
    ]
    assert [s.name for s in symbols.rank(found, 10)] == ["predict_proba", "predict", "fit", "_logistic"]
    assert len(symbols.rank(found, 2)) == 2


def test_test_detection_and_stems():
    assert testfiles.is_test("sklearn/linear_model/tests/test_logistic.py")
    assert testfiles.is_test("packages/lexical/src/__tests__/unit/LexicalEditor.test.ts")
    assert testfiles.is_test("test/boost/sstable_test.cc")
    assert not testfiles.is_test("sklearn/linear_model/_logistic.py")
    assert testfiles.stem("sklearn/linear_model/tests/test_logistic.py") == "logistic"
    assert testfiles.stem("sklearn/linear_model/_logistic.py") == "logistic"
    assert testfiles.stem("src/LexicalEditor.test.ts") == "lexicaleditor"
    assert testfiles.stem("test/boost/sstable_test.cc") == "sstable"


def test_near_tests_are_named_after_the_change_and_closest_first():
    tree = [
        "sklearn/linear_model/_logistic.py",
        "sklearn/linear_model/tests/test_logistic.py",
        "sklearn/other/tests/test_logistic.py",
        "sklearn/linear_model/tests/test_ridge.py",
        "benchmarks/deep/deeper/deepest/far/test_logistic.py",
    ]
    found = testfiles.near_tests(["sklearn/linear_model/_logistic.py"], tree, limit=5)
    assert [p for p, _ in found] == [
        "sklearn/linear_model/tests/test_logistic.py",
        "sklearn/other/tests/test_logistic.py",
    ]
    assert found[0][1] == "test named after sklearn/linear_model/_logistic.py"
