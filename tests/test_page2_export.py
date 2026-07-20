import builtins
import csv
import os
import tempfile
import unittest
from unittest.mock import patch

from page2 import Page2Widget


class _ValueControl:
    def __init__(self, value):
        self._value = value

    def text(self):
        return str(self._value)

    def value(self):
        return self._value


class _ExportHarness:
    """Exercise Page2's export methods without constructing a QWidget."""

    export_labeled_imu = Page2Widget.export_labeled_imu
    _export_labeled_csv = Page2Widget._export_labeled_csv
    _label_for_time = Page2Widget._label_for_time
    _time_label_events = Page2Widget._time_label_events
    _save_current_fog_intervals = Page2Widget._save_current_fog_intervals
    _preferred_time_column = staticmethod(Page2Widget._preferred_time_column)
    _timestamp_from_row = staticmethod(Page2Widget._timestamp_from_row)

    def __init__(self, session_dir: str, prefog_s: float = 0.5, include_prefog: bool = True):
        self.dir_input = _ValueControl(session_dir)
        self.prefog_spin = _ValueControl(prefog_s)
        self.fog_intervals = [(1000, 2000)]
        self.experiment_intervals = []
        self._prefog_enabled = include_prefog

    def _include_prefog(self):
        return self._prefog_enabled


class _NoBulkReadText:
    """File proxy that permits iteration/readline but rejects bulk reads."""

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._wrapped.__exit__(exc_type, exc_value, traceback)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._wrapped)

    def readline(self, *args, **kwargs):
        return self._wrapped.readline(*args, **kwargs)

    def read(self, *_args, **_kwargs):
        raise AssertionError("sensor input must not be read into memory in bulk")

    def readlines(self, *_args, **_kwargs):
        raise AssertionError("sensor input must not be read into memory in bulk")

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _write_csv(path: str, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: str):
    with open(path, "r", newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def _read_bytes(path: str):
    with open(path, "rb") as file:
        return file.read()


class Page2LabeledCsvExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.session_dir = self.temporary_dir.name
        self.exporter = _ExportHarness(self.session_dir)

    def test_sync_timestamp_labels_boundaries_and_overwrites_existing_label(self):
        input_path = os.path.join(self.session_dir, "emg.csv")
        output_path = os.path.join(self.session_dir, "emg_labeled.csv")
        rows = [
            {"sync_timestamp": "0.499999", "pc_timestamp": "1.5", "value": "a", "label": "9"},
            {"sync_timestamp": "0.500000", "pc_timestamp": "1.5", "value": "b", "label": "9"},
            {"sync_timestamp": "0.999999", "pc_timestamp": "1.5", "value": "c", "label": "9"},
            {"sync_timestamp": "1.000000", "pc_timestamp": "9.0", "value": "d", "label": "9"},
            {"sync_timestamp": "2.000000", "pc_timestamp": "9.0", "value": "e", "label": "9"},
            {"sync_timestamp": "2.000001", "pc_timestamp": "1.5", "value": "f", "label": "9"},
        ]
        _write_csv(input_path, ["sync_timestamp", "pc_timestamp", "value", "label"], rows)
        source_before = _read_bytes(input_path)

        count = self.exporter._export_labeled_csv(
            input_path,
            output_path,
            prefog_s=0.5,
            include_prefog=True,
        )

        self.assertEqual(count, len(rows))
        exported = _read_csv(output_path)
        self.assertEqual([row["label"] for row in exported], ["0", "1", "1", "2", "2", "0"])
        self.assertEqual([row["value"] for row in exported], list("abcdef"))
        self.assertEqual(list(exported[0]), ["sync_timestamp", "pc_timestamp", "value", "label"])
        self.assertEqual(_read_bytes(input_path), source_before)

    def test_prefog_can_be_disabled(self):
        input_path = os.path.join(self.session_dir, "eeg.csv")
        output_path = os.path.join(self.session_dir, "eeg_labeled.csv")
        _write_csv(
            input_path,
            ["sync_timestamp", "value"],
            [
                {"sync_timestamp": "0.750000", "value": "pre-window"},
                {"sync_timestamp": "1.250000", "value": "fog"},
            ],
        )

        self.exporter._export_labeled_csv(
            input_path,
            output_path,
            prefog_s=0.5,
            include_prefog=False,
        )

        self.assertEqual([row["label"] for row in _read_csv(output_path)], ["0", "2"])

    def test_public_export_generates_imu_emg_and_eeg_labeled_files(self):
        sources = {
            "imu.csv": ("sync_timestamp", "0.750000"),
            "emg.csv": ("pc_timestamp", "1.250000"),
            "eeg.csv": ("relative_timestamp", "2.250000"),
        }
        for filename, (time_column, timestamp) in sources.items():
            _write_csv(
                os.path.join(self.session_dir, filename),
                [time_column, "value"],
                [{time_column: timestamp, "value": filename}],
            )

        with (
            patch("page2.QMessageBox.information") as information,
            patch("page2.QMessageBox.warning") as warning,
            patch("page2.QMessageBox.critical") as critical,
        ):
            self.exporter.export_labeled_imu()

        warning.assert_not_called()
        critical.assert_not_called()
        information.assert_called_once()
        expected = {
            "imu_labeled.csv": ("0.750000", "1"),
            "emg_labeled.csv": ("1.250000", "2"),
            "eeg_labeled.csv": ("2.250000", "0"),
        }
        for filename, (sync_timestamp, label) in expected.items():
            output_path = os.path.join(self.session_dir, filename)
            self.assertTrue(os.path.exists(output_path), filename)
            rows = _read_csv(output_path)
            self.assertEqual(rows[0]["sync_timestamp"], sync_timestamp)
            self.assertEqual(rows[0]["label"], label)

    def test_many_rows_are_exported_by_iteration_without_bulk_read(self):
        input_path = os.path.join(self.session_dir, "imu.csv")
        output_path = os.path.join(self.session_dir, "imu_labeled.csv")
        row_count = 5001
        _write_csv(
            input_path,
            ["sync_timestamp", "sample"],
            (
                {"sync_timestamp": f"{index / 1000.0:.6f}", "sample": str(index)}
                for index in range(row_count)
            ),
        )
        real_open = builtins.open
        absolute_input_path = os.path.abspath(input_path)

        def guarded_open(file, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            opened = real_open(file, *args, **kwargs)
            if os.path.abspath(os.fspath(file)) == absolute_input_path and "r" in mode:
                return _NoBulkReadText(opened)
            return opened

        with patch("builtins.open", side_effect=guarded_open):
            exported_count = self.exporter._export_labeled_csv(
                input_path,
                output_path,
                prefog_s=0.25,
                include_prefog=True,
            )

        self.assertEqual(exported_count, row_count)
        exported = _read_csv(output_path)
        self.assertEqual(len(exported), row_count)
        self.assertEqual(exported[749]["label"], "0")
        self.assertEqual(exported[750]["label"], "1")
        self.assertEqual(exported[1000]["label"], "2")
        self.assertEqual(exported[2000]["label"], "2")
        self.assertEqual(exported[2001]["label"], "0")
        self.assertEqual(exported[-1]["sample"], str(row_count - 1))


if __name__ == "__main__":
    unittest.main()
