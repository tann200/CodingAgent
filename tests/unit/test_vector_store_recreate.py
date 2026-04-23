import pyarrow as pa
from unittest.mock import MagicMock, patch

from src.core.indexing.vector_store import VectorStore


def test_add_with_arrowinvalid_recreates_table(tmp_path):
    ws = str(tmp_path)
    vs = VectorStore(ws)

    # Prepare a mock table whose add raises ArrowInvalid the first time
    initial_tbl = MagicMock()

    def first_add(data=None):
        raise pa.ArrowInvalid("cannot cast list to double")

    initial_tbl.add.side_effect = first_add

    # Recreated table whose add succeeds
    recreated_tbl = MagicMock()
    recreated_tbl.add.return_value = None

    # Patch db.open_table to return the initial table, and create_table to return recreated
    with (
        patch.object(vs.db, "open_table", return_value=initial_tbl),
        patch.object(vs.db, "create_table", return_value=recreated_tbl),
    ):
        # create a dummy schema and data
        schema = MagicMock()
        data = [{"vector": [0.1, 0.2], "text": "x"}]

        # Call internal helper (exercising the index + add flow indirectly)
        new_tbl = vs._add_with_recreate("some_table", initial_tbl, schema, data)

        assert new_tbl is recreated_tbl
        # ensure recreated_tbl.add was called
        recreated_tbl.add.assert_called_once()
