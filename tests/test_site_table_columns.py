"""Sites-table column sizing.

Every data column holds a *cell widget*, which `ResizeToContents` measures as
empty — so the widths must be declared explicitly or the columns collapse. That
showed up as Asset and AZ Rotation being unusable in Propagation Loss mode,
which is the only mode that shows them.
"""

from __future__ import annotations

from waveshed.gui import site_analysis_tab as sat


def _widget_columns():
    """Every column except Location, which stretches instead."""
    return [c for c in range(len(sat._SITE_COLUMNS)) if c != sat._COL_LOCATION]


class TestColumnWidths:
    def test_every_widget_column_has_a_width(self):
        missing = [sat._SITE_COLUMNS[c] for c in _widget_columns()
                   if c not in sat._COL_WIDTHS]
        assert not missing, f"columns with no declared width: {missing}"

    def test_location_is_not_given_a_fixed_width(self):
        """Location stretches; a fixed width there would fight the stretch."""
        assert sat._COL_LOCATION not in sat._COL_WIDTHS

    def test_widths_clear_the_minimum(self):
        for col, width in sat._COL_WIDTHS.items():
            assert width >= sat._COL_MIN_WIDTH, sat._SITE_COLUMNS[col]

    def test_loss_mode_columns_are_wide_enough_to_use(self):
        """Asset and AZ Rotation are the two the LOS/LOSS switch unhides."""
        for col in (sat._COL_ASSET, sat._COL_AZ_ROTATION):
            assert sat._COL_WIDTHS[col] >= 120, sat._SITE_COLUMNS[col]

    def test_total_fits_the_dialog_minimum(self):
        """The dialog must be able to show the table without squeezing.

        This is the coupling that broke: the table needs ~785 px of fixed
        columns in Propagation Loss mode and the dialog minimum was 640.
        """
        from waveshed.gui import main_dialog

        fixed = sum(sat._COL_WIDTHS.values())
        min_width = main_dialog._DEFAULT_SIZE[0]
        assert fixed < min_width, (
            f"fixed columns total {fixed}px but the dialog opens at "
            f"{min_width}px — the table cannot fit"
        )
