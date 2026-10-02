// Cross-checks selectBatch in frontend/js/row_reconcile.js against the fixture table that the
// Python oracle (tests/reconcile_oracle.py) is tested on too (#497). CI has no node, so this runs
// locally: `node --test tests/js/*.test.mjs`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const here = new URL(".", import.meta.url);
const source = readFileSync(new URL("../../frontend/js/row_reconcile.js", here), "utf8");
const context = {};
vm.runInNewContext(source, context);
const { selectBatch } = context.rowReconcile;
const cases = JSON.parse(readFileSync(new URL("select_batch_cases.json", here), "utf8"));

for (const c of cases) {
    test(c.name, () => {
        const { batch, offsets } = selectBatch(c.inflight, c.settled, c.max, c.settledMin, c.offsets);
        assert.deepEqual([...batch], c.batch);
        assert.deepEqual({ ...offsets }, c.nextOffsets);
    });
}
