import ast
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import numpy as np
import pandas as pd


SCRIPT = Path(__file__).with_name("install_rdagent_serial_factor_dedup.ps1")
TARGET_RELATIVE = Path("rdagent/scenarios/qlib/developer/factor_runner.py")

LEGACY_FACTOR_RUNNER = textwrap.dedent(
    '''\
    from pathlib import Path

    import pandas as pd
    from pandarallel import pandarallel

    pandarallel.initialize(verbose=1)


    class QlibFactorRunner:
        def calculate_information_coefficient(
            self, concat_feature: pd.DataFrame, SOTA_feature_column_size: int, new_feature_columns_size: int
        ) -> pd.DataFrame:
            res = pd.Series(index=range(SOTA_feature_column_size * new_feature_columns_size))
            for col1 in range(SOTA_feature_column_size):
                for col2 in range(SOTA_feature_column_size, SOTA_feature_column_size + new_feature_columns_size):
                    res.loc[col1 * new_feature_columns_size + col2 - SOTA_feature_column_size] = concat_feature.iloc[
                        :, col1
                    ].corr(concat_feature.iloc[:, col2])
            return res

        def deduplicate_new_factors(self, SOTA_feature: pd.DataFrame, new_feature: pd.DataFrame) -> pd.DataFrame:
            concat_feature = pd.concat([SOTA_feature, new_feature], axis=1)
            IC_max = (
                concat_feature.groupby("datetime")
                .parallel_apply(
                    lambda x: self.calculate_information_coefficient(x, SOTA_feature.shape[1], new_feature.shape[1])
                )
                .mean()
            )
            IC_max.index = pd.MultiIndex.from_product([range(SOTA_feature.shape[1]), range(new_feature.shape[1])])
            IC_max = IC_max.unstack().max(axis=0)
            return new_feature.iloc[:, IC_max[IC_max < 0.99].index]

        def combine(self, SOTA_factor, new_factors):
            combined_factors = pd.concat([SOTA_factor, new_factors], axis=1).dropna()
            return combined_factors
    '''
)


def _powershell_executable():
    for candidate in ("powershell.exe", "pwsh.exe", "powershell", "pwsh"):
        executable = shutil.which(candidate)
        if executable:
            return executable
    raise unittest.SkipTest("PowerShell is required for the installer regression test")


def _install(root: Path):
    result = subprocess.run(
        [
            _powershell_executable(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-RdagentRoot",
            str(root),
            "-Python",
            sys.executable,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(
            f"installer failed with exit code {result.returncode}:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _load_runner_class(source: str):
    tree = ast.parse(source)
    runner = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QlibFactorRunner"
    )
    methods = [
        node
        for node in runner.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"calculate_information_coefficient", "deduplicate_new_factors"}
    ]
    module = ast.Module(
        body=[ast.Import(names=[ast.alias(name="pandas", asname="pd")]), ast.ClassDef(
            name="QlibFactorRunner",
            bases=[],
            keywords=[],
            body=methods,
            decorator_list=[],
        )],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, "<patched-factor-runner>", "exec"), namespace)
    return namespace["QlibFactorRunner"]


def _serial_reference(sota: pd.DataFrame, new: pd.DataFrame):
    keep = []
    for new_column in new.columns:
        correlations = []
        for sota_column in sota.columns:
            by_date = []
            paired = pd.concat([sota[sota_column], new[new_column]], axis=1)
            for _, frame in paired.groupby("datetime"):
                by_date.append(frame.iloc[:, 0].corr(frame.iloc[:, 1]))
            correlations.append(pd.Series(by_date, dtype=float).mean())
        maximum = pd.Series(correlations, dtype=float).abs().max()
        if maximum < 0.99:
            keep.append(new_column)
    return keep


class SerialFactorDedupInstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp_dir.name)
        self.target = self.root / TARGET_RELATIVE
        self.target.parent.mkdir(parents=True)
        self.target.write_text(LEGACY_FACTOR_RUNNER, encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_installer_is_idempotent_and_removes_all_parallel_execution(self):
        original = self.target.read_bytes()
        _install(self.root)
        installed = self.target.read_bytes()
        source = installed.decode("utf-8")

        self.assertNotEqual(original, installed)
        self.assertNotIn("pandarallel", source)
        self.assertNotIn("parallel_apply", source)
        self.assertIn('.groupby("datetime")\n            .apply(', source)
        self.assertIn(".unstack().abs().max(axis=0)", source)
        self.assertNotIn("axis=1).dropna()", source)

        backup = Path(f"{self.target}.pre_serial_dedup.bak")
        self.assertEqual(backup.read_bytes(), original)
        first_digest = hashlib.sha256(installed).hexdigest()
        _install(self.root)
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(), first_digest)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(
            list(self.target.parent.glob("factor_runner.py.pre_serial_dedup*.bak")),
            [backup],
        )

    def test_serial_result_matches_pairwise_pearson_reference(self):
        _install(self.root)
        runner = _load_runner_class(self.target.read_text(encoding="utf-8"))()

        index = pd.MultiIndex.from_product(
            [pd.to_datetime(["2026-01-05", "2026-01-06"]), list("abcde")],
            names=["datetime", "instrument"],
        )
        base = np.tile(np.arange(1.0, 6.0), 2)
        sparse_sota = np.tile([np.nan, np.nan, np.nan, 2.0, 1.0], 2)
        sota = pd.DataFrame({"sota": base, "sparse_sota": sparse_sota}, index=index)
        new = pd.DataFrame(
            {
                "negative_duplicate": -base,
                # This has pairwise observations with `sota`, while every row
                # is null in either this factor or `sparse_sota`. A global
                # dropna would incorrectly erase the usable observations.
                "pairwise_keep": np.tile([1.0, 0.0, 1.0, np.nan, np.nan], 2),
                "constant": np.full(len(index), 7.0),
            },
            index=index,
        )

        expected = _serial_reference(sota, new)
        result = runner.deduplicate_new_factors(sota, new)
        self.assertEqual(expected, ["pairwise_keep"])
        self.assertEqual(list(result.columns), expected)
        self.assertTrue(result["pairwise_keep"].isna().any())

    def test_threshold_is_strict_and_absolute(self):
        _install(self.root)
        runner = _load_runner_class(self.target.read_text(encoding="utf-8"))()
        index = pd.MultiIndex.from_product(
            [pd.to_datetime(["2026-01-05", "2026-01-06"]), ["a", "b"]],
            names=["datetime", "instrument"],
        )
        sota = pd.DataFrame({"sota": [1.0, 2.0, 1.0, 2.0]}, index=index)
        new = pd.DataFrame({"candidate": [2.0, 1.0, 2.0, 1.0]}, index=index)

        for correlation, expected_count in ((0.99, 0), (-0.99, 0), (0.989999, 1), (-0.989999, 1)):
            runner.calculate_information_coefficient = (
                lambda _frame, _sota_size, _new_size, value=correlation: pd.Series([value])
            )
            result = runner.deduplicate_new_factors(sota, new)
            self.assertEqual(
                result.shape[1],
                expected_count,
                msg=f"unexpected strict-gate result for correlation={correlation}",
            )


if __name__ == "__main__":
    unittest.main()
