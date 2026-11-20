# Decision records and shared ordinal reservations

Run the commands from the kit root with Python 3.11 or later. The ledgers store one JSON object per line.

## Decisions

Record the date the decision was made, the person who made or approved it, and a durable reference to their decision. An issue comment permalink, a review, or an approved document can serve as the source. The ledger validates that the date and attribution fields are present. Whoever records the decision verifies the source.

```sh
python3 -m core.ledgers decision record --id default-export \
  --date 2026-02-04 --decided-by 'Example Maintainer' \
  --source 'https://example.org/decisions/17' \
  --summary 'Keep CSV as the default export'
python3 -m core.ledgers decision show --id default-export
```

The default file is `decisions.jsonl` under the kit's XDG state directory in
`ledgers/`. Use `--ledger` to select a different file. IDs are unique within
each file. Give a superseding decision a new ID and update the test to refer to it.

### Pin behavior in pytest or Vitest

Run a real operation and pass its observed result and the decided result to the helper. A mismatch reports the decision ID, date, attribution, and source. Use stable fixture paths from the test runner.

```python
from core.ledgers.pins import assert_decision_pin


def test_default_export():
    assert_decision_pin(
        export_report().format,
        "csv",
        ledger="decisions.jsonl",
        decision_id="default-export",
    )
```

The JavaScript helper works in the Vitest Node environment and uses deep strict equality.

```js
import { test } from 'vitest'
import { assertDecisionPin } from '../core/ledgers/pins.mjs'

test('uses CSV by default', () => {
  assertDecisionPin(exportReport().format, 'csv', {
    ledger: new URL('../decisions.jsonl', import.meta.url),
    decisionId: 'default-export',
  })
})
```

## Ordinals

Workers that allocate numbers for the same resource must use the same ledger path on a filesystem with working POSIX file locks. The command returns the allocated number. It takes the largest recorded number for the resource or the supplied floor, whichever is higher, and adds one. A successful reservation consumes its number even if the work is abandoned.

```sh
python3 -m core.ledgers ordinal reserve \
  --ledger /var/tmp/example-ordinals.jsonl --resource migrations --after 12
```

The default ordinal ledger is `ledgers/resources.jsonl` under the kit's XDG
state directory. Each ledger uses a sibling file ending in `.lock`. Keep
that file in place while writers are active. Separate ledger files allocate
independently.

### Check migrations in CI

Fetch the current target branch and enough history to compute the merge base. Run the check against the proposed commit.

```sh
git fetch origin main:refs/remotes/origin/main
python3 -m core.ledgers ordinal check-migrations \
  --repo . --target origin/main --head HEAD --directory migrations
```

The check reads tracked files from the Git commits. By default it examines `*.sql` under the migration directory, including subdirectories. Use `--pattern` for another filename glob. Use a positive numeric prefix followed by `_`, `-`, or a period, as in `013_add_index.sql`. Leading zeros are allowed. The check requires unique numbers across each tree and requires files introduced since the merge base to exceed the current target's maximum. Exit code 1 signals a migration violation. Exit code 2 signals invalid input or unavailable Git history.

Use `--immutable` for projects where applied migrations must stay fixed.
Run it against the candidate built on the current target to detect changed,
deleted, or renamed migrations from that target.
