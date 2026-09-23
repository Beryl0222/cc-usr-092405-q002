"use strict";

const { spawnSync } = require("node:child_process");

// 发现根目录契约测试（service_contract.py）与 tests/ 下全部专题测试
const result = spawnSync(
  "python3",
  ["-m", "unittest", "discover", "-s", ".", "-p", "*.py", "-v"],
  { stdio: "inherit" },
);
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
