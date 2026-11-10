from datetime import datetime, timezone

import pandas as pd
import pytest

from ashare_research.snapshots import freeze
from ashare_research.sources import AcquiredData


def test_dataset_name_cannot_escape_snapshot_root(tmp_path):
    data = AcquiredData("../outside", "synthetic", pd.DataFrame({"x": [1]}),
                        datetime.now(timezone.utc).isoformat(), {})
    with pytest.raises(ValueError, match="dataset"):
        freeze(data, tmp_path)

